"""Install-ownership fix: GitHub App user authorization + verified bindings.

GitHub's docs explicitly treat the `installation_id` GitHub appends to the
App setup URL as a spoofable hint. This module therefore refuses to accept
any browser-supplied installation/account/repo data as ownership evidence.
Instead it binds each authenticated FixHub session to GitHub's OWN statement
about that user, via the GitHub Apps web application flow:

1. GET /api/github/install/start   (logged-in FixHub session)
   -> sets a signed, session-bound, expiring, single-use state cookie
      (PKCE S256 verifier baked in), then 302 to
      https://github.com/login/oauth/authorize?client_id=...&state=...
2. GitHub redirects to /api/github/install/callback?code=...&state=...
   -> the one-time code is exchanged (client_secret held server-side) for a
      user access token, identity verified via GET /user, and the set of
      installations that token can access fetched via GET /user/installations.
   -> GitHubAuthorization persists the verified immutable numeric GitHub
      user id + the Fernet-encrypted token; GitHubConnection / Repository
      rows are marked verified ONLY for installations GitHub confirmed.

Other user-facing endpoints (issues/contents/file/connect) then verify a
row's installation_id against the *current user's* persisted verified
identity before any installation token is minted. The App JWT stays
reserved for webhook/app-level operations and never enumerates
installations for a browser.

State/CSRF design:
- cookie "fixhub_gh_state": HttpOnly, SameSite=settings, ~10 min TTL;
- HMAC-SHA256 signature keyed on a hash of FIXHUB_CREDENTIAL_ENCRYPTION_KEY
  (required on every deployed boot; dev/test set it via conftest);
- binds to the caller's opaque session token hash, so a state issued to one
  browser session is worthless in every other one;
- verbatim-return matching against the OAuth `state` query parameter
  (cookie + param must agree, compared constant-time);
- server-enforced one-time use: the state nonce is consumed Redis-side
  (SETNX + TTL) at the callback, so a replayed state cookie is rejected
  even across processes; when Redis is unavailable this degrades to the
  historical (cookie deletion + GitHub's single-use code + session
  binding + expiring HMAC) guarantees instead of blocking authorization;
- PKCE S256: the code_verifier lives only inside the signed cookie, never
  in the DB or URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import _db, get_current_user
from app.config import settings
from app.db.database import utcnow
from app.db.models import (
    GitHubAuthorization,
    GitHubConnection,
    Repository,
    User,
)

router = APIRouter()

STATE_COOKIE = "fixhub_gh_state"
STATE_TTL_S = 600
TOKEN_FRESH_MARGIN_S = 90  # refresh slightly before GitHub's own expiry
INSTALL_START_URL = "/api/github/install/start"


# --- state cookie helpers ----------------------------------------------------


def _oauth_enabled() -> bool:
    return bool(
        (settings.GITHUB_APP_CLIENT_ID or "").strip()
        and (settings.GITHUB_APP_CLIENT_SECRET or "").strip()
    )


def oauth_required_payload() -> dict:
    """JSON body extras for endpoints that need GitHub re-verification."""
    return {
        "github_authorization_required": True,
        "start_url": INSTALL_START_URL,
    }


def _state_key() -> bytes:
    """HMAC key derived from the credential master key (never logged)."""
    raw = (settings.FIXHUB_CREDENTIAL_ENCRYPTION_KEY or "").strip()
    if not raw:
        raise RuntimeError(
            "credential encryption not configured "
            "(set FIXHUB_CREDENTIAL_ENCRYPTION_KEY)"
        )
    return hashlib.sha256(("fixhub-github-oauth|" + raw).encode("utf-8")).digest()


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
    """Signature check only; expiry + session binding are separate checks."""
    try:
        blob, sig = (value or "").split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(
        _state_key(), blob.encode("ascii"), hashlib.sha256
    ).hexdigest()
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
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def _session_bind_hash(request: Request) -> str:
    """Session-binding fingerprint: SHA-256 of the current session cookie.

    Never exposes the raw token; makes a state issued to one browser session
    worthless in every other one (sessions are opaque random tokens).
    """
    raw = request.cookies.get(settings.AUTH_COOKIE_NAME, "")
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def _set_state_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        key=STATE_COOKIE,
        value=value,
        max_age=STATE_TTL_S,
        httponly=True,
        secure=bool(settings.AUTH_COOKIE_SECURE),
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )


def _clear_state_cookie(response: Response) -> None:
    response.set_cookie(
        key=STATE_COOKIE,
        value="",
        max_age=0,
        httponly=True,
        secure=bool(settings.AUTH_COOKIE_SECURE),
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )


# --- server-enforced one-time state nonces (Redis, best effort) ---------------


def _state_nonce_register(nonce: str) -> None:
    """Record an OAuth state nonce as issued (best effort, Redis-backed).

    Turns "delete the state cookie in the browser" into a server-enforced
    one-time guarantee: the callback refuses any nonce that is already
    consumed, across processes/workers. Redis unavailability degrades to
    the stateless behavior (cookie deletion + GitHub's single-use code +
    session binding + expiring HMAC) rather than blocking fresh flows.
    """
    if not nonce:
        return
    try:
        import redis as _redis

        client = _redis.Redis.from_url(settings.REDIS_URL, socket_timeout=3)
        client.setex(f"fixhub:gh_state:{nonce}", STATE_TTL_S, "1")
    except Exception:
        pass


def _state_nonce_consume(nonce: str) -> bool:
    """Atomically consume a state nonce. True => proceed, False => replay.

    SETNX with a TTL marks consumption, so exactly one callback per nonce
    succeeds even across workers. On Redis failure this evaluates True
    (fail-open): a fresh authorization must never depend on Redis liveness.
    """
    if not nonce:
        return True
    try:
        import redis as _redis

        client = _redis.Redis.from_url(settings.REDIS_URL, socket_timeout=3)
        return bool(
            client.set(f"fixhub:gh_state:{nonce}", "1", nx=True, ex=STATE_TTL_S)
        )
    except Exception:
        return True


# --- the web application flow -------------------------------------------------


@router.get("/api/github/install/start")
def install_start(
    request: Request,
    next: str = "",
    user: User = Depends(get_current_user),
):
    """Begin GitHub user authorization for the logged-in FixHub session.

    Requires an authenticated FixHub session (401 for anonymous callers,
    instead of silently starting a GitHub round-trip). The signed state
    carries a PKCE verifier, an expiry stamp, the session binding, and the
    validated next-redirect target.
    """
    if not _oauth_enabled():
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub App user authorization is not configured "
                "(set GITHUB_APP_CLIENT_ID / GITHUB_APP_CLIENT_SECRET)"
            ),
        )
    verifier = secrets.token_urlsafe(48)  # PKCE verifier (unreserved chars only)
    challenge = _b64url_encode(hashlib.sha256(verifier.encode("utf-8")).digest())
    payload = {
        "v": 1,
        "n": secrets.token_urlsafe(24),
        "u": _session_bind_hash(request),
        "x": int(utcnow().timestamp()) + STATE_TTL_S,
        "k": verifier,
        "p": _safe_next(next),
    }
    state = _state_encode(payload)
    _state_nonce_register(payload["n"])
    response = RedirectResponse(
        _authorize_url(state=state, code_challenge=challenge), status_code=302
    )
    _set_state_cookie(response, state)
    return response


@router.get("/api/github/install/callback")
def install_callback(
    request: Request,
    db: Session = Depends(_db),
    user: User = Depends(get_current_user),
):
    """GitHub's redirect target: verify state, verify identity, persist."""
    code = (request.query_params.get("code") or "").strip()
    state_param = (request.query_params.get("state") or "").strip()
    gh_error = (request.query_params.get("error") or "").strip()
    next_path = "/"
    if gh_error or not code or not state_param:
        response = RedirectResponse("/?github_connect=error", status_code=302)
        _clear_state_cookie(response)
        return response
    data = _state_decode(state_param)
    cookie_state = (request.cookies.get(STATE_COOKIE) or "").strip()
    expired = (
        data is None
        or not isinstance(data.get("x"), int)
        or data["x"] <= int(utcnow().timestamp())
    )
    unbound = data is not None and data.get("u") != _session_bind_hash(request)
    replay = data is not None and not _state_nonce_consume(
        str(data.get("n") or "")
    )
    if (
        data is None
        or expired
        or unbound
        or replay
        # double-check the returned state matches what we set (no GitHub-side
        # swap); constant-time so the match itself leaks no prefix timing
        or not hmac.compare_digest(state_param, cookie_state)
    ):
        response = RedirectResponse("/?github_connect=error", status_code=302)
        _clear_state_cookie(response)
        return response
    next_path = _safe_next(str(data.get("p") or "/"))
    if not _oauth_enabled():
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub App user authorization is not configured "
                "(set GITHUB_APP_CLIENT_ID / GITHUB_APP_CLIENT_SECRET)"
            ),
        )
    try:
        from app.github import client as _gh

        token_payload = _gh.exchange_authorization_code(
            code=code,
            code_verifier=str(data.get("k") or ""),
            redirect_uri=(settings.GITHUB_APP_REDIRECT_URI or "").strip(),
        )
        access_token = str(token_payload.get("access_token", ""))
        ident = _gh.get_user_identity(token=access_token)
        installations = _gh.list_user_installations(token=access_token)
        if not ident.get("id"):
            raise ValueError("github returned no user identity")
    except Exception as exc:
        response = JSONResponse(
            status_code=502,
            content={"error": f"github connect failed: {str(exc)[:300]}"},
        )
        # Every failure path must retire the state cookie: a stale signed
        # state must not survive a failed exchange to be retried later.
        _clear_state_cookie(response)
        return response
    _persist_authorization(db, user, ident, token_payload)
    _reconcile_verified_connections(db, user, ident, installations)
    db.commit()
    target = next_path + ("&" if "?" in next_path else "?") + "github_connect=done"
    response = RedirectResponse(target, status_code=302)
    _clear_state_cookie(response)
    return response


def _authorize_url(*, state: str, code_challenge: str) -> str:
    from urllib.parse import urlencode

    params: dict = {
        "client_id": (settings.GITHUB_APP_CLIENT_ID or "").strip(),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    redirect_uri = (settings.GITHUB_APP_REDIRECT_URI or "").strip()
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    return f"https://github.com/login/oauth/authorize?{urlencode(params)}"


# --- persistence + verification services -------------------------------------


def _aware(dt: datetime | None) -> datetime | None:
    """Naive datetimes are legacy rows: assume UTC (deps.get_current_user does the same)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _persist_authorization(
    db: Session, user: User, ident: dict, token_payload: dict
) -> GitHubAuthorization:
    """Store the verified identity + Fernet-encrypted user access token."""
    from app.llm import crypto as _crypto

    now = utcnow()
    row = (
        db.query(GitHubAuthorization)
        .filter(GitHubAuthorization.user_id == user.id)
        .first()
    )
    if row is None:
        row = GitHubAuthorization(user_id=user.id)
        db.add(row)
    row.github_user_id = int(ident["id"])
    row.github_login = (str(ident.get("login") or ""))[:255]
    row.encrypted_user_token = _crypto.encrypt_secret(
        str(token_payload.get("access_token", ""))
    )
    refresh_token = str(token_payload.get("refresh_token") or "")
    if refresh_token:
        row.encrypted_refresh_token = _crypto.encrypt_secret(refresh_token)
    row.token_expires_at = (
        now + timedelta(seconds=int(token_payload["expires_in"]))
        if token_payload.get("expires_in")
        else None
    )
    row.refresh_token_expires_at = (
        now + timedelta(seconds=int(token_payload["refresh_token_expires_in"]))
        if refresh_token and token_payload.get("refresh_token_expires_in")
        else None
    )
    row.last_verified_at = now
    return row


def _reconcile_verified_connections(
    db: Session, user: User, ident: dict, installations: list[dict]
) -> None:
    """Stamp verified claims ONLY where GitHub confirmed access.

    - rows already claimed by this user whose installation is now confirmed:
      verified (legacy NULL rows re-verified toward a legit owner);
    - rows for installations NOT in the confirmed set: left alone (stay
      unverified for user APIs; never deleted, webhooks still work);
    - brand-new rows are created for confirmed installations this user had
      not yet connected (GitHub-truthful, not a silent guess);
    - Repository rows owned by this user whose installation matches the
      confirmed set get the same verification stamp.
    """
    now = utcnow()
    gh_id = int(ident["id"])
    want = {str(i.get("id", "")): i for i in installations if str(i.get("id", ""))}
    conn_rows = (
        db.query(GitHubConnection).filter(GitHubConnection.user_id == user.id).all()
    )
    seen: set[str] = set()
    for conn in conn_rows:
        inst = want.get(conn.installation_id)
        if inst is None:
            continue
        if conn.github_user_id is None or int(conn.github_user_id) == gh_id:
            conn.github_user_id = gh_id
            conn.verified_at = now
            conn.github_account = (str(inst.get("account") or ""))[:255]
            conn.github_account_type = (str(inst.get("type") or ""))[:32]
            seen.add(conn.installation_id)
    for iid, inst in want.items():
        if iid in seen:
            continue
        db.add(
            GitHubConnection(
                user_id=user.id,
                installation_id=iid[:64],
                github_user_id=gh_id,
                github_account=(str(inst.get("account") or ""))[:255],
                github_account_type=(str(inst.get("type") or ""))[:32],
                verified_at=now,
            )
        )
    for repo in (
        db.query(Repository).filter(Repository.owner_id == user.id).all()
    ):
        if repo.installation_id in want and (
            repo.github_user_id is None or int(repo.github_user_id) == gh_id
        ):
            repo.github_user_id = gh_id
            repo.verified_at = now


def get_user_token(db: Session, user: User) -> dict | None:
    """A fresh (or refreshed) user access token for this FixHub user.

    Returns {token, github_user_id, github_login} or None when the user has
    no verified authorization, the token cannot decrypt, or (for expiring
    tokens) no unexpired refresh token exists. Never creates or trusts any
    client-supplied identity.

    Rotation safety: GitHub rotates (and invalidates) the refresh token on
    every refresh. Two workers can hit expiry together, so before any
    refresh the row is re-read under SELECT ... FOR UPDATE (a no-op on
    SQLite) and re-checked: the first worker's rotation commits, and the
    other worker's locked re-read sees the fresh token and returns it
    instead of refreshing again with the (now invalidated) RT — that race
    previously persisted a revoked token pair and locked the user out. A
    commit failure after a successful GitHub-side rotation is retried
    once; a second failure returns None (full re-authorization, never a
    half-rotated substitution).
    """
    from app.llm import crypto as _crypto
    from app.github import client as _gh

    def _decrypted_token(row_obj: GitHubAuthorization) -> str:
        try:
            return _crypto.decrypt_secret(row_obj.encrypted_user_token)
        except Exception:
            return ""

    row = (
        db.query(GitHubAuthorization)
        .filter(GitHubAuthorization.user_id == user.id)
        .first()
    )
    if row is None or not row.github_user_id:
        return None
    if not (row.encrypted_user_token or "").strip():
        return None
    token = _decrypted_token(row)
    if not token:
        return None
    expires = _aware(row.token_expires_at)
    now = datetime.now(timezone.utc)
    if expires is None:
        # Non-expiring user token (GitHub Apps may opt out of expiration).
        return {
            "token": token,
            "github_user_id": int(row.github_user_id),
            "github_login": row.github_login or "",
        }
    if expires > now + timedelta(seconds=TOKEN_FRESH_MARGIN_S):
        return {
            "token": token,
            "github_user_id": int(row.github_user_id),
            "github_login": row.github_login or "",
        }
    # --- refresh needed: serialize racing workers on the row itself ---
    if not (row.encrypted_refresh_token or "").strip():
        return None
    refresh_exp = _aware(row.refresh_token_expires_at)
    if refresh_exp is None or refresh_exp <= now:
        return None

    def _apply_rotated(target: GitHubAuthorization) -> None:
        target.encrypted_user_token = _crypto.encrypt_secret(
            str(fresh.get("access_token", ""))
        )
        if fresh.get("refresh_token"):
            target.encrypted_refresh_token = _crypto.encrypt_secret(
                str(fresh["refresh_token"])
            )
        target.token_expires_at = (
            now + timedelta(seconds=int(fresh["expires_in"]))
            if fresh.get("expires_in")
            else None
        )
        if fresh.get("refresh_token") and fresh.get("refresh_token_expires_in"):
            target.refresh_token_expires_at = now + timedelta(
                seconds=int(fresh["refresh_token_expires_in"])
            )

    # Re-read under a row lock (SQLite: a no-op) and double-check: another
    # worker's refresh may have committed between our read and this lock.
    # Honoring a fresh token beats burning the rotated refresh token.
    locked = (
        db.query(GitHubAuthorization)
        .filter(GitHubAuthorization.user_id == user.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        return None
    locked_exp = _aware(locked.token_expires_at)
    current = _decrypted_token(locked) if locked.github_user_id else ""
    if current and (
        locked_exp is None
        or locked_exp
        > datetime.now(timezone.utc) + timedelta(seconds=TOKEN_FRESH_MARGIN_S)
    ):
        # A concurrent refresh already produced a valid token: use it and
        # release the row lock while keeping the request's transaction.
        db.commit()
        return {
            "token": current,
            "github_user_id": int(locked.github_user_id),
            "github_login": locked.github_login or "",
        }
    try:
        raw_refresh = _crypto.decrypt_secret(locked.encrypted_refresh_token)
        fresh = _gh.refresh_user_access_token(refresh_token=raw_refresh)
    except Exception:
        # Failed refresh wrote nothing; a genuinely dead RT reauthorizes.
        return None
    try:
        _apply_rotated(locked)
        db.commit()
    except Exception:
        # GitHub already consumed the old refresh token, so a lost commit
        # would otherwise force a full re-connect. Retry once on a fresh
        # transaction; a second failure gives up cleanly.
        db.rollback()
        try:
            retry = (
                db.query(GitHubAuthorization)
                .filter(GitHubAuthorization.user_id == user.id)
                .with_for_update()
                .first()
            )
            if retry is None:
                return None
            _apply_rotated(retry)
            db.commit()
        except Exception:
            db.rollback()
            return None
    return {
        "token": str(fresh.get("access_token", "")),
        "github_user_id": int(locked.github_user_id),
        "github_login": locked.github_login or "",
    }


def installation_verified_for_user(
    row: Repository | None, db: Session, user: User
) -> bool:
    """True only when the row's installation is bound to this user's verified identity.

    Guards every user-facing endpoint that mints an installation token: a
    Repository row connected pre-fix (github_user_id NULL) or poisoned by the
    old first-claim connect bug (bound to another GitHub account) is refused
    here, even when the FixHub row itself is owned by the requester.
    """
    if row is None or not row.installation_id:
        return False
    auth = (
        db.query(GitHubAuthorization)
        .filter(
            GitHubAuthorization.user_id == user.id,
            GitHubAuthorization.github_user_id.isnot(None),
        )
        .first()
    )
    if auth is None or row.github_user_id is None:
        return False
    return int(row.github_user_id) == int(auth.github_user_id)
