"""GitHub App auth: short-lived installation tokens. No secrets in logs/context."""
from __future__ import annotations

import time

import httpx
import jwt as _pyjwt

from app.config import settings


def _private_key() -> str:
    if settings.GITHUB_APP_PRIVATE_KEY:
        return settings.GITHUB_APP_PRIVATE_KEY
    if settings.GITHUB_APP_PRIVATE_KEY_PATH:
        with open(settings.GITHUB_APP_PRIVATE_KEY_PATH, "r", encoding="utf-8") as fh:
            return fh.read()
    raise RuntimeError("GitHub App private key is not configured")


def app_jwt() -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": settings.GITHUB_APP_ID}
    return _pyjwt.encode(payload, _private_key(), algorithm="RS256")


def installation_token(installation_id: str | int) -> str:
    """Exchange App JWT for a 1-hour installation token."""
    url = f"{settings.GITHUB_API_URL}/app/installations/{installation_id}/access_tokens"
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {app_jwt()}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def clone_url_with_token(full_name: str, token: str) -> str:
    # Token is embedded for git only; callers must never log this URL.
    return f"https://x-access-token:{token}@github.com/{full_name}.git"
