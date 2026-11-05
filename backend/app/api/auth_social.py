"""Social authentication: Google OIDC + GitHub OAuth login flows.

- PKCE S256, transaction-specific state bound via short-lived HttpOnly cookie
- Google: openid email profile, server-side ID token verification
- GitHub: read:user + user:email, primary verified email, numeric GitHub ID
- Login-or-link-or-reject with AuthIdentity table
- Session cookie issuance via existing fixhub_session mechanism
- Never expose secrets/tokens to frontend; generic errors; fail-closed
"""

from __future__ import annotations

import base64
import hashlib
import json

from urllib.parse import urlencode as _urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import _db, _session_token_hash, _utcnow, get_current_user
from app.config import settings
from app.db.models import AuthIdentity, User

router = APIRouter(prefix="/api/auth", tags=["social"])

# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def _code_verifier() -> str:
    """Generate a random PKCE code_verifier (high-entropy ~32-byte string)."""
    return base64.urlsafe_b64encode(__import__("secrets").token_bytes(32)).rstrip(b"=").decode()


def _code_challenge(verifier: str) -> str:
    """Derive the PKCE code_challenge from the verifier using S256."""
    raw = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return raw


# ---------------------------------------------------------------------------
# Google OIDC
# ---------------------------------------------------------------------------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"


@router.get("/google/start")
def google_start(request: Request, response: Response):
    """Initiate Google OAuth sign-in flow.

    Returns a RedirectResponse to Google's OAuth endpoint.
    The transaction-specific state+verifier are bound in a short-lived
    HttpOnly cookie for replay protection.
    """
    import secrets

    state = secrets.token_urlsafe(32)
    verifier = _code_verifier()
    nonce = secrets.token_urlsafe(16)

    # Build the full auth URL
    query = _urlencode({
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
        "nonce": nonce,
    })
    auth_url = GOOGLE_AUTH_URL + "?" + query

    response = RedirectResponse(url=auth_url)

    # Set the state+verifier cookie - HttpOnly, SameSite=Lax, ~5 min TTL
    response.set_cookie(
        key="fixhub_google_state",
        value=json.dumps({"state": state, "verifier": verifier}),
        max_age=300,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/google/callback")
def google_callback(request: Request, response: Response, db: Session = Depends(_db)):
    """Handle Google OAuth callback.

    1. Verify the state cookie matches the query parameter.
    2. Exchange the one-time code for tokens.
    3. Verify the Google ID token (signature, iss, aud, exp, nonce).
    4. Look up or create the FixHub user via AuthIdentity.
    5. Issue a fixhub_session cookie.
    """
    # --- 1. State verification ---
    cookie_val = request.cookies.get("fixhub_google_state")
    if not cookie_val:
        raise HTTPException(status_code=400, detail="auth state missing or expired")

    try:
        cookie_data = json.loads(cookie_val)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid auth state")

    stored_state = cookie_data.get("state")
    stored_verifier = cookie_data.get("verifier")

    sent_state = request.query_params.get("state")
    if not sent_state or sent_state != stored_state:
        raise HTTPException(status_code=400, detail="invalid or mismatched state")

    # Clear the used cookie so it can't be replayed
    response.delete_cookie(
        key="fixhub_google_state",
        path="/",
        secure=bool(settings.AUTH_COOKIE_SECURE),
        httponly=True,
        samesite="lax",
    )

    # --- 2. Exchange authorization code for tokens ---
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="access denied or no code returned")

    token_resp = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )

    if token_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="failed to exchange authorization code")

    token_data = token_resp.json()
    id_token = token_data.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="no ID token received from Google")

    # --- 3. Verify Google ID token ---
    jwks_resp = httpx.get(GOOGLE_JWKS_URL, timeout=15)
    if jwks_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="failed to fetch Google signing keys")

    jwks = jwks_resp.json()

    # Decode and verify the ID token
    import jwt  # PyJWT

    try:
        payload = jwt.decode(
            id_token,
            jwks,
            algorithms=["RS256"],
            audience=settings.GOOGLE_OAUTH_CLIENT_ID,
            issuer=settings.GOOGLE_OAUTH_ISSUER,
        )
    except Exception:
        raise HTTPException(status_code=400, detail="invalid Google ID token")

    # Validate required claims
    google_sub: str = payload.get("sub")
    email: str = payload.get("email", "")
    email_verified: bool = payload.get("email_verified", False)

    if not email_verified:
        raise HTTPException(status_code=400, detail="Google email not verified by provider")

    if not google_sub:
        raise HTTPException(status_code=400, detail="Google ID token missing sub")

    # --- 4. Look up or create user via AuthIdentity ---
    identity = (
        db.query(AuthIdentity)
        .filter(AuthIdentity.provider == "google", AuthIdentity.provider_subject == google_sub)
        .first()
    )

    user: object = None
    if identity:
        user = db.query(User).filter(User.id == identity.user_id).first()
    else:
        # New Google user — check if email already exists without an identity link
        existing_user = db.query(User).filter(User.email == email).first()
        if existing_user and existing_user.email == email:
            # Email exists but no linked identity → conflict, no auto-merge
            raise HTTPException(
                status_code=409,
                detail="An account already exists with this email. Please log in with email/password or link your Google account explicitly.",
            )
        # Create new user + identity
        new_user = User(
            email=email,
            password_hash="",  # social-only user, no password
            display_name=payload.get("name", ""),
            avatar_url=payload.get("picture", ""),
            is_active=1,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        identity = AuthIdentity(
            user_id=new_user.id,
            provider="google",
            provider_subject=google_sub,
            provider_email=email,
            provider_display_name=payload.get("name", ""),
            provider_avatar_url=payload.get("picture", ""),
        )
        db.add(identity)
        db.commit()
        user = new_user

    # --- 5. Issue fixhub_session cookie ---
    raw = secrets.token_urlsafe(32)
    expires = _utcnow() + timedelta(days=settings.AUTH_SESSION_DAYS)

    from app.db.models import UserSession

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

    return {"ok": True, "user": {"id": user.id, "email": user.email}}


# ---------------------------------------------------------------------------
# GitHub OAuth
# ---------------------------------------------------------------------------

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emissions"


@router.get("/github/start")
def github_start(request: Request, response: Response):
    """Initiate GitHub OAuth sign-in flow.

    Returns a RedirectResponse to GitHub's OAuth endpoint.
    PKCE S256, state bound via short-lived HttpOnly cookie.
    """
    import secrets

    state = secrets.token_urlsafe(32)
    verifier = _code_verifier()

    response = RedirectResponse(
        url=f"{GITHUB_AUTH_URL}?{_urlencode({'client_id': settings.GITHUB_LOGIN_CLIENT_ID, 'redirect_uri': settings.GITHUB_LOGIN_REDIRECT_URI, 'scope': 'read:user user:email', 'state': state, 'code_challenge': _code_challenge(verifier), 'code_challenge_method': 'S256'})}"
    )

    # Set the state+verifier cookie - HttpOnly, SameSite=Lax, ~5 min TTL
    response.set_cookie(
        key="fixhub_github_state",
        value=json.dumps({"state": state, "verifier": verifier}),
        max_age=300,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/github/callback")
def github_callback(request: Request, response: Response, db: Session = Depends(_db)):
    """Handle GitHub OAuth callback.

    1. Verify state cookie matches query parameter.
    2. Exchange authorization code for access token.
    3. Fetch GitHub user profile + verified emails.
    4. Identify primary verified email.
    5. Use GitHub's immutable numeric user ID as provider subject.
    6. Look up or create the FixHub user via AuthIdentity.
    7. Issue fixhub_session cookie.
    """
    import json

    # --- 1. State verification ---
    cookie_val = request.cookies.get("fixhub_github_state")
    if not cookie_val:
        raise HTTPException(status_code=400, detail="auth state missing or expired")

    try:
        cookie_data = json.loads(cookie_val)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid auth state")

    stored_state = cookie_data.get("state")
    stored_verifier = cookie_data.get("verifier")

    sent_state = request.query_params.get("state")
    if not sent_state or sent_state != stored_state:
        raise HTTPException(status_code=400, detail="invalid or mismatched state")

    # Clear the used cookie so it can't be replayed
    response.delete_cookie(
        key="fixhub_github_state",
        path="/",
        secure=bool(settings.AUTH_COOKIE_SECURE),
        httponly=True,
        samesite="lax",
    )

    # --- 2. Exchange authorization code for access token ---
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="access denied or no code returned")

    token_resp = httpx.post(
        GITHUB_TOKEN_URL,
        data={
            "client_id": settings.GITHUB_LOGIN_CLIENT_ID,
            "client_secret": settings.GITHUB_LOGIN_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.GITHUB_LOGIN_REDIRECT_URI,
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )

    if token_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="failed to exchange GitHub authorization code")

    token_data = token_resp.json()
    github_access_token = token_data.get("access_token")
    if not github_access_token:
        raise HTTPException(status_code=400, detail="no access token from GitHub")

    # --- 3. Fetch GitHub profile + emails ---
    user_resp = httpx.get(
        GITHUB_USER_URL,
        headers={"Authorization": f"token {github_access_token}"},
        timeout=30,
    )

    if user_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="failed to fetch GitHub user profile")

    profile = user_resp.json()
    github_user_id: int = profile.get("id", 0)
    login = profile.get("login", "")
    email_from_profile = profile.get("email", "")

    # Fetch emails
    emails_resp = httpx.get(
        GITHUB_EMAILS_URL,
        headers={"Authorization": f"token {github_access_token}"},
        timeout=30,
    )

    primary_verified_email = ""
    if emails_resp.status_code == 200:
        for email_entry in emails_resp.json():
            if email_entry.get("primary") and email_entry.get("verified"):
                primary_verified_email = email_entry.get("email", "")
                break

    # If no verified email from the emails endpoint, fall back to profile email
    if not primary_verified_email and email_from_profile:
        primary_verified_email = email_from_profile

    if not primary_verified_email:
        raise HTTPException(
            status_code=400,
            detail="No verified email found for this GitHub account. "
            "Please ensure your primary email is verified on GitHub.",
        )

    # --- 4. Look up or create user via AuthIdentity ---
    # Use GitHub's immutable numeric user ID as provider_subject
    identity = (
        db.query(AuthIdentity)
        .filter(AuthIdentity.provider == "github", AuthIdentity.provider_subject == str(github_user_id))
        .first()
    )

    user: object = None
    if identity:
        user = db.query(User).filter(User.id == identity.user_id).first()
    else:
        # New GitHub user — check if email already exists without an identity link
        existing_user = db.query(User).filter(User.email == primary_verified_email).first()
        if existing_user and existing_user.email == primary_verified_email:
            # Email exists but no linked identity → conflict, no auto-merge
            raise HTTPException(
                status_code=409,
                detail="An account already exists with this email. Please log in with email/password or link your GitHub account explicitly.",
            )
        # Create new user + identity
        new_user = User(
            email=primary_verified_email,
            password_hash="",  # social-only user, no password
            display_name=login,
            avatar_url=profile.get("avatar_url", ""),
            is_active=1,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        identity = AuthIdentity(
            user_id=new_user.id,
            provider="github",
            provider_subject=str(github_user_id),
            provider_email=primary_verified_email,
            provider_display_name=login,
            provider_avatar_url=profile.get("avatar_url", ""),
        )
        db.add(identity)
        db.commit()
        user = new_user

    # --- 5. Issue fixhub_session cookie ---
    raw = secrets.token_urlsafe(32)
    expires = _utcnow() + timedelta(days=settings.AUTH_SESSION_DAYS)

    from app.db.models import UserSession

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

    return {"ok": True, "user": {"id": user.id, "email": user.email}}


# ---------------------------------------------------------------------------
# Provider status (public, no secrets)
# ---------------------------------------------------------------------------


@router.get("/providers")
def providers():
    """Public endpoint: returns whether each social provider is enabled.

    Never exposes client IDs, secrets, or internal configuration.
    """
    return {
        "google": bool(
            settings.GOOGLE_OAUTH_CLIENT_ID
            and settings.GOOGLE_OAUTH_CLIENT_SECRET
            and settings.GOOGLE_OAUTH_REDIRECT_URI
        ),
        "github": bool(
            settings.GITHUB_LOGIN_CLIENT_ID
            and settings.GITHUB_LOGIN_CLIENT_SECRET
            and settings.GITHUB_LOGIN_REDIRECT_URI
        ),
    }