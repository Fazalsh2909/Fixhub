import os
import sys

# Ensure backend/ is importable and use an isolated test database file.
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("WORKSPACE_ROOT", "./test-workspaces")

try:  # Phase 3: throwaway Fernet key for credential tests (never production).
    from cryptography.fernet import Fernet as _Fernet

    os.environ.setdefault(
        "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", _Fernet.generate_key().decode()
    )
except Exception:
    pass

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

TEST_DB = os.path.join(BACKEND, "test-fixhub.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

from app.db.database import Base  # noqa: E402
import app.db.database as _dbmod  # noqa: E402
import app.github.webhook as _webhook  # noqa: E402
import app.tasks.service as _service  # noqa: E402

_test_engine = create_engine(
    f"sqlite:///{TEST_DB}", connect_args={"check_same_thread": False}
)
_TestSession = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=_test_engine)

# Point all SessionLocal users at the test DB.
_dbmod.SessionLocal = _TestSession
_webhook.SessionLocal = _TestSession
_service.SessionLocal = _TestSession


collect_ignore = ["fixtures"]  # deterministic target repos, not our suite

# Final hardening: the live GitHub recovery test is opt-in only (needs
# FIXHUB_LIVE_TEST=1 + FIXHUB_TEST_REPO + FIXHUB_TEST_TOKEN). The normal suite
# must never depend on GitHub credentials.
if os.environ.get("FIXHUB_LIVE_TEST") != "1":
    collect_ignore.append("test_github_recovery_live.py")


def make_user(
    db,
    email: str = "a@example.com",
    password: str = "test-password-123",
    display_name: str = "Test User",
):
    """Phase 2 helper: create a local user directly (bypasses HTTP)."""
    from argon2 import PasswordHasher

    from app.db.models import User

    u = User(
        email=email.strip().lower(),
        password_hash=PasswordHasher().hash(password),
        display_name=display_name,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def session_cookies(db, user) -> dict:
    """Phase 2 helper: mint an opaque session row for `user`, return cookie dict."""
    import hashlib
    import secrets
    from datetime import timedelta

    from app.config import settings
    from app.db.database import utcnow
    from app.db.models import UserSession

    raw = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=utcnow() + timedelta(days=settings.AUTH_SESSION_DAYS),
            revoked=0,
        )
    )
    db.commit()
    return {settings.AUTH_COOKIE_NAME: raw}


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_test_engine)
    Base.metadata.create_all(bind=_test_engine)
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def enc_key(monkeypatch):
    """Throwaway Fernet master key for credential tests (never production)."""
    from cryptography.fernet import Fernet

    from app.config import settings

    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", key)
    return key
