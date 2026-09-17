"""JWT helpers (demo)."""
import time

import jwt

SECRET = "demo-secret"
ALGO = "HS256"


def make_token(expired: bool = False) -> str:
    now = int(time.time())
    payload = {"sub": "user-1", "exp": now - 10 if expired else now + 3600}
    return jwt.encode(payload, SECRET, algorithm=ALGO)


def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=[ALGO])
