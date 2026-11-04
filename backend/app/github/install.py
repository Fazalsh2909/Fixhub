"""Install-ownership fix: GitHub App user authorization + verified bindings.

GitHub's docs explicitly treat the `installation_id` GitHub appends to the
App setup URL as a spoofable hint. This module therefore refuses to accept
any browser-supplied installation/account/repo data as ownership evidence.
Instead it binds each authenticated FixHub session to GitHub's OWN statement
about that user, via the GitHub Apps web application flow:

1. GET /api/github/install/start   (logged-in FixHub session)
   -> sets a signed, session-bound, expiring cookie whose payload alone
      holds the PKCE S256 verifier (never to be sent anywhere), then 302
      to
      https://github.com/login/oauth/authorize?client_id=...&state=...
      where the URL state is an opaque, unpredictable signed payload
      that contains ONLY the nonce + binding/expiry/redirect record —
      never the verifier.
2. GitHub redirects to /api/github/install/callback?code=...&state=...
   -> the one-time code is exchanged (client_secret held server-side) for a
      user access token, identity verified via GET /user, and the set of
      installations that token can access fetched via GET /user/installations.
   -> GitHubAuthorization persists the verified immutable numeric GitHub
      user id + the Fernet-encrypted token; GitHubConnection / Repository
      rows are marked verified ONLY for installations GitHub confirmed.

Other user-facing endpoints (issues/contents/file/from-issue/published-diff)
then refuse rows whose installation_id AND exact repository full_name are
not part of GitHub's CURRENT grants for the user's verified identity: the
persisted binding is checked first, then live /user/installations and the
per-installation repository listing are re-validated (short permission
cache, failed lookups never cached) before any installation token is
minted. The App JWT stays reserved for webhook/app-level operations and
never enumerates installations for a browser.

State/CSRF design:
- cookie "fixhub_gh_state": HttpOnly, SameSite=settings, ~10 min TTL;
- HMAC-SHA256 signature keyed on a hash of FIXHUB_CREDENTIAL_ENCRYPTION_KEY
  (required on every deployed boot; dev/test set it via conftest);
- binds to the caller's opaque session token hash, so a state issued to one
  browser session is worthless in every other one;
- the OAuth URL state and the cookie are TWO separately signed payloads
  (v2), minted together at start but with different contents: the URL
  state is opaque {v,n,u,x,p}; the cookie additionally carries the PKCE
  verifier {v,n,u,x,k,p}. At the callback both must verify, be unexpired,
  bind to this session, and AGREE on their nonce (constant-time);
- server-enforced one-time use: the nonce is consumed Redis-side at the
  callback (SET key NX EX ttl; the consumed key is created ONLY at the
  callback — issuance is proven by the signed cookie, never by a stored
  key), so a replayed callback fails even across processes/workers. When
  the store is unreachable a deployed environment (ENV=prod) fails SAFE
  with a temporary 503 and never authorizes without enforcement; dev/test
  degrade to the stateless guarantees below instead;
- PKCE S256: the code_verifier lives ONLY inside the HttpOnly signed
  cookie — never in the URL (no access-log/referrer exposure), the DB,
  or any browser-visible output.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

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
    Task,
    User,
)

router = APIRouter()

STATE_COOKIE = "fixhub_gh_state"
STATE_TTL_S = 600
TOKEN_FRESH_MARGIN_S = 90  # refresh slightly before GitHub's own expiry
INSTALL_START_URL = "/api/github/install/start"


def _safe_install_url(value: str) -> str:
    """Only absolute https URLs are exposed to the browser (no javascript:)."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return ""
    return value


def app_install_url() -> str:
    """Public GitHub App install URL, or "" when it is not configured.

    The slug is operator-supplied (never guessed here). An explicit install
    URL wins; otherwise the canonical GitHub.com install page is derived.
    Both settings are public values and are the only GitHub App config sent
    to the browser (no client id/secret, key, or token).
    """
    explicit = (settings.GITHUB_APP_INSTALL_URL or "").strip()
    if explicit:
        return _safe_install_url(explicit)
    slug = (settings.GITHUB_APP_SLUG or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", slug):
        return ""
    return f"https://github.com/apps/{slug}/installations/new"


def public_config() -> dict:
    """Safe-to-expose GitHub connection settings for the web UI."""
    return {
        "install_url": app_install_url(),
        "oauth_enabled": _oauth_enabled(),
        "start_url": INSTALL_START_URL,
    }


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


def _callback_error(request: Request, *, status_code: int, content: dict):
    """Retire OAuth state; show the app's error state for browser navigations.

    The callback is reached by a top-level browser navigation. Returning raw
    JSON there leaves the user on an API page with no retry. API clients
    (Accept without text/html) keep the original status + JSON unchanged.
    """
    if "text/html" in (request.headers.get("accept") or "").lower():
        response = RedirectResponse("/?github_connect=error", status_code=302)
    else:
        response = JSONResponse(status_code=status_code, content=content)
    _clear_state_cookie(response)
    return response


# --- server-enforced one-time state nonces (Redis) ----------------------------


def _get_state_store():
    """Redis client factory for one-time state-nonce consumption.

    Deliberately a module-level function: tests swap in a Redis-compatible
    fake by patching THIS factory — the consumption logic itself is never
    stubbed, so tests exercise real nonce semantics.
    """
    import redis as _redis

    return _redis.Redis.from_url(settings.REDIS_URL, socket_timeout=3)


def _state_nonce_consume(nonce: str) -> bool | None:
    """Atomically consume a one-time OAuth state nonce (tri-state).

    The consumed key is created ONLY here, at the callback:
        SET fixhub:gh_state:{nonce} "1" NX EX STATE_TTL_S
    so exactly ONE callback per nonce can ever succeed, across processes
    and workers. There is no start-time registration: that the nonce was
    issued by FixHub is proven by the signed session-bound cookie which
    shares the nonce; that it was never consumed before is proven here.

    True  => fresh first use: the callback may proceed.
    False => nonce already consumed: replay; the callback must reject.
    None  => enforcement unavailable (Redis unreachable / import failed):
             callers must fail SAFE — a deployed environment
             (settings.ENV == "prod") returns a temporary error instead of
             silently authorizing without the one-time guarantee; dev/test
             may degrade to the stateless guarantees (expiring HMAC state,
             session binding, cookie deletion, GitHub's single-use code).
    """
    if not nonce:
        return False  # no nonce identity: freshness is unprovable
    try:
        return bool(
            _get_state_store().set(
                f"fixhub:gh_state:{nonce}", "1", nx=True, ex=STATE_TTL_S
            )
        )
    except Exception:
        return None


# --- the web application flow -------------------------------------------------


@router.get("/api/github/install/start")
def install_start(
    request: Request,
    next: str = "",
    user: User = Depends(get_current_user),
):
    """Begin GitHub user authorization for the logged-in FixHub session.

    Requires an authenticated FixHub session (401 for anonymous callers,
    instead of silently starting a GitHub round-trip). The URL state is an
    opaque signed nonce record; the PKCE verifier rides only in the HttpOnly
    signed state cookie (same nonce, verifier field, expiry, session
    binding, validated next-redirect target).
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
        "v": 2,
        "n": secrets.token_urlsafe(24),
        "u": _session_bind_hash(request),
        "x": int(utcnow().timestamp()) + STATE_TTL_S,
        "p": _safe_next(next),
    }
    # The URL state GitHub carries back and forth is opaque: NO verifier.
    state = _state_encode(payload)
    # The verifier travels only in the HttpOnly signed cookie (same nonce).
    cookie_state = _state_encode({**payload, "k": verifier})
    response = RedirectResponse(
        _authorize_url(state=state, code_challenge=challenge), status_code=302
    )
    _set_state_cookie(response, cookie_state)
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
    # Two signed payloads were minted together: the URL state (opaque, no
    # verifier) and the cookie (also carrying the PKCE verifier). Both must
    # verify, be unexpired, bind to THIS session, and agree on their nonce.
    url_data = _state_decode(state_param)
    cookie_state = (request.cookies.get(STATE_COOKIE) or "").strip()
    cookie_data = _state_decode(cookie_state)
    bind = _session_bind_hash(request)
    now_ts = int(utcnow().timestamp())
    url_ok = (
        isinstance(url_data, dict)
        and url_data.get("v") == 2
        and isinstance(url_data.get("x"), int)
        and url_data["x"] > now_ts
        and url_data.get("u") == bind
    )
    cookie_ok = (
        isinstance(cookie_data, dict)
        and cookie_data.get("v") == 2
        and isinstance(cookie_data.get("x"), int)
        and cookie_data["x"] > now_ts
        and cookie_data.get("u") == bind
        # PKCE lives ONLY in the cookie: no verifier there, no exchange.
        and isinstance(cookie_data.get("k"), str)
        and bool(cookie_data["k"])
    )
    nonce = str(url_data.get("n") or "") if url_ok else ""
    verifier = str(cookie_data["k"]) if cookie_ok else ""
    cookie_nonce = (
        str(cookie_data.get("n") or "") if isinstance(cookie_data, dict) else ""
    )
    if not (
        url_ok
        and cookie_ok
        # minted together: URL state + cookie must describe ONE flow; the
        # nonce agreement closes GitHub-side swaps and mismatched/replayed
        # pairs and is compared constant-time (both are HMAC-verified here)
        and hmac.compare_digest(nonce, cookie_nonce)
        and bool(nonce)
    ):
        response = RedirectResponse("/?github_connect=error", status_code=302)
        _clear_state_cookie(response)
        return response
    # Cheap checks passed: enforce server-side single use of the nonce.
    consumed = _state_nonce_consume(nonce)
    if consumed is False:
        # Replay of an already-used state: never re-run the exchange.
        response = RedirectResponse("/?github_connect=error", status_code=302)
        _clear_state_cookie(response)
        return response
    if consumed is None:
        if settings.ENV == "prod":
            # Fail SAFE, not silently weaker: deployed environments must
            # never authorize when one-time enforcement is unavailable.
            return _callback_error(
                request,
                status_code=503,
                content={
                    "detail": (
                        "authorization temporarily unavailable "
                        "(state store unreachable); retry shortly"
                    )
                },
            )
        # Dev/test degrade to the stateless guarantees (expiring HMAC state,
        # session binding, cookie deletion, GitHub's single-use code).
    next_path = _safe_next(str(cookie_data.get("p") or "/"))
    if not _oauth_enabled():
        return _callback_error(
            request,
            status_code=400,
            content={
                "detail": (
                    "GitHub App user authorization is not configured "
                    "(set GITHUB_APP_CLIENT_ID / GITHUB_APP_CLIENT_SECRET)"
                )
            },
        )
    try:
        from app.github import client as _gh

        token_payload = _gh.exchange_authorization_code(
            code=code,
            code_verifier=verifier,
            redirect_uri=(settings.GITHUB_APP_REDIRECT_URI or "").strip(),
        )
        access_token = str(token_payload.get("access_token", ""))
        ident = _gh.get_user_identity(token=access_token)
        installations = _gh.list_user_installations(token=access_token)
        if not ident.get("id"):
            raise ValueError("github returned no user identity")
    except Exception as exc:
        # Every failure path must retire the state cookie: a stale signed
        # state must not survive a failed exchange to be retried later.
        return _callback_error(
            request,
            status_code=502,
            content={"error": f"github connect failed: {str(exc)[:300]}"},
        )
    _persist_authorization(db, user, ident, token_payload)
    # A fresh OAuth grant is the user re-proving access: drop any cached
    # live-permission decision so everything is re-asked against GitHub.
    _clear_user_perm_cache(user.id)
    _reconcile_verified_connections(
        db, user, ident, installations, token=access_token
    )
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


# --- live permission cache (repository-level revalidation) --------------------
#
# The browser gate must re-check GitHub's CURRENT grants, but asking GitHub
# twice per request is wasteful: successful listings live in a deliberately
# SHORT cache. Its TTL is read at call time (tests shrink it to 0), a
# successful OAuth re-connect clears the user's entries immediately, and
# failures are NEVER cached — the cache cannot preserve access after the
# user's grants change by more than its own expiry.
PERM_CACHE_TTL_S = 60
_PERM_INSTALL_CACHE: dict[int, tuple[float, frozenset[str]]] = {}
_PERM_REPO_CACHE: dict[tuple[int, str], tuple[float, frozenset[str]]] = {}


def _clear_user_perm_cache(user_id: int | None = None) -> None:
    """Drop live-permission cache entries for one user (or all users)."""
    if user_id is None:
        _PERM_INSTALL_CACHE.clear()
        _PERM_REPO_CACHE.clear()
        return
    _PERM_INSTALL_CACHE.pop(user_id, None)
    for key in [k for k in _PERM_REPO_CACHE if k[0] == user_id]:
        _PERM_REPO_CACHE.pop(key, None)


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
    db: Session, user: User, ident: dict, installations: list[dict], *, token: str
) -> None:
    """Stamp verified claims ONLY where GitHub confirmed access.

    - rows already claimed by this user whose installation is now confirmed:
      verified (legacy NULL rows re-verified toward a legit owner);
    - rows for installations NOT in the confirmed set: left alone (stay
      unverified for user APIs; never deleted, webhooks still work);
    - brand-new rows are created for confirmed installations this user had
      not yet connected (GitHub-truthful, not a silent guess);
    - Repository rows owned by this user are verified ONLY when BOTH their
      installation is confirmed AND the exact repository full_name appears
      in GitHub's user-accessible listing for that installation; a listing
      failure is never treated as verification and leaves rows unverified.
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
    from app.github import client as _gh

    # Repository-level truth: exact membership in each installation's
    # user-accessible repo listing. An API failure maps to None for that
    # installation — never to "verified" — and its rows stay unverified.
    accessible: dict[str, frozenset[str] | None] = {}
    for iid in want:
        try:
            listing = _gh.list_user_installation_repos(
                token=token, installation_id=iid
            )
        except Exception:
            accessible[iid] = None
            continue
        accessible[iid] = frozenset(
            str(r.get("full_name", "")) for r in listing if r.get("full_name")
        )
    for repo in (
        db.query(Repository).filter(Repository.owner_id == user.id).all()
    ):
        if repo.installation_id not in want or (
            repo.github_user_id is not None and int(repo.github_user_id) != gh_id
        ):
            continue
        names = accessible.get(repo.installation_id)
        if not names or (repo.github_full_name or "") not in names:
            continue  # stays unverified; preserved for webhooks, never deleted
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
    """True only when GitHub CURRENTLY authorizes this user for the row.

    Guards every user-facing endpoint that mints an installation token.
    Three layers, cheapest first:

    1. persisted-binding precheck: a Repository row connected pre-fix
       (github_user_id NULL) or poisoned by the old first-claim connect bug
       (bound to another GitHub account) is refused with zero GitHub calls;
    2. live install authorization: the row's installation_id must still be
       in GitHub's /user/installations listing for this user's token —
       get_user_token refuses revoked, undecryptable, or unrefreshable
       secrets outright;
    3. live repository authorization: the row's exact github_full_name must
       appear in the installation's CURRENT user-accessible repository
       listing — membership is never inferred from the persisted row.

    Fail-closed: any GitHub API/refresh failure returns False and is never
    cached. Successful listings are cached briefly (PERM_CACHE_TTL_S is read
    at each call so tests can shrink it); _clear_user_perm_cache drops a
    user's entries on every successful OAuth re-connect, and entries expire
    on their own, so no access can outlive GitHub's statement about it.
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
    if int(row.github_user_id) != int(auth.github_user_id):
        return False

    from app.github import client as _gh

    token = get_user_token(db, user)
    user_token = (token or {}).get("token", "")
    if not user_token:
        # Revoked/expired-beyond-refresh/corrupt secret: no live proof can
        # exist, so nothing to ask GitHub for. Refuse.
        return False

    iid = str(row.installation_id)
    now = time.time()
    ttl = PERM_CACHE_TTL_S

    installs = _PERM_INSTALL_CACHE.get(user.id)
    if installs is None or installs[0] <= now:
        try:
            ids = frozenset(
                str(i.get("id", ""))
                for i in _gh.list_user_installations(token=user_token)
                if str(i.get("id", ""))
            )
            _PERM_INSTALL_CACHE[user.id] = (time.time() + ttl, ids)
        except Exception:
            _clear_user_perm_cache(user.id)  # a failure caches nothing
            return False
        installs = _PERM_INSTALL_CACHE[user.id]
    if iid not in installs[1]:
        return False

    allowed = _PERM_REPO_CACHE.get((user.id, iid))
    if allowed is None or allowed[0] <= now:
        try:
            names = frozenset(
                str(r.get("full_name", ""))
                for r in _gh.list_user_installation_repos(
                    token=user_token, installation_id=iid
                )
                if r.get("full_name")
            )
        except Exception:
            _PERM_REPO_CACHE.pop((user.id, iid), None)  # failures never cached
            return False
        _PERM_REPO_CACHE[(user.id, iid)] = (time.time() + ttl, names)
        allowed = _PERM_REPO_CACHE[(user.id, iid)]
    return str(row.github_full_name) in allowed[1]


def task_github_authorization(
    db: Session, task: Task, user: User
) -> tuple[bool, str]:
    """Current GitHub truth for a browser-originated task operation.

    Reuses installation_verified_for_user() (persisted identity binding, live
    installation membership, then exact repository membership) for every task
    bound to a GitHub App installation. Tasks with no GitHub binding have no
    installation-token path to protect and are allowed so local/self-hosted
    work (and the existing test fixtures) keep functioning.

    Fail-closed when the task claims a repository binding but the Repository
    row is gone: a formerly GitHub-backed run must never fall through to a
    local push after the repo was disconnected.
    """
    full_name = str(getattr(task, "repository", "") or "")
    row = (
        db.query(Repository)
        .filter(Repository.github_full_name == full_name)
        .first()
        if full_name
        else None
    )
    if row is None:
        if getattr(task, "repository_id", None) is not None:
            return False, "repository is no longer connected"
        return True, ""
    if not str(row.installation_id or "").strip():
        return True, ""
    if installation_verified_for_user(row, db, user):
        return True, ""
    return False, f"GitHub access to {full_name} is no longer authorized"
