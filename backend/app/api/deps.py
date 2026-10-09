"""Phase 2 authentication + authorization dependencies (centralized, reusable).

- get_current_user: opaque session cookie -> User, else 401.
- require_owned_task / require_owned_repository: ownership-scoped lookups that
  behave like 404 for unauthorized resources (no existence leak).
- _csrf_ok: minimal same-origin check for cookie-authed state-changing requests.

Workers never use these (they load task->owner from the DB directly and must
never receive browser session tokens).
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Repository, Task, User, UserSession


def _db():
    from app.db.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _session_token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    # Phase 4: aware UTC everywhere (PostgreSQL returns tz-aware datetimes).
    # The tzinfo-strip guard below stays for legacy naive rows.
    from datetime import timezone

    return datetime.now(timezone.utc)


def get_current_user(request: Request, db: Session = Depends(_db)) -> User:
    """Authenticate via the opaque session cookie. Never trusts client IDs."""
    raw = request.cookies.get(settings.AUTH_COOKIE_NAME, "")
    if not raw:
        raise HTTPException(status_code=401, detail="not authenticated")
    row = (
        db.query(UserSession)
        .filter(UserSession.token_hash == _session_token_hash(raw))
        .first()
    )
    if not row or row.revoked:
        raise HTTPException(status_code=401, detail="not authenticated")
    if row.expires_at is not None:
        from datetime import timezone

        exp = row.expires_at
        if exp.tzinfo is None:  # legacy naive row: assume UTC
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < _utcnow():
            raise HTTPException(status_code=401, detail="session expired")
    user = db.query(User).filter(User.id == row.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def _csrf_ok(request: Request) -> bool:
    """Minimal CSRF posture for cookie auth: when an Origin header is present
    on a state-changing request, it must match the request host. Same-origin
    fetch always sends a matching Origin; cross-site forgery sends the
    attacker's. Requests without Origin (curl, TestClient, EventSource GET)
    are unaffected."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    origin = request.headers.get("origin", "")
    if not origin:
        return True
    try:
        from urllib.parse import urlparse as _urlparse

        o_host = (_urlparse(origin).hostname or "").lower()
        h_host = (request.headers.get("host", "").split(":")[0] or "").lower()
        return bool(o_host) and o_host == h_host
    except Exception:
        return False


def require_csrf(request: Request) -> None:
    if not _csrf_ok(request):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


def require_owned_task(
    task_id: int, user: User = Depends(get_current_user), db: Session = Depends(_db)
) -> Task:
    """Task scoped to the authenticated user. 404 when missing OR not owned."""
    t = db.query(Task).filter(Task.id == task_id, Task.owner_id == user.id).first()
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    return t


def require_owned_repository_by_name(
    full_name: str, user: User, db: Session
) -> Repository:
    """Repository scoped to the authenticated user. 404 when missing/not owned."""
    row = (
        db.query(Repository)
        .filter(
            Repository.github_full_name == full_name, Repository.owner_id == user.id
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="repository not found")
    return row


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Phase 4.5: operational endpoints need ADMIN, not just login."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="administrator access required")
    return user
