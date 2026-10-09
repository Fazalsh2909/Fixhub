"""Phase 3 key-management service: the ONLY place that encrypts/decrypts keys.

Endpoints pass (authenticated user, provider, api_key, optional model/base_url);
this module handles validation, encryption, storage, retrieval, deletion.
Decrypted secrets leave here solely for runtime client construction and
provider connection tests — callers must drop them immediately.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import LLMCredential
from app.llm import crypto as _crypto
from app.llm import providers as _providers


class NoCredential(RuntimeError):
    """User has no credential for this provider."""


def _utcnow_naive() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)


def create_or_update_credential(
    db: Session,
    *,
    user_id: int,
    provider: str,
    api_key: str,
    model: str = "",
    base_url: str = "",
) -> LLMCredential:
    """Validate, encrypt, and upsert one credential per user + provider."""
    resolved = _providers.resolve_endpoint(
        provider=provider, base_url=base_url, model=model
    )
    key = (api_key or "").strip()
    if len(key) < 8:
        raise ValueError("api_key looks too short to be valid")
    encrypted = _crypto.encrypt_secret(key)
    row = (
        db.query(LLMCredential)
        .filter(
            LLMCredential.user_id == user_id,
            LLMCredential.provider == resolved["provider"],
        )
        .first()
    )
    if row:
        row.encrypted_secret = encrypted
        row.key_hint = _crypto.key_hint(key)
        row.model = resolved["model"]
        if resolved["provider"] == "custom":
            row.base_url = resolved["base_url"]
        row.status = "configured"
        row.last_tested_at = None
    else:
        row = LLMCredential(
            user_id=user_id,
            provider=resolved["provider"],
            base_url=resolved["base_url"] if resolved["provider"] == "custom" else "",
            encrypted_secret=encrypted,
            key_hint=_crypto.key_hint(key),
            model=resolved["model"],
            status="configured",
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def metadata_for_user(db: Session, *, user_id: int) -> list[dict]:
    """Safe metadata for ALL of the user's credentials (never secrets)."""
    rows = (
        db.query(LLMCredential)
        .filter(LLMCredential.user_id == user_id)
        .order_by(LLMCredential.provider.asc())
        .all()
    )
    return [metadata_of(r) for r in rows]


def metadata_of(row: LLMCredential) -> dict:
    return {
        "provider": row.provider,
        "configured": True,
        "model": row.model or "",
        "base_url": row.base_url or "",
        "key_hint": row.key_hint or "",
        "status": row.status or "configured",
        "last_tested_at": str(row.last_tested_at) if row.last_tested_at else None,
        "updated_at": str(row.updated_at),
    }


def get_for_user(db: Session, *, user_id: int, provider: str) -> LLMCredential | None:
    """User-scoped lookup. Never accepts untrusted user IDs from callers."""
    try:
        name = _providers.get_provider(provider).name
    except _providers.UnknownProvider:
        return None
    return (
        db.query(LLMCredential)
        .filter(LLMCredential.user_id == user_id, LLMCredential.provider == name)
        .first()
    )


def validate_credential_access(
    db: Session, *, user_id: int, provider: str
) -> LLMCredential:
    row = get_for_user(db, user_id=user_id, provider=provider)
    if not row:
        raise NoCredential(f"no {provider} credential configured for this account")
    return row


def decrypt_for_runtime(
    db: Session, *, user_id: int, provider: str
) -> tuple[str, str, str]:
    """Return (base_url, api_key, model) for task execution. Scoped by owner."""
    row = validate_credential_access(db, user_id=user_id, provider=provider)
    secret = _crypto.decrypt_secret(row.encrypted_secret)
    try:
        resolved = _providers.resolve_endpoint(
            provider=row.provider,
            base_url=row.base_url if row.provider == "custom" else "",
            model=row.model,
        )
    except ValueError:
        # Stored config drifted (e.g. custom URL cleared): use stored values.
        resolved = {"base_url": row.base_url, "model": row.model}
    base = (
        resolved["base_url"] or _providers.get_provider(row.provider).default_base_url
    )
    return base, secret, row.model or resolved["model"]


def default_provider_for_user(db: Session, *, user_id: int) -> str | None:
    """The user's single default provider (first configured, alpha order)."""
    row = (
        db.query(LLMCredential)
        .filter(LLMCredential.user_id == user_id)
        .order_by(LLMCredential.provider.asc())
        .first()
    )
    return row.provider if row else None


def delete_credential(db: Session, *, user_id: int, provider: str) -> bool:
    """Revoke (delete) a credential. Returns True when a row was removed."""
    row = get_for_user(db, user_id=user_id, provider=provider)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def mark_tested(
    db: Session, *, user_id: int, provider: str, ok: bool, latency_ms: int = 0
) -> None:
    row = get_for_user(db, user_id=user_id, provider=provider)
    if not row:
        return
    row.status = "verified" if ok else "error"
    if ok:
        row.last_tested_at = _utcnow_naive()
    db.commit()


def rotate_credentials(db: Session) -> dict:
    """Phase 4.5 master-key rotation: re-encrypt every credential with the
    current key, verifying each decrypts afterwards. Single transaction —
    any failure rolls everything back (mixed state stays readable via the
    previous-key fallback). Never logs plaintext. Returns counts."""
    rotated = 0
    already = 0
    try:
        # Fail fast when no current key is configured.
        _crypto.encrypt_secret("fixhub-rotation-probe")
    except Exception as exc:
        raise ValueError(f"rotation unavailable: {exc}") from exc
    rows = db.query(LLMCredential).order_by(LLMCredential.id.asc()).all()
    try:
        for row in rows:
            secret = _crypto.decrypt_secret(row.encrypted_secret)
            fresh = _crypto.encrypt_and_verify(secret)
            if fresh != row.encrypted_secret:
                row.encrypted_secret = fresh
                rotated += 1
            else:
                already += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"rotated": rotated, "already_current": already, "total": len(rows)}
