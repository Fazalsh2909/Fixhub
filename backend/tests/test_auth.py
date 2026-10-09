"""Phase 2 authentication tests: signup / login / session / me / logout.

Deterministic, no external services. Uses the shared `db` fixture (fresh
SQLite file per test) and FastAPI TestClient.
"""

from fastapi.testclient import TestClient

from app.config import settings
from app.db.models import User, UserSession
from app.main import app
from tests.conftest import make_user, session_cookies

GOOD_PW = "correct-horse-123"


def test_signup_ok_sets_cookie_and_safe_user(db):
    c = TestClient(app)
    r = c.post(
        "/api/auth/signup",
        json={"email": "New@Example.com", "password": GOOD_PW, "display_name": "New"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "new@example.com"
    assert body["display_name"] == "New"
    assert "password" not in body and "password_hash" not in body
    assert settings.AUTH_COOKIE_NAME in r.cookies
    u = db.query(User).filter(User.email == "new@example.com").first()
    assert u and u.password_hash and GOOD_PW not in u.password_hash


def test_signup_duplicate_rejected(db):
    make_user(db, email="dup@example.com")
    c = TestClient(app)
    r = c.post(
        "/api/auth/signup", json={"email": "dup@example.com", "password": GOOD_PW}
    )
    assert r.status_code == 409, r.text


def test_signup_invalid_email_rejected(db):
    c = TestClient(app)
    for bad in ("not-an-email", "a@b", "", "x" * 300 + "@example.com"):
        r = c.post("/api/auth/signup", json={"email": bad, "password": GOOD_PW})
        assert r.status_code == 400, (bad, r.text)


def test_signup_weak_password_rejected(db):
    c = TestClient(app)
    for weak in ("", "short", "123456789"):
        r = c.post(
            "/api/auth/signup", json={"email": "w@example.com", "password": weak}
        )
        assert r.status_code == 400, (weak, r.text)


def test_login_ok_and_me(db):
    make_user(db, email="l@example.com", password=GOOD_PW)
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": "l@example.com", "password": GOOD_PW})
    assert r.status_code == 200, r.text
    assert "password_hash" not in r.json()
    me = c.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "l@example.com"


def test_login_wrong_password_rejected(db):
    make_user(db, email="w2@example.com", password=GOOD_PW)
    c = TestClient(app)
    r = c.post(
        "/api/auth/login",
        json={"email": "w2@example.com", "password": "wrong-password-xyz"},
    )
    assert r.status_code == 401, r.text


def test_login_unknown_account_rejected_same_shape(db):
    c = TestClient(app)
    r = c.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": GOOD_PW}
    )
    assert r.status_code == 401, r.text
    # Same error shape as wrong password (no account enumeration).
    assert r.json() == {"detail": "invalid email or password"}


def test_me_unauthenticated_rejected(db):
    c = TestClient(app)
    r = c.get("/api/auth/me")
    assert r.status_code == 401, r.text


def test_me_tampered_cookie_rejected(db):
    c = TestClient(app)
    r = c.get("/api/auth/me", cookies={settings.AUTH_COOKIE_NAME: "forged-token"})
    assert r.status_code == 401, r.text


def test_logout_revokes_session(db):
    u = make_user(db, email="o@example.com")
    cookies = session_cookies(db, u)
    c = TestClient(app)
    assert c.get("/api/auth/me", cookies=cookies).status_code == 200
    r = c.post("/api/auth/logout", cookies=cookies)
    assert r.status_code == 200, r.text
    assert c.get("/api/auth/me", cookies=cookies).status_code == 401
    row = db.query(UserSession).filter(UserSession.user_id == u.id).first()
    assert row and row.revoked == 1


def test_expired_session_rejected(db):
    import hashlib
    import secrets
    from datetime import timedelta

    from app.db.database import utcnow

    u = make_user(db, email="e@example.com")
    raw = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            user_id=u.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=utcnow() - timedelta(seconds=1),
            revoked=0,
        )
    )
    db.commit()
    c = TestClient(app)
    r = c.get("/api/auth/me", cookies={settings.AUTH_COOKIE_NAME: raw})
    assert r.status_code == 401, r.text
