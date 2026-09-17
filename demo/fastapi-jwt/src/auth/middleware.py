"""Auth middleware (demo) — BUG: expired tokens raise unhandled ExpiredSignatureError → 500.

Correct behavior: return 401. Fixhub's job is to catch the expiry and return 401.
"""
from fastapi import Request
from fastapi.responses import JSONResponse

from .token import decode_token


async def auth_middleware(request: Request, call_next):
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
        claims = decode_token(token)  # BUG: raises jwt.ExpiredSignatureError → 500
        request.state.user = claims.get("sub")
    return await call_next(request)
