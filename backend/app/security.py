"""Minimal API auth for prod: Bearer token on mutating routes.

Empty API_TOKEN = open (dev/test, keeps existing tests green).
Set API_TOKEN in prod → all mutating routes require
`Authorization: Bearer <token>`. Webhook keeps its own HMAC check.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from .config import settings


async def require_api_token(authorization: str = Header(default="")) -> None:
    token = settings.api_token.strip()
    if not token:
        return  # dev/test: open
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization.removeprefix("Bearer ").strip()
    if not presented or not hmac.compare_digest(presented, token):
        raise HTTPException(status_code=403, detail="invalid api token")
