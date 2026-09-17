"""GitHub App auth: App JWT -> installation token (short-lived, cached).

Tokens are returned to callers only — never logged, never stored in DB,
never forwarded to the sandbox. Cache is in-memory with expiry margin.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import jwt

from ..config import settings

API = "https://api.github.com"

# installation_id -> (token, expires_at_epoch)
_cache: dict[str, tuple[str, float]] = {}


def _private_key_pem() -> str:
    if settings.github_app_private_key_path:
        return Path(settings.github_app_private_key_path).read_text(encoding="utf-8")
    return settings.github_private_key


def app_configured() -> bool:
    return bool(settings.github_app_id and _private_key_pem())


def build_app_jwt(app_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def get_installation_token(installation_id: str) -> str:
    """Exchange App JWT for an installation token. Cached until ~60s before expiry."""
    hit = _cache.get(installation_id)
    if hit and hit[1] - 60 > time.time():
        return hit[0]
    pem = _private_key_pem()
    if not settings.github_app_id or not pem:
        raise RuntimeError("GitHub App not configured (app id / private key missing)")
    app_jwt = build_app_jwt(settings.github_app_id, pem)
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
        r.raise_for_status()
        data = r.json()
    token = data["token"]
    # GitHub returns ISO8601 expires_at; fall back to 50 min on parse failure.
    try:
        from datetime import datetime, timezone

        exp = (
            datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except Exception:
        exp = time.time() + 3000
    _cache[installation_id] = (token, exp)
    return token


def clear_token_cache() -> None:
    _cache.clear()
