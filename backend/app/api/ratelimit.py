"""Phase 4.5 login rate limiting: durable per-account + per-IP buckets.

Counters live in the login_attempts table (restart-safe, multi-instance).
IPs are stored as truncated SHA-256 hashes and rows outside the active
window are pruned — no raw IPs retained indefinitely. Lockouts return the
same generic 401 as bad credentials (no enumeration oracle, no lockout
signal). Successful login resets the account bucket.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.db.database import utcnow
from app.db.models import LoginAttempt

_IP_HASH_LEN = 12


def client_ip_hash(request) -> str:
    """Privacy-conscious client identifier (truncated hash, never raw IP)."""
    try:
        forwarded = (
            (request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()
        )
        raw = forwarded or (request.client.host if request.client else "")
    except Exception:
        raw = ""
    if not raw:
        return "ip:unknown"
    digest = hashlib.sha256(f"fixhub-login|{raw}".encode()).hexdigest()[:_IP_HASH_LEN]
    return f"ip:{digest}"


def _aware(dt):
    """Normalize a stored timestamp to aware UTC (legacy naive rows assumed UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import timezone

        return dt.replace(tzinfo=timezone.utc)
    return dt


def _get(db: Session, bucket: str) -> LoginAttempt | None:
    return db.query(LoginAttempt).filter(LoginAttempt.bucket == bucket).first()


def _active(row: LoginAttempt | None, now, window_start) -> LoginAttempt | None:
    if not row:
        return None
    if (_aware(row.last_seen) or now) < window_start:
        return None
    return row


def is_locked(db: Session, *, email: str, ip_hash: str) -> bool:
    """True when either bucket is currently locked. Read-only (no writes)."""
    now = utcnow()
    window_start = now - timedelta(seconds=settings.LOGIN_WINDOW_S)
    for bucket in (f"email:{email}", ip_hash):
        row = _active(_get(db, bucket), now, window_start)
        if row and row.attempts >= settings.LOGIN_MAX_ATTEMPTS:
            locked_until = (_aware(row.last_seen) or now) + timedelta(
                seconds=settings.LOGIN_LOCKOUT_S
            )
            if locked_until > now:
                return True
    return False


def record_failure(db: Session, *, email: str, ip_hash: str) -> None:
    """Increment both buckets; prune rows outside the window. Never raises."""
    try:
        now = utcnow()
        window_start = now - timedelta(seconds=settings.LOGIN_WINDOW_S)
        for bucket in (f"email:{email}", ip_hash):
            row = _get(db, bucket)
            if row is None or (_aware(row.last_seen) or now) < window_start:
                if row is None:
                    row = LoginAttempt(
                        bucket=bucket, attempts=0, first_seen=now, last_seen=now
                    )
                    db.add(row)
                else:
                    row.attempts = 0
                    row.first_seen = now
                row.locked_until = None
            row.attempts = (row.attempts or 0) + 1
            row.last_seen = now
        # Privacy pruning: drop buckets idle beyond twice the window.
        cutoff = now - timedelta(seconds=2 * settings.LOGIN_WINDOW_S)
        db.query(LoginAttempt).filter(LoginAttempt.last_seen < cutoff).delete(
            synchronize_session=False
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def record_success(db: Session, *, email: str) -> None:
    """Reset the account bucket on successful login. Never raises."""
    try:
        row = _get(db, f"email:{email}")
        if row:
            db.delete(row)
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
