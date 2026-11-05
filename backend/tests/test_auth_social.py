"""Social login tests: Google OIDC + GitHub OAuth, end to end.

No external network: provider exchange/verification helpers are patched at
their module boundary, except for the Google ID-token verifier which is
exercised with a real locally-signed RS256 token.
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import parse_qs, quote, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api import auth_social
from app.api.auth_social import SocialAuthError
from app.config import settings
from app.db.models import AuthIdentity, User
from app.main import app
from tests.conftest import make_user


@pytest.fixture()
def oauth_settings(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setattr(
        settings,
        "GOOGLE_OAUTH_REDIRECT_URI",
        "http://localhost:5173/api/auth/google/callback",
    )
    monkeypatch.setattr(settings, "GITHUB_LOGIN_CLIENT_ID", "github-client-id")
    monkeypatch.setattr(settings, "GITHUB_LOGIN_CLIENT_SECRET", "github-secret")
    monkeypatch.setattr(
        settings,
        "GITHUB_LOGIN_REDIRECT_URI",
        "http://localhost:5173/api/auth/github/callback",
    )
    monkeypatch.setattr(
        settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "test-social-state-key"
    )


def _start(client: TestClient, provider: str, next_path: str = "/") -> dict:
    r = client.get(
        f"/api/auth/{provider}/start?next={quote(next_path)}",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    return {"response": r, "query": parse_qs(urlsplit(r.headers["location"]).query)}


def _google_callback(
    client: TestClient, monkeypatch, claims: dict, next_path: str = "/"
):
    started = _start(client, "google", next_path)
    state = started["query"]["state"][0]
    nonce = started["query"]["nonce"][0]
    seen: dict = {}

    def fake_exchange(code: str, verifier: str) -> dict:
        seen["code"] = code
        seen["verifier"] = verifier
        return {"id_token": "stub-id-token"}

    def fake_verify(token: str, token_nonce: str) -> dict:
        seen["token"] = token
        seen["nonce"] = token_nonce
        return claims

    monkeypatch.setattr(auth_social, "_google_exchange_code", fake_exchange)
    monkeypatch.setattr(auth_social, "_google_verify_id_token", fake_verify)
    r = client.get(
        f"/api/auth/google/callback?code=google-code&state={quote(state)}",
        follow_redirects=False,
    )
    return r, seen


def _github_callback(
    client: TestClient,
    monkeypatch,
    *,
    profile: dict,
    email: str,
    next_path: str = "/",
):
    started = _start(client, "github", next_path)
    state = started["query"]["state"][0]
    seen: dict = {}

    def fake_exchange(code: str, verifier: str) -> dict:
        seen["code"] = code
        seen["verifier"] = verifier
        return {"access_token": "github-access-token"}

    monkeypatch.setattr(auth_social, "_github_exchange_code", fake_exchange)
    monkeypatch.setattr(
        auth_social, "_github_fetch_profile", lambda token: profile
    )
    monkeypatch.setattr(
        auth_social, "_github_primary_verified_email", lambda token: email
    )
    r = client.get(
        f"/api/auth/github/callback?code=github-code&state={quote(state)}",
        follow_redirects=False,
    )
    return r, seen


# --- provider status + start -------------------------------------------------


def test_providers_reports_nested_enabled_flags(db, oauth_settings):
    r = TestClient(app).get("/api/auth/providers")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "google": {"enabled": True},
        "github": {"enabled": True},
    }


def test_providers_disabled_when_config_missing(db, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GITHUB_LOGIN_CLIENT_ID", "")
    body = TestClient(app).get("/api/auth/providers").json()
    assert body == {
        "google": {"enabled": False},
        "github": {"enabled": False},
    }


def test_google_start_redirects_with_pkce_state_and_cookie(db, oauth_settings):
    c = TestClient(app)
    started = _start(c, "google", "/?view=settings")
    r = started["response"]
    q = started["query"]

    assert r.headers["location"].startswith(auth_social.GOOGLE_AUTH_URL + "?")
    assert q["client_id"] == ["google-client-id"]
    assert q["redirect_uri"] == [
        "http://localhost:5173/api/auth/google/callback"
    ]
    assert q["scope"] == ["openid email profile"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"][0] and q["nonce"][0]

    cookie = c.cookies.get("fixhub_google_state")
    assert cookie
    cookie_data = auth_social._state_decode(cookie)
    assert cookie_data is not None
    assert cookie_data["pr"] == "google"
    assert cookie_data["p"] == "/?view=settings"
    verifier = cookie_data["k"]
    assert q["code_challenge"][0] == auth_social._b64url_encode(
        hashlib.sha256(verifier.encode()).digest()
    )
    assert "httponly" in r.headers["set-cookie"].lower()


def test_github_start_redirects_with_expected_scope(db, oauth_settings):
    c = TestClient(app)
    started = _start(c, "github")
    r = started["response"]
    q = started["query"]
    assert r.headers["location"].startswith(auth_social.GITHUB_AUTH_URL + "?")
    assert q["scope"] == ["read:user user:email"]
    assert q["code_challenge_method"] == ["S256"]
    assert c.cookies.get("fixhub_github_state")


def test_start_rejected_when_provider_not_configured(db, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GITHUB_LOGIN_CLIENT_ID", "")
    c = TestClient(app)
    assert c.get("/api/auth/google/start").status_code == 400
    assert c.get("/api/auth/github/start").status_code == 400


# --- Google callback ---------------------------------------------------------


def test_google_new_user_signup_sets_session(db, oauth_settings, monkeypatch):
    c = TestClient(app)
    claims = {
        "sub": "google-sub-1",
        "email": "New.Google@Example.com",
        "email_verified": True,
        "name": "New Google",
        "picture": "https://example.com/a.png",
    }
    r, seen = _google_callback(c, monkeypatch, claims)

    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/?auth=google"
    assert seen["code"] == "google-code"
    assert seen["token"] == "stub-id-token"
    assert seen["nonce"]

    user = db.query(User).filter(User.email == "new.google@example.com").first()
    assert user is not None
    assert user.password_hash == ""
    assert user.display_name == "New Google"
    assert user.avatar_url == "https://example.com/a.png"
    identity = db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id).first()
    assert identity.provider == "google"
    assert identity.provider_subject == "google-sub-1"
    assert identity.provider_email == "new.google@example.com"

    me = c.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "new.google@example.com"


def test_google_returning_identity_logs_into_same_user(
    db, oauth_settings, monkeypatch
):
    user = make_user(db, email="returning@example.com")
    db.add(
        AuthIdentity(
            user_id=user.id,
            provider="google",
            provider_subject="google-sub-returning",
            provider_email="old@example.com",
            provider_display_name="Old",
            provider_avatar_url="",
        )
    )
    db.commit()

    c = TestClient(app)
    r, _ = _google_callback(
        c,
        monkeypatch,
        {
            "sub": "google-sub-returning",
            "email": "returning@example.com",
            "email_verified": True,
            "name": "Updated Name",
        },
    )
    assert r.status_code == 303
    assert db.query(User).count() == 1
    identity = db.query(AuthIdentity).first()
    assert identity.user_id == user.id
    assert identity.provider_display_name == "Updated Name"
    assert c.get("/api/auth/me").json()["id"] == user.id


def test_google_verified_email_links_existing_password_account(
    db, oauth_settings, monkeypatch
):
    user = make_user(db, email="existing@example.com", display_name="Local Name")
    original_hash = user.password_hash

    c = TestClient(app)
    r, _ = _google_callback(
        c,
        monkeypatch,
        {
            "sub": "google-sub-link",
            "email": "existing@example.com",
            "email_verified": True,
            "name": "Provider Name",
            "picture": "https://example.com/p.png",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?auth=google"
    assert db.query(User).count() == 1
    linked = db.query(AuthIdentity).first()
    assert linked.provider_subject == "google-sub-link"
    assert linked.user_id == user.id
    db.refresh(user)
    assert user.password_hash == original_hash
    assert user.display_name == "Local Name"  # local profile wins
    assert user.avatar_url == "https://example.com/p.png"  # empty field filled
    # Password login still works after linking.
    c2 = TestClient(app)
    assert (
        c2.post(
            "/api/auth/login",
            json={"email": "existing@example.com", "password": "test-password-123"},
        ).status_code
        == 200
    )


def test_google_inactive_identity_is_rejected(db, oauth_settings, monkeypatch):
    user = make_user(db, email="off@example.com")
    user.is_active = 0
    db.add(
        AuthIdentity(
            user_id=user.id,
            provider="google",
            provider_subject="google-sub-off",
            provider_email="off@example.com",
        )
    )
    db.commit()

    c = TestClient(app)
    r, _ = _google_callback(
        c,
        monkeypatch,
        {
            "sub": "google-sub-off",
            "email": "off@example.com",
            "email_verified": True,
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?auth_error=disabled"
    assert c.get("/api/auth/me").status_code == 401


def test_google_exchange_failure_returns_app_error(
    db, oauth_settings, monkeypatch
):
    c = TestClient(app)
    started = _start(c, "google")
    state = started["query"]["state"][0]

    def boom(code: str, verifier: str) -> dict:
        raise SocialAuthError("provider")

    monkeypatch.setattr(auth_social, "_google_exchange_code", boom)
    r = c.get(
        f"/api/auth/google/callback?code=x&state={quote(state)}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?auth_error=provider"
    assert db.query(User).count() == 0


def test_callback_rejects_tampered_state(db, oauth_settings):
    c = TestClient(app)
    _start(c, "google")
    r = c.get(
        "/api/auth/google/callback?code=x&state=forged",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?auth_error=state"


def test_callback_state_is_single_use(db, oauth_settings, monkeypatch):
    c = TestClient(app)
    claims = {
        "sub": "google-sub-once",
        "email": "once@example.com",
        "email_verified": True,
    }
    r, _ = _google_callback(c, monkeypatch, claims)
    state = parse_qs(urlsplit(str(r.request.url)).query).get("state", [""])[0]
    # The flow consumed and cleared its cookie: the same URL cannot replay.
    r2 = c.get(
        f"/api/auth/google/callback?code=again&state={quote(state)}",
        follow_redirects=False,
    )
    assert r2.headers["location"] == "/?auth_error=state"


def test_google_success_redirect_preserves_safe_next_only(
    db, oauth_settings, monkeypatch
):
    claims = {
        "sub": "google-sub-next",
        "email": "next@example.com",
        "email_verified": True,
    }
    c = TestClient(app)
    r, _ = _google_callback(c, monkeypatch, claims, "/?view=settings")
    assert r.headers["location"] == "/?view=settings&auth=google"

    c2 = TestClient(app)
    r2, _ = _google_callback(c2, monkeypatch, claims, "//evil.example")
    assert r2.headers["location"] == "/?auth=google"


# --- GitHub callback ---------------------------------------------------------


def test_github_new_user_signup_sets_session(db, oauth_settings, monkeypatch):
    c = TestClient(app)
    profile = {
        "id": 424242,
        "login": "octocat",
        "name": "Octo Cat",
        "avatar_url": "https://avatars.example.com/octo.png",
        "email": None,
    }
    r, seen = _github_callback(
        c, monkeypatch, profile=profile, email="Octo@Example.com"
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?auth=github"
    assert seen["code"] == "github-code"
    assert seen["verifier"]

    user = db.query(User).filter(User.email == "octo@example.com").first()
    assert user is not None
    assert user.password_hash == ""
    assert user.display_name == "Octo Cat"
    identity = db.query(AuthIdentity).first()
    assert identity.provider == "github"
    assert identity.provider_subject == "424242"
    assert c.get("/api/auth/me").json()["email"] == "octo@example.com"


def test_github_returning_identity_logs_into_same_user(
    db, oauth_settings, monkeypatch
):
    user = make_user(db, email="gh-return@example.com")
    db.add(
        AuthIdentity(
            user_id=user.id,
            provider="github",
            provider_subject="999",
            provider_email="gh-return@example.com",
        )
    )
    db.commit()

    c = TestClient(app)
    r, _ = _github_callback(
        c,
        monkeypatch,
        profile={"id": 999, "login": "gh-return"},
        email="gh-return@example.com",
    )
    assert r.status_code == 303
    assert db.query(User).count() == 1
    assert c.get("/api/auth/me").json()["id"] == user.id


def test_github_missing_verified_email_is_rejected(
    db, oauth_settings, monkeypatch
):
    c = TestClient(app)
    r, _ = _github_callback(
        c,
        monkeypatch,
        profile={"id": 123, "login": "no-email"},
        email="",
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?auth_error=email"
    assert db.query(User).count() == 0


def test_github_emails_endpoint_uses_primary_verified_address(monkeypatch):
    calls: list[dict] = []

    class _Resp:
        status_code = 200

        def json(self):
            return [
                {"email": "secondary@example.com", "primary": False, "verified": True},
                {"email": "Primary@Example.com", "primary": True, "verified": True},
            ]

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return _Resp()

    monkeypatch.setattr(auth_social.httpx, "get", fake_get)
    email = auth_social._github_primary_verified_email("gh-token")
    assert email == "primary@example.com"
    assert calls[0]["url"] == "https://api.github.com/user/emails"
    assert calls[0]["headers"]["Authorization"] == "Bearer gh-token"


def test_github_unverified_primary_email_is_not_trusted(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return [
                {"email": "unverified@example.com", "primary": True, "verified": False}
            ]

    monkeypatch.setattr(
        auth_social.httpx, "get", lambda *a, **k: _Resp()
    )
    assert auth_social._github_primary_verified_email("gh-token") == ""


# --- Google ID token verification -------------------------------------------


def _sign_google_token(claims: dict):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-kid"})
    return token, key.public_key()


def _claims(**overrides):
    now = int(time.time())
    base = {
        "iss": "https://accounts.google.com",
        "aud": "google-client-id",
        "sub": "google-sub-verify",
        "email": "verify@example.com",
        "email_verified": True,
        "nonce": "expected-nonce",
        "iat": now,
        "exp": now + 300,
    }
    base.update(overrides)
    return base


def test_google_id_token_verification_accepts_valid_token(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    token, public_key = _sign_google_token(_claims())
    monkeypatch.setattr(
        auth_social, "_google_signing_key", lambda _token: public_key
    )
    payload = auth_social._google_verify_id_token(token, "expected-nonce")
    assert payload["sub"] == "google-sub-verify"


def test_google_id_token_rejects_wrong_nonce(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    token, public_key = _sign_google_token(_claims())
    monkeypatch.setattr(
        auth_social, "_google_signing_key", lambda _token: public_key
    )
    with pytest.raises(SocialAuthError) as exc:
        auth_social._google_verify_id_token(token, "different-nonce")
    assert exc.value.code == "state"


def test_google_id_token_rejects_unverified_email(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    token, public_key = _sign_google_token(_claims(email_verified=False))
    monkeypatch.setattr(
        auth_social, "_google_signing_key", lambda _token: public_key
    )
    with pytest.raises(SocialAuthError) as exc:
        auth_social._google_verify_id_token(token, "expected-nonce")
    assert exc.value.code == "email"


def test_google_id_token_rejects_wrong_issuer_or_audience(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    for overrides in (
        {"iss": "https://evil.example"},
        {"aud": "someone-else"},
    ):
        token, public_key = _sign_google_token(_claims(**overrides))
        monkeypatch.setattr(
            auth_social, "_google_signing_key", lambda _token, pk=public_key: pk
        )
        with pytest.raises(SocialAuthError) as exc:
            auth_social._google_verify_id_token(token, "expected-nonce")
        assert exc.value.code == "provider"


def test_google_id_token_rejects_expired_token(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "google-client-id")
    now = int(time.time())
    token, public_key = _sign_google_token(
        _claims(iat=now - 600, exp=now - 300)
    )
    monkeypatch.setattr(
        auth_social, "_google_signing_key", lambda _token: public_key
    )
    with pytest.raises(SocialAuthError) as exc:
        auth_social._google_verify_id_token(token, "expected-nonce")
    assert exc.value.code == "provider"
