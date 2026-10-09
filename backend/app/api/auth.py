"""Phase 2 local authentication: signup / login / logout / me.

- POST /api/auth/signup {email, password, display_name?} -> safe user
- POST /api/auth/login {email, password} -> safe user + opaque session cookie
- POST /api/auth/logout -> revoke session + clear cookie
- GET /api/auth/me -> safe user or 401

Security: Argon2id hashing (argon2-cffi); sessions are opaque random tokens
stored as SHA-256 hashes with expiry + revocation; raw token travels solely
as an HttpOnly cookie (Secure in production, SameSite lax). Passwords and
hashes never appear in logs, responses (other than the write-only signup/
login inputs), task context, or agent context.
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.api.deps import _db, _session_token_hash, _utcnow, get_current_user
from app.config import settings
from app.db.models import User, UserSession

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _ph():
    from argon2 import PasswordHasher

    return PasswordHasher()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _validate_signup(email: str, password: str) -> str:
    email = _normalize_email(email)
    if not email or not _EMAIL_RE.match(email) or len(email) > 255:
        raise HTTPException(status_code=400, detail="valid email required")
    if not password or len(password) < settings.AUTH_PASSWORD_MIN_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"password must be at least {settings.AUTH_PASSWORD_MIN_LEN} characters",
        )
    return email


def _safe_user(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name or "",
        "avatar_url": u.avatar_url or "",
        "is_admin": bool(u.is_admin),
    }


def _issue_session(db: Session, response: Response, user: User) -> None:
    raw = secrets.token_urlsafe(32)
    expires = _utcnow() + timedelta(days=settings.AUTH_SESSION_DAYS)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=_session_token_hash(raw),
            expires_at=expires,
            revoked=0,
        )
    )
    db.commit()
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=raw,
        max_age=settings.AUTH_SESSION_DAYS * 86400,
        httponly=True,
        secure=bool(settings.AUTH_COOKIE_SECURE),
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )


@router.post("/api/auth/signup")
def signup(payload: dict, response: Response, db: Session = Depends(_db)) -> dict:
    email = _validate_signup(
        str(payload.get("email", "")), str(payload.get("password", ""))
    )
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="email already registered")
    display = str(payload.get("display_name", "") or "").strip()[:128]
    user = User(
        email=email,
        password_hash=_ph().hash(str(payload.get("password"))),
        display_name=display,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    _issue_session(db, response, user)
    return _safe_user(user)


@router.post("/api/auth/login")
def login(
    payload: dict, request: Request, response: Response, db: Session = Depends(_db)
) -> dict:
    from argon2.exceptions import VerifyMismatchError

    from app.api import ratelimit as _rl

    email = _normalize_email(str(payload.get("email", "")))
    password = str(payload.get("password", ""))
    ip_hash = _rl.client_ip_hash(request)
    # Rate-limit gate first: locked buckets get the generic 401 (no oracle).
    if email and _rl.is_locked(db, email=email, ip_hash=ip_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    user = db.query(User).filter(User.email == email).first() if email else None
    ok = False
    if user and user.password_hash:
        try:
            ok = _ph().verify(user.password_hash, password)
        except VerifyMismatchError:
            ok = False
        except Exception:
            ok = False
    # Same 401 shape for unknown account vs wrong password (no enumeration).
    if not user or not ok:
        if email:
            _rl.record_failure(db, email=email, ip_hash=ip_hash)
        raise HTTPException(status_code=401, detail="invalid email or password")
    if not user.is_active:
        if email:
            _rl.record_failure(db, email=email, ip_hash=ip_hash)
        raise HTTPException(status_code=401, detail="invalid email or password")
    _rl.record_success(db, email=email)
    _issue_session(db, response, user)
    return _safe_user(user)


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(_db)) -> dict:
    raw = request.cookies.get(settings.AUTH_COOKIE_NAME, "")
    if raw:
        row = (
            db.query(UserSession)
            .filter(UserSession.token_hash == _session_token_hash(raw))
            .first()
        )
        if row:
            row.revoked = 1
            db.commit()
    # Mirror issue attributes so the cookie is actually cleared everywhere.
    response.delete_cookie(
        key=settings.AUTH_COOKIE_NAME,
        path="/",
        secure=bool(settings.AUTH_COOKIE_SECURE),
        httponly=True,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return _safe_user(user)
