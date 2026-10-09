"""Phase 3 BYOK endpoints: provider list, credential CRUD, connection test.

- GET /api/llm/providers — supported providers + defaults (no secrets)
- GET /api/llm/credentials — caller's credential metadata only (never keys)
- POST /api/llm/credentials {provider, api_key, model?, base_url?} -> metadata
- POST /api/llm/credentials/test {provider} -> safe test result
- DELETE /api/llm/credentials/{provider} — revoke

All routes require authentication; every credential query is scoped to the
caller (never accepts user_id). Encryption lives in the service layer —
no endpoint here contains crypto logic.
"""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import _db, get_current_user, require_admin, require_csrf
from app.db.models import User
from app.llm import credentials as _creds
from app.llm import crypto as _crypto
from app.llm import providers as _providers

router = APIRouter()


def _safe_error(exc: Exception, secret: str = "") -> tuple[str, str]:
    """Classify a provider failure into a safe category + short message.

    Never includes keys, headers, or raw bodies that could carry secrets.
    """
    name = type(exc).__name__
    text = str(exc)[:300]
    if secret and len(secret) >= 8 and secret in text:
        text = text.replace(secret, "[REDACTED]")
    low = f"{name} {text}".lower()
    if "401" in low or "403" in low or "unauthorized" in low or "invalid" in low:
        return "invalid_credential", "provider rejected the credential (check the key)"
    if "429" in low or "rate" in low or "quota" in low:
        return "rate_limited", "provider rate limit hit — try again shortly"
    if "timeout" in low or "connect" in low or "unreachable" in low:
        return "unreachable", "provider unreachable — try again shortly"
    return "provider_error", "provider request failed"


@router.get("/api/llm/providers")
def list_providers(user: User = Depends(get_current_user)) -> list[dict]:
    _ = user
    return [
        {
            "name": spec.name,
            "label": spec.label,
            "default_base_url": spec.default_base_url,
            "default_model": spec.default_model,
            "needs_base_url": spec.needs_base_url,
            "api_format": spec.api_format,
        }
        for spec in _providers.PROVIDERS.values()
    ]


@router.get("/api/llm/credentials")
def list_credentials(
    user: User = Depends(get_current_user), db: Session = Depends(_db)
) -> list[dict]:
    return _creds.metadata_for_user(db, user_id=user.id)


@router.post("/api/llm/credentials")
def save_credential(
    payload: dict,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> dict:
    require_csrf(request)
    provider = str(payload.get("provider", ""))
    api_key = str(payload.get("api_key", ""))
    model = str(payload.get("model", "") or "")
    base_url = str(payload.get("base_url", "") or "")
    try:
        row = _creds.create_or_update_credential(
            db,
            user_id=user.id,
            provider=provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
        )
    except _providers.UnknownProvider as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300])
    except _crypto.CredentialEncryptionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)[:300])
    return _creds.metadata_of(row)


@router.post("/api/llm/credentials/test")
def test_credential(
    payload: dict,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> dict:
    """Cheap validation: GET /models when supported, else minimal validation.

    Decrypts in memory, performs one lightweight request, discards the
    secret. Never returns the key or raw provider payloads.
    """
    require_csrf(request)
    provider = str(payload.get("provider", ""))
    try:
        row = _creds.validate_credential_access(db, user_id=user.id, provider=provider)
    except _creds.NoCredential as exc:
        raise HTTPException(status_code=404, detail=str(exc)[:300])
    try:
        secret = _crypto.decrypt_secret(row.encrypted_secret)
    except _crypto.CredentialEncryptionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)[:300])
    try:
        resolved = _providers.resolve_endpoint(
            provider=row.provider,
            base_url=row.base_url if row.provider == "custom" else "",
            model=row.model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300])
    base = (
        resolved["base_url"] or _providers.get_provider(row.provider).default_base_url
    )
    started = time.monotonic()
    outcome = _probe(base, secret, resolved["model"])
    latency_ms = int((time.monotonic() - started) * 1000)
    _creds.mark_tested(
        db,
        user_id=user.id,
        provider=row.provider,
        ok=outcome["success"],
        latency_ms=latency_ms,
    )
    return {
        "success": outcome["success"],
        "provider": row.provider,
        "model": row.model or "",
        "latency_ms": latency_ms,
        "error_category": outcome["error_category"],
        "error": outcome["error"],
    }


def _probe(base_url: str, secret: str, model: str) -> dict:
    """One minimal validation request. Returns sanitized outcome dict."""
    headers = {"Authorization": f"Bearer {secret}"}
    try:
        resp = httpx.get(base_url.rstrip("/") + "/models", headers=headers, timeout=20)
    except Exception as exc:
        category, message = _safe_error(exc, secret)
        return {"success": False, "error_category": category, "error": message}
    if resp.status_code == 200:
        return {"success": True, "error_category": "", "error": ""}
    if resp.status_code in (401, 403):
        return {
            "success": False,
            "error_category": "invalid_credential",
            "error": "provider rejected the credential (check the key)",
        }
    if resp.status_code == 404:
        # Endpoint lacks /models: fall back to a minimal validation request
        # against the user's own model (may incur provider usage).
        return _probe_chat(base_url, secret, model)
    if resp.status_code == 429:
        return {
            "success": False,
            "error_category": "rate_limited",
            "error": "provider rate limit hit — try again shortly",
        }
    return {
        "success": False,
        "error_category": "provider_error",
        "error": f"provider returned HTTP {resp.status_code}",
    }


def _probe_chat(base_url: str, secret: str, model: str) -> dict:
    """Fallback validation: one minimal chat request (may incur usage)."""
    try:
        resp = httpx.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ok"}],
                "max_tokens": 1,
            },
            timeout=30,
        )
    except Exception as exc:
        category, message = _safe_error(exc, secret)
        return {"success": False, "error_category": category, "error": message}
    if resp.status_code == 200:
        return {"success": True, "error_category": "", "error": ""}
    if resp.status_code in (401, 403):
        return {
            "success": False,
            "error_category": "invalid_credential",
            "error": "provider rejected the credential (check the key)",
        }
    if resp.status_code == 429:
        return {
            "success": False,
            "error_category": "rate_limited",
            "error": "provider rate limit hit — try again shortly",
        }
    if resp.status_code == 400:
        return {
            "success": False,
            "error_category": "provider_error",
            "error": "provider rejected the validation request (check model name)",
        }
    return {
        "success": False,
        "error_category": "provider_error",
        "error": f"provider returned HTTP {resp.status_code}",
    }


@router.delete("/api/llm/credentials/{provider}")
def delete_credential(
    provider: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> dict:
    require_csrf(request)
    try:
        _providers.get_provider(provider)
    except _providers.UnknownProvider as exc:
        raise HTTPException(status_code=404, detail=str(exc)[:300])
    if not _creds.delete_credential(db, user_id=user.id, provider=provider):
        raise HTTPException(status_code=404, detail="credential not found")
    return {"ok": True, "provider": provider}


@router.post("/api/llm/credentials/rotate")
def rotate_credentials(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(_db),
) -> dict:
    """Phase 4.5 master-key rotation (ADMIN only).

    Re-encrypts every stored credential with the current
    FIXHUB_CREDENTIAL_ENCRYPTION_KEY in one transaction, verifying each.
    Operational procedure: set the new key as current + old as
    FIXHUB_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS, call this, verify counts,
    then remove the old key from the environment. The old key is never
    deleted by this tool. Never returns or logs secrets.
    """
    require_csrf(request)
    try:
        result = _creds.rotate_credentials(db)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)[:300])
    result["by"] = user.email
    return result
