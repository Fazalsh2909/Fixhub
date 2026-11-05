"""Social authentication: Google OIDC + GitHub OAuth login.

Flow (both providers):

1. GET /api/auth/{provider}/start?next=/path
   - create a single-use nonce + PKCE S256 verifier
   - put the signed state in the provider URL, and a signed HttpOnly cookie
     carrying the verifier / nonce / safe redirect target
   - 302 to the provider's authorize endpoint
2. GET /api/auth/{provider}/callback?code=...&state=...
   - verify both signatures, expiry, provider, and nonce agreement
   - exchange the code server-side (PKCE verifier sent here only)
   - verify the identity (Google ID token; GitHub verified primary email)
   - link or create the FixHub user, issue the normal opaque session cookie
   - 303 back into the app with `auth=<provider>` (or `auth_error=<code>`)

Security:
- client secrets, access tokens, and ID tokens never reach the browser;
- state is HMAC-signed and bound to a short-lived HttpOnly cookie;
- the PKCE verifier exists only inside that HttpOnly cookie;
- redirect targets are app-relative only (no open redirect);
- Google ID tokens are verified (RS256 JWKS, issuer, audience, expiry, nonce,
  verified email) with PyJWT;
- GitHub subjects use the immutable numeric GitHub user id and only a primary
  VERIFIED email from GET /user/emails is trusted;
- an existing email/password account is linked only after the provider proves
  that email, which is the standard verified-email account-linking rule.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import _issue_session
from app.api.deps import _db
from app.config import settings
from app.db.models import AuthIdentity, User

logger = logging.getLogger("fixhub.auth.social")

router = APIRouter(prefix="/api/auth", tags=["social"])

# --- provider endpoints ------------------------------------------------------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

_STATE_COOKIE = {
    "google": "fixhub_google_state",
    "github": "fixhub_github_state",
}
STATE_TTL_S = 600
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SocialAuthError(Exception):
    """A safe, user-facing error code for a failed social login."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- signed state cookie -----------------------------------------------------


def _state_key() -> bytes:
    """HMAC key derived from the credential master key (never logged).

    Deployed environments already require FIXHUB_CREDENTIAL_ENCRYPTION_KEY at
    startup, so the process-local fallback can never weaken staging/prod. It
    keeps local dev and tests working without an extra secret.
    """
    raw = (settings.FIXHUB_CREDENTIAL_ENCRYPTION_KEY or "").strip()
    if raw:
        return hashlib.sha256(("fixhub-social-oauth|" + raw).encode("utf-8")).digest()
    return _DEV_STATE_KEY


_DEV_STATE_KEY = secrets.token_bytes(32)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes | None:
    try:
        pad = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode((text + pad).encode("ascii"))
    except Exception:
        return None


def _state_encode(payload: dict) -> str:
    blob = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_state_key(), blob.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{blob}.{sig}"


def _state_decode(value: str) -> dict | None:
    """Signature check only; expiry and provider checks are separate."""
    try:
        blob, sig = (value or "").split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_state_key(), blob.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    raw = _b64url_decode(blob)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _safe_next(next_path: str) -> str:
    """Only app-relative redirect targets survive the flow (no open redirect)."""
    value = (next_path or "").strip()
    if (
        value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and "\r" not in value
        and "\n" not in value
    ):
        return value
    return "/"


def _with_query(path: str, key: str, value: str) -> str:
    """Append one query parameter to a safe app-relative path."""
    parts = urlsplit(_safe_next(path))
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in {key, "auth", "auth_error"}
    ]
    query.append((key, value))
    return urlunsplit(("", "", parts.path or "/", urlencode(query), ""))


def _set_state_cookie(response: RedirectResponse, provider: str, value: str) -> None:
    response.set_cookie(
        key=_STATE_COOKIE[provider],
        value=value,
        max_age=STATE_TTL_S,
        httponly=True,
        secure=bool(settings.AUTH_COOKIE_SECURE),
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )


def _clear_state_cookie(response: RedirectResponse, provider: str) -> None:
    response.set_cookie(
        key=_STATE_COOKIE[provider],
        value="",
        max_age=0,
        httponly=True,
        secure=bool(settings.AUTH_COOKIE_SECURE),
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )


def _begin_flow(provider: str, next_path: str) -> dict:
    """Mint one transaction: URL state (opaque) + signed cookie (with verifier)."""
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url_encode(hashlib.sha256(verifier.encode("utf-8")).digest())
    expires = int(time.time()) + STATE_TTL_S
    base = {"v": 2, "n": nonce, "x": expires, "pr": provider}
    return {
        "state": _state_encode(base),
        "cookie": _state_encode(
            {**base, "p": _safe_next(next_path), "k": verifier}
        ),
        "verifier": verifier,
        "challenge": challenge,
        "nonce": nonce,
    }


def _validate_callback_state(
    request: Request, provider: str
) -> dict | None:
    """Both signed payloads must verify, be fresh, agree, and match provider."""
    url_data = _state_decode((request.query_params.get("state") or "").strip())
    cookie_data = _state_decode(
        (request.cookies.get(_STATE_COOKIE[provider]) or "").strip()
    )
    now = int(time.time())
    url_ok = (
        isinstance(url_data, dict)
        and url_data.get("v") == 2
        and url_data.get("pr") == provider
        and isinstance(url_data.get("x"), int)
        and url_data["x"] > now
        and isinstance(url_data.get("n"), str)
        and bool(url_data["n"])
    )
    cookie_ok = (
        isinstance(cookie_data, dict)
        and cookie_data.get("v") == 2
        and cookie_data.get("pr") == provider
        and isinstance(cookie_data.get("x"), int)
        and cookie_data["x"] > now
        and isinstance(cookie_data.get("n"), str)
        and bool(cookie_data["n"])
        and isinstance(cookie_data.get("k"), str)
        and bool(cookie_data["k"])
    )
    if not (url_ok and cookie_ok):
        return None
    if not hmac.compare_digest(str(url_data["n"]), str(cookie_data["n"])):
        return None
    return {
        "next": _safe_next(str(cookie_data.get("p") or "/")),
        "verifier": str(cookie_data["k"]),
        "nonce": str(cookie_data["n"]),
    }


def _callback_error(
    provider: str, code: str, next_path: str = "/"
) -> RedirectResponse:
    """Browser-safe failure: return to the app with a friendly error code."""
    response = RedirectResponse(
        _with_query(next_path, "auth_error", code), status_code=303
    )
    _clear_state_cookie(response, provider)
    return response


def _callback_success(
    db: Session, provider: str, user: User, next_path: str
) -> RedirectResponse:
    response = RedirectResponse(
        _with_query(next_path, "auth", provider), status_code=303
    )
    _issue_session(db, response, user)
    _clear_state_cookie(response, provider)
    return response


# --- user creation / linking -------------------------------------------------


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _find_user_by_email(db: Session, email: str) -> User | None:
    # Signup stores lower-case addresses; lower() keeps legacy mixed-case rows
    # linkable without creating a duplicate account.
    return db.query(User).filter(func.lower(User.email) == email).first()


def _sync_identity(
    identity: AuthIdentity, *, email: str, display_name: str, avatar_url: str
) -> None:
    if email:
        identity.provider_email = email
    if display_name:
        identity.provider_display_name = display_name
    if avatar_url:
        identity.provider_avatar_url = avatar_url


def _fill_profile(user: User, *, display_name: str, avatar_url: str) -> None:
    # Never overwrite a user's chosen local profile with provider data.
    if not user.display_name and display_name:
        user.display_name = display_name
    if not user.avatar_url and avatar_url:
        user.avatar_url = avatar_url


def _login_or_create_user(
    db: Session,
    *,
    provider: str,
    subject: str,
    email: str,
    display_name: str = "",
    avatar_url: str = "",
) -> User:
    """Find an identity, else link a verified email, else create a new user."""
    email = _normalize_email(email)
    if not email or not _EMAIL_RE.match(email) or len(email) > 255:
        raise SocialAuthError("email")

    subject = str(subject or "").strip()
    if not subject or len(subject) > 255:
        raise SocialAuthError("provider")

    display_name = (display_name or "").strip()[:128]
    avatar_url = (avatar_url or "").strip()[:512]

    identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == provider,
            AuthIdentity.provider_subject == subject,
        )
        .first()
    )
    if identity is not None:
        user = db.query(User).filter(User.id == identity.user_id).first()
        if user is None:
            raise SocialAuthError("provider")
        if not user.is_active:
            raise SocialAuthError("disabled")
        _sync_identity(
            identity,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
        )
        _fill_profile(user, display_name=display_name, avatar_url=avatar_url)
        db.commit()
        return user

    user = _find_user_by_email(db, email)
    if user is not None:
        if not user.is_active:
            raise SocialAuthError("disabled")
    else:
        user = User(
            email=email,
            password_hash="",  # social-only user; password login rejects this
            display_name=display_name,
            avatar_url=avatar_url,
            is_active=1,
        )
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            # A concurrent signup won the unique-email race: link to that row.
            db.rollback()
            user = _find_user_by_email(db, email)
            if user is None:
                raise SocialAuthError("provider")
            if not user.is_active:
                raise SocialAuthError("disabled")

    if user.id is not None:
        _fill_profile(user, display_name=display_name, avatar_url=avatar_url)

    identity = AuthIdentity(
        user_id=user.id,
        provider=provider,
        provider_subject=subject,
        provider_email=email,
        provider_display_name=display_name,
        provider_avatar_url=avatar_url,
    )
    db.add(identity)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent callback created the identity (or the user) first.
        db.rollback()
        identity = (
            db.query(AuthIdentity)
            .filter(
                AuthIdentity.provider == provider,
                AuthIdentity.provider_subject == subject,
            )
            .first()
        )
        if identity is None:
            # Email race after our flush: retry the link once on the winner.
            user = _find_user_by_email(db, email)
            if user is None:
                raise SocialAuthError("provider")
            if not user.is_active:
                raise SocialAuthError("disabled")
            db.add(
                AuthIdentity(
                    user_id=user.id,
                    provider=provider,
                    provider_subject=subject,
                    provider_email=email,
                    provider_display_name=display_name,
                    provider_avatar_url=avatar_url,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                identity = (
                    db.query(AuthIdentity)
                    .filter(
                        AuthIdentity.provider == provider,
                        AuthIdentity.provider_subject == subject,
                    )
                    .first()
                )
                if identity is None:
                    raise SocialAuthError("provider")
                user = db.query(User).filter(User.id == identity.user_id).first()
                if user is None or not user.is_active:
                    raise SocialAuthError("disabled")
                return user
        else:
            user = db.query(User).filter(User.id == identity.user_id).first()
            if user is None or not user.is_active:
                raise SocialAuthError("disabled")
            return user
    db.refresh(user)
    return user


# --- Google OIDC -------------------------------------------------------------

_GOOGLE_JWKS_CLIENT = jwt.PyJWKClient(
    GOOGLE_JWKS_URL, cache_keys=True, timeout=15
)

_ALLOWED_GOOGLE_ISSUERS = {
    "https://accounts.google.com",
    "accounts.google.com",
}


def _google_enabled() -> bool:
    return bool(
        (settings.GOOGLE_OAUTH_CLIENT_ID or "").strip()
        and (settings.GOOGLE_OAUTH_CLIENT_SECRET or "").strip()
        and (settings.GOOGLE_OAUTH_REDIRECT_URI or "").strip()
    )


def _google_signing_key(id_token: str):
    try:
        return _GOOGLE_JWKS_CLIENT.get_signing_key_from_jwt(id_token).key
    except Exception as exc:
        raise SocialAuthError("provider") from exc


def _google_exchange_code(code: str, code_verifier: str) -> dict:
    try:
        resp = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except Exception as exc:
        raise SocialAuthError("provider") from exc
    if resp.status_code != 200:
        raise SocialAuthError("provider")
    try:
        data = resp.json()
    except Exception as exc:
        raise SocialAuthError("provider") from exc
    if not isinstance(data, dict):
        raise SocialAuthError("provider")
    return data


def _google_verify_id_token(id_token: str, nonce: str) -> dict:
    """Verify signature/issuer/audience/expiry/nonce and return the claims."""
    try:
        key = _google_signing_key(id_token)
    except SocialAuthError:
        raise
    except Exception as exc:
        raise SocialAuthError("provider") from exc
    try:
        payload = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=settings.GOOGLE_OAUTH_CLIENT_ID,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except Exception as exc:
        raise SocialAuthError("provider") from exc

    configured_issuer = (
        settings.GOOGLE_OAUTH_ISSUER or "https://accounts.google.com"
    ).strip()
    allowed_issuers = _ALLOWED_GOOGLE_ISSUERS | {configured_issuer}
    if payload.get("iss") not in allowed_issuers:
        raise SocialAuthError("provider")

    sent_nonce = str(payload.get("nonce") or "")
    if not nonce or not hmac.compare_digest(sent_nonce, nonce):
        raise SocialAuthError("state")

    verified = payload.get("email_verified")
    if not (verified is True or str(verified).lower() == "true"):
        raise SocialAuthError("email")

    return payload


@router.get("/google/start")
def google_start(next: str = "/"):
    """Send the browser to Google with signed state + PKCE."""
    if not _google_enabled():
        raise HTTPException(
            status_code=400,
            detail=(
                "Google login is not configured "
                "(set GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / "
                "GOOGLE_OAUTH_REDIRECT_URI)"
            ),
        )
    flow = _begin_flow("google", next)
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": flow["state"],
        "code_challenge": flow["challenge"],
        "code_challenge_method": "S256",
        "nonce": flow["nonce"],
    }
    response = RedirectResponse(
        f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302
    )
    _set_state_cookie(response, "google", flow["cookie"])
    return response


@router.get("/google/callback")
def google_callback(request: Request, db: Session = Depends(_db)):
    """Google's redirect target: verify, exchange, log in, return to the app."""
    state = _validate_callback_state(request, "google")
    if state is None:
        # Signature/expiry failure: the redirect target is untrusted.
        return _callback_error("google", "state")
    next_path = state["next"]

    provider_error = (request.query_params.get("error") or "").strip()
    if provider_error:
        return _callback_error("google", "access_denied", next_path)

    code = (request.query_params.get("code") or "").strip()
    if not code:
        return _callback_error("google", "access_denied", next_path)

    try:
        token_data = _google_exchange_code(code, state["verifier"])
        id_token = str(token_data.get("id_token") or "").strip()
        if not id_token:
            raise SocialAuthError("provider")
        claims = _google_verify_id_token(id_token, state["nonce"])
        user = _login_or_create_user(
            db,
            provider="google",
            subject=str(claims.get("sub") or ""),
            email=str(claims.get("email") or ""),
            display_name=str(claims.get("name") or ""),
            avatar_url=str(claims.get("picture") or ""),
        )
    except SocialAuthError as exc:
        return _callback_error("google", exc.code, next_path)
    except Exception:
        logger.warning("google social login failed", exc_info=False)
        return _callback_error("google", "provider", next_path)

    return _callback_success(db, "google", user, next_path)


# --- GitHub OAuth ------------------------------------------------------------


def _github_enabled() -> bool:
    return bool(
        (settings.GITHUB_LOGIN_CLIENT_ID or "").strip()
        and (settings.GITHUB_LOGIN_CLIENT_SECRET or "").strip()
        and (settings.GITHUB_LOGIN_REDIRECT_URI or "").strip()
    )


def _github_exchange_code(code: str, code_verifier: str) -> dict:
    try:
        resp = httpx.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": settings.GITHUB_LOGIN_CLIENT_ID,
                "client_secret": settings.GITHUB_LOGIN_CLIENT_SECRET,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": settings.GITHUB_LOGIN_REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except Exception as exc:
        raise SocialAuthError("provider") from exc
    if resp.status_code != 200:
        raise SocialAuthError("provider")
    try:
        data = resp.json()
    except Exception as exc:
        raise SocialAuthError("provider") from exc
    if not isinstance(data, dict) or not str(data.get("access_token") or ""):
        raise SocialAuthError("provider")
    return data


def _github_api_get(url: str, access_token: str):
    try:
        resp = httpx.get(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
    except Exception as exc:
        raise SocialAuthError("provider") from exc
    if resp.status_code != 200:
        raise SocialAuthError("provider")
    try:
        return resp.json()
    except Exception as exc:
        raise SocialAuthError("provider") from exc


def _github_fetch_profile(access_token: str) -> dict:
    data = _github_api_get(GITHUB_USER_URL, access_token)
    if not isinstance(data, dict):
        raise SocialAuthError("provider")
    return data


def _github_primary_verified_email(access_token: str) -> str:
    """Only GitHub's own primary+verified flag is trusted (never /user.email)."""
    data = _github_api_get(GITHUB_EMAILS_URL, access_token)
    if not isinstance(data, list):
        raise SocialAuthError("provider")
    for entry in data:
        if (
            isinstance(entry, dict)
            and entry.get("primary") is True
            and entry.get("verified") is True
            and entry.get("email")
        ):
            return _normalize_email(str(entry["email"]))
    return ""


@router.get("/github/start")
def github_start(next: str = "/"):
    """Send the browser to GitHub with signed state + PKCE."""
    if not _github_enabled():
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub login is not configured "
                "(set GITHUB_LOGIN_CLIENT_ID / GITHUB_LOGIN_CLIENT_SECRET / "
                "GITHUB_LOGIN_REDIRECT_URI)"
            ),
        )
    flow = _begin_flow("github", next)
    params = {
        "client_id": settings.GITHUB_LOGIN_CLIENT_ID,
        "redirect_uri": settings.GITHUB_LOGIN_REDIRECT_URI,
        "scope": "read:user user:email",
        "state": flow["state"],
        "code_challenge": flow["challenge"],
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(
        f"{GITHUB_AUTH_URL}?{urlencode(params)}", status_code=302
    )
    _set_state_cookie(response, "github", flow["cookie"])
    return response


@router.get("/github/callback")
def github_callback(request: Request, db: Session = Depends(_db)):
    """GitHub's redirect target: verify, exchange, log in, return to the app."""
    state = _validate_callback_state(request, "github")
    if state is None:
        return _callback_error("github", "state")
    next_path = state["next"]

    provider_error = (request.query_params.get("error") or "").strip()
    if provider_error:
        return _callback_error("github", "access_denied", next_path)

    code = (request.query_params.get("code") or "").strip()
    if not code:
        return _callback_error("github", "access_denied", next_path)

    try:
        token_data = _github_exchange_code(code, state["verifier"])
        access_token = str(token_data.get("access_token") or "").strip()
        profile = _github_fetch_profile(access_token)
        github_user_id = profile.get("id")
        if not github_user_id:
            raise SocialAuthError("provider")
        email = _github_primary_verified_email(access_token)
        if not email:
            raise SocialAuthError("email")
        user = _login_or_create_user(
            db,
            provider="github",
            subject=str(int(github_user_id)),
            email=email,
            display_name=str(profile.get("name") or profile.get("login") or ""),
            avatar_url=str(profile.get("avatar_url") or ""),
        )
    except SocialAuthError as exc:
        return _callback_error("github", exc.code, next_path)
    except Exception:
        logger.warning("github social login failed", exc_info=False)
        return _callback_error("github", "provider", next_path)

    return _callback_success(db, "github", user, next_path)


# --- provider status (public, no secrets) ------------------------------------


@router.get("/providers")
def providers() -> dict:
    """Public endpoint: which social providers are fully configured."""
    return {
        "google": {"enabled": _google_enabled()},
        "github": {"enabled": _github_enabled()},
    }
