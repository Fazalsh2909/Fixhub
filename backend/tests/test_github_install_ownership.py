"""Install-ownership fix regression tests (GitHub App user authorization).

Contract under test:
- The GitHub App JWT NEVER enumerates installations for a browser; each
  FixHub user sees only the installations GitHub confirmed for THEM via
  /user/installations (web application flow, verified server-side).
- A client-supplied installation_id is untrusted: connect requires a
  verified identity + GitHub membership proof for installation AND repo.
- User A cannot see/claim/use anything belonging to user B's GitHub.
- Legacy (pre-fix) rows are never silently assigned; they are verified only
  when GitHub itself confirms access during the OAuth reconcile.
- One-time state usage is enforced SERVER-side (Redis SETNX at the callback;
  tests use a Redis-compatible fake — the consumption logic is never
  stubbed). The PKCE verifier lives ONLY in the signed HttpOnly state
  cookie: the authorization URL state is opaque and never carries it.
- Unauthenticated requests stay rejected.
"""

import hashlib
import hmac
import json
import threading
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

import app.github.install as _install
from app.config import settings
from app.db.models import GitHubAuthorization, GitHubConnection, Repository, Task
from app.main import app
from tests.conftest import make_user, session_cookies


def _enable_oauth(monkeypatch):
    monkeypatch.setattr(settings, "GITHUB_APP_CLIENT_ID", "Iv1.t45t")
    monkeypatch.setattr(settings, "GITHUB_APP_CLIENT_SECRET", "t45t-5ecret")


def _stub_github(
    monkeypatch,
    *,
    token: str,
    gh_id: int,
    login: str,
    installations: list[dict],
    repos_by_installation: dict | None = None,
    exchange_record: list | None = None,
):
    """Deterministic stand-in for github.com (web flow + user token APIs).

    A per-test registry (carried on the monkeypatch so it dies with the
    test) lets SEVERAL users be stubbed in one test: each user token maps
    to its own identity + installation set, mirroring GitHub's per-user
    authorization. Tokens with no registry entry are genuinely unknown to
    GitHub: /user/installations and the per-installation repo listing
    return [] for them (fixhub then refuses connects), and identity probes
    with an unknown token raise (GitHub 401-equivalent).
    """
    from app.github import client as _gh

    registry = getattr(monkeypatch, "_fixhub_gh_registry", None)
    if registry is None:
        registry = {"tokens": {}, "repos": {}, "pending": token}
        monkeypatch._fixhub_gh_registry = registry
    registry["pending"] = token
    registry["tokens"][token] = {
        "gh_id": gh_id,
        "login": login,
        "installations": list(installations),
    }
    for iid, repo_list in (repos_by_installation or {}).items():
        registry["repos"][str(iid)] = list(repo_list)

    def _exchange(*, code, code_verifier="", redirect_uri=""):
        if exchange_record is not None:
            # Record the verifier VALUE (not just presence): replay/PKCE
            # tests assert the exchanged verifier equals the cookie one.
            exchange_record.append(
                {
                    "code": code,
                    "verifier_present": bool(code_verifier),
                    "verifier": code_verifier,
                }
            )
        return {
            "access_token": registry["pending"],
            "refresh_token": "ghr-" + str(registry["pending"]),
            "expires_in": 600,
            "refresh_token_expires_in": 3600,
            "token_type": "bearer",
        }

    def _identity(*, token: str):
        info = registry["tokens"].get(token)
        if info is None:
            raise AssertionError("identity fetched with a foreign user token")
        return {"id": info["gh_id"], "login": info["login"]}

    def _installations(*, token: str):
        # GitHub returns installations only for a token it knows; an
        # unknown user token sees nothing (fixhub then denies connects).
        info = registry["tokens"].get(token)
        return list(info["installations"]) if info else []

    def _repos(*, token: str, installation_id):
        if token not in registry["tokens"]:
            return []
        return list(registry["repos"].get(str(installation_id), []))

    monkeypatch.setattr(_gh, "exchange_authorization_code", _exchange)
    monkeypatch.setattr(_gh, "get_user_identity", _identity)
    monkeypatch.setattr(_gh, "list_user_installations", _installations)
    monkeypatch.setattr(_gh, "list_user_installation_repos", _repos)


def _fail_no_mint(monkeypatch):
    """Any installation-token mint request from these paths must fail the test."""
    from app.github import app_auth as _auth

    def _boom(iid):
        raise AssertionError(f"installation token minted for {iid!r}")

    monkeypatch.setattr(_auth, "installation_token", _boom)


def _drive_authorization(monkeypatch, c, cookies, *, code="code-ok", **stub_kwargs):
    """Full start->callback round trip with explicit cookie transport."""
    _enable_oauth(monkeypatch)
    _stub_github(monkeypatch, **stub_kwargs)
    r = c.get(
        "/api/github/install/start",
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?"), location
    q = parse_qs(urlparse(location).query)
    assert q["code_challenge_method"] == ["S256"]  # PKCE per GitHub docs
    state = q["state"][0]
    # GitHub redirects back with ?code=...&state=...; pass the session cookie
    # plus the state cookie returned by the start response (explicit, no jar
    # dependence).
    state_cookie = r.cookies.get(_install.STATE_COOKIE)
    assert state_cookie, "start response must set the signed state cookie"
    r2 = c.get(
        f"/api/github/install/callback?code={code}&state={state}",
        cookies={**cookies, _install.STATE_COOKIE: state_cookie},
        follow_redirects=False,
    )
    return r2


def test_full_flow_binds_identity_persists_encoded_token_and_connects(
    db, monkeypatch
):
    _enable_oauth(monkeypatch)
    exchange_seen: list = []
    _stub_github(
        monkeypatch,
        token="tok-ghu-A",
        gh_id=501,
        login="acme-user",
        installations=[
            {"id": "i1", "account": "acme", "type": "User"},
            {"id": "i2", "account": "acme-org", "type": "Organization"},
        ],
        repos_by_installation={
            "i1": [
                {"full_name": "acme/app", "private": True, "default_branch": "main"}
            ],
            "i2": [
                {"full_name": "acme-org/lib", "private": False, "default_branch": "trunk"}
            ],
        },
        exchange_record=exchange_seen,
    )
    u = make_user(db, email="flow@example.com")
    docs = {"cs": session_cookies(db, u)}
    c = TestClient(app)
    r = _drive_authorization(
        monkeypatch,
        c,
        docs["cs"],
        token="tok-ghu-A",
        gh_id=501,
        login="acme-user",
        installations=[
            {"id": "i1", "account": "acme", "type": "User"},
            {"id": "i2", "account": "acme-org", "type": "Organization"},
        ],
        repos_by_installation={
            "i1": [
                {"full_name": "acme/app", "private": True, "default_branch": "main"}
            ],
            "i2": [
                {"full_name": "acme-org/lib", "private": False, "default_branch": "trunk"}
            ],
        },
        exchange_record=exchange_seen,
    )
    assert r.status_code == 302, r.text
    assert r.headers["location"].endswith("github_connect=done")
    row = (
        db.query(GitHubAuthorization)
        .filter(GitHubAuthorization.user_id == u.id)
        .first()
    )
    assert row is not None and row.github_user_id == 501 and row.github_login == "acme-user"
    from app.llm import crypto as _crypto

    assert _crypto.decrypt_secret(row.encrypted_user_token) == "tok-ghu-A"
    assert "tok-ghu-A" not in (row.encrypted_user_token or "")  # never plaintext
    assert exchange_seen and exchange_seen[0]["verifier_present"] is True  # PKCE
    # OAuth reconcile created verified connections for BOTH confirmed installs.
    installs = c.get("/api/github/installations", cookies=docs["cs"]).json()
    assert {i["id"] for i in installs} == {"i1", "i2"}
    by_id = {i["id"]: i for i in installs}
    assert by_id["i1"]["account"] == "acme"
    assert by_id["i2"]["type"] == "Organization"
    # Legitimate connection now works for each installation (two in one user).
    for full_name, iid in (("acme/app", "i1"), ("acme-org/lib", "i2")):
        conn = c.post(
            "/api/repositories/connect",
            json={"github_full_name": full_name, "installation_id": iid},
            cookies=docs["cs"],
        )
        assert conn.status_code == 200, conn.text
    repos = [
        x["github_full_name"] for x in c.get("/api/repositories", cookies=docs["cs"]).json()
    ]
    assert set(repos) == {"acme/app", "acme-org/lib"}
    both = c.get("/api/github/installations", cookies=docs["cs"]).json()
    assert len(both) == 2


def test_user_a_never_sees_user_b_installations_or_private_repos(db, monkeypatch):
    # A's verified GitHub world: acme with a PRIVATE repo.
    u = make_user(db, email="iso-a@example.com")
    ca = session_cookies(db, u)
    c = TestClient(app)
    _drive_authorization(
        monkeypatch,
        c,
        ca,
        token="tok-ghu-a",
        gh_id=601,
        login="alice",
        installations=[{"id": "ia", "account": "alice-org", "type": "User"}],
        repos_by_installation={
            "ia": [{"full_name": "alice-org/private-app", "private": True, "default_branch": "main"}]
        },
    )
    # B's verified GitHub world: bob with a different private install.
    ub = make_user(db, email="iso-b@example.com")
    cb = session_cookies(db, ub)
    _drive_authorization(
        monkeypatch,
        c,
        cb,
        token="tok-ghu-b",
        gh_id=602,
        login="bob",
        installations=[{"id": "ib", "account": "bob-org", "type": "User"}],
        repos_by_installation={
            "ib": [
                {"full_name": "bob-org/secret-repo", "private": True, "default_branch": "main"},
                {"full_name": "bob-org/public-repo", "private": False, "default_branch": "main"},
            ]
        },
    )
    # A's listing contains ONLY A's installation + repos; B's private data
    # never shows up (the user-token API refuses foreign tokens structurally).
    listing = c.get("/api/github/repos", cookies=ca).json()
    ids = [g["installation_id"] for g in listing]
    assert ids == ["ia"] and "ib" not in ids
    bodies = " ".join(x["github_full_name"] for g in listing for x in g["repos"]).strip()
    assert "alice-org/private-app" in bodies
    assert "bob-org/secret-repo" not in bodies and "bob-org/public-repo" not in bodies
    # Installations endpoint: A never sees bob's installation ids/accounts.
    ach = c.get("/api/github/installations", cookies=ca).json()
    assert [i["id"] for i in ach] == ["ia"]
    assert all("bob" not in (i.get("account") or "") for i in ach)


def test_cross_user_spoofed_installation_connect_rejected(db, monkeypatch):
    """A cannot connect B's installation by changing installation_id."""
    u = make_user(db, email="spoof-a@example.com")
    ca = session_cookies(db, u)
    ub = make_user(db, email="spoof-b@example.com")
    cb = session_cookies(db, ub)
    c = TestClient(app)
    _drive_authorization(
        monkeypatch,
        c,
        ca,
        token="tok-ghu-a",
        gh_id=611,
        login="alice2",
        installations=[{"id": "inst-a", "account": "alice2", "type": "User"}],
        repos_by_installation={"inst-a": [{"full_name": "alice2/her-repo", "private": False, "default_branch": "main"}]},
    )
    _drive_authorization(
        monkeypatch,
        c,
        cb,
        token="tok-ghu-b",
        gh_id=612,
        login="bob2",
        installations=[{"id": "inst-b", "account": "bob2", "type": "User"}],
        repos_by_installation={"inst-b": [{"full_name": "bob2/repo", "private": True, "default_branch": "main"}]},
    )
    cb_repo = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "bob2/repo", "installation_id": "inst-b"},
        cookies=cb,
    )
    assert cb_repo.status_code == 200, cb_repo.text
    # A tries to connect bob's repo through bob's installation id: 404, no rows.
    r = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "bob2/repo", "installation_id": "inst-b"},
        cookies=ca,
    )
    assert r.status_code == 404, r.text
    # ... and A cannot attach her own repo name to bob's installation id.
    r2 = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "alice2/her-repo", "installation_id": "inst-b"},
        cookies=ca,
    )
    assert r2.status_code == 404, r2.text
    row = db.query(Repository).filter(Repository.github_full_name == "bob2/repo").first()
    assert row.owner_id == ub.id


def test_access_through_foreign_installation_refused_without_token_mint(
    db, monkeypatch
):
    """A poisoned Repository row (bound to B's GitHub identity) cannot be used
    to mint an installation token through A's session."""
    u = make_user(db, email="poison-owner@example.com")
    ca = session_cookies(db, u)
    c = TestClient(app)
    _drive_authorization(
        monkeypatch,
        c,
        ca,
        token="tok-ghu-a",
        gh_id=621,
        login="alice3",
        installations=[{"id": "inst-a3", "account": "alice3", "type": "User"}],
        repos_by_installation={"inst-a3": []},
    )
    _fail_no_mint(monkeypatch)
    # Simulate the pre-fix poisoning bug in the DB directly.
    db.add(
        Repository(
            github_full_name="victim/private",
            installation_id="inst-b3",
            owner_id=u.id,
            github_user_id=None,
        )
    )
    db.commit()
    for url in (
        "/api/github/issues?repo=victim/private",
        "/api/github/contents?repo=victim/private",
        "/api/github/file?repo=victim/private&path=x.py",
    ):
        r = c.get(url, cookies=ca)
        assert r.status_code == 400, (url, r.text)
        assert r.json()["github_authorization_required"] is True
    # A row bound to a DIFFERENT GitHub account than the verified identity is
    # refused the same way (no token mint, no cross-account reach).
    poisoned2 = Repository(
        github_full_name="victim/private2",
        installation_id="inst-b3",
        owner_id=u.id,
        github_user_id=999999,
    )
    db.add(poisoned2)
    db.commit()
    r = c.get("/api/github/issues?repo=victim/private2", cookies=ca)
    assert r.status_code == 400, r.text


def test_legacy_rows_not_silently_assigned_but_reconciled_by_github(db, monkeypatch):
    """Pre-fix rows stay unusable for the browser API until GitHub confirms
    access during the OAuth flow; non-confirmed rows stay unverified."""
    u = make_user(db, email="legacy-owner@example.com")
    db.add(GitHubConnection(user_id=u.id, installation_id="legacy-1"))  # github_user_id NULL
    db.add(GitHubConnection(user_id=u.id, installation_id="legacy-9"))
    db.add(
        Repository(
            github_full_name="legacy/app",
            installation_id="legacy-1",
            owner_id=u.id,
            github_user_id=None,
        )
    )
    db.add(
        Repository(
            github_full_name="legacy/other",
            installation_id="legacy-9",
            owner_id=u.id,
            github_user_id=None,
        )
    )
    db.commit()
    docs = {"cs": session_cookies(db, u)}
    c = TestClient(app)
    # Before the OAuth flow: nothing verified must surface.
    assert c.get("/api/github/installations", cookies=docs["cs"]).json() == []
    r = _drive_authorization(
        monkeypatch,
        c,
        docs["cs"],
        token="tok-ghu-l",
        gh_id=631,
        login="legacy-user",
        installations=[{"id": "legacy-1", "account": "legacy-user", "type": "User"}],
        repos_by_installation={
            "legacy-1": [{"full_name": "legacy/app", "private": False, "default_branch": "main"}]
        },
    )
    assert r.status_code == 302, r.text
    # GitHub confirmed legacy-1 only: that one is verified now.
    ids = [i["id"] for i in c.get("/api/github/installations", cookies=docs["cs"]).json()]
    assert ids == ["legacy-1"]
    legacy1 = db.query(Repository).filter(Repository.github_full_name == "legacy/app").first()
    assert legacy1.github_user_id == 631
    # legacy-9 was NOT confirmed: still unverified and refused (with no mint).
    _fail_no_mint(monkeypatch)
    r9 = c.get("/api/github/issues?repo=legacy/other", cookies=docs["cs"])
    assert r9.status_code == 400, r9.text
    legacy9 = db.query(Repository).filter(Repository.github_full_name == "legacy/other").first()
    assert legacy9.github_user_id is None
    # The verified one works end-to-end through A's session (token mint ok).
    from app.github import app_auth as _auth
    from app.github import client as _gh

    monkeypatch.setattr(_auth, "installation_token", lambda iid: "ok-token")
    monkeypatch.setattr(
        _gh,
        "list_issues",
        lambda *, token, full_name, state="open", limit=50: [
            {"number": 1, "title": "t", "body": "b", "url": "", "labels": []}
        ]
        if full_name == "legacy/app"
        else [],
    )
    r1 = c.get("/api/github/issues?repo=legacy/app", cookies=docs["cs"])
    assert r1.status_code == 200, r1.text
    assert r1.json()[0]["number"] == 1


def test_forged_or_missing_state_cookie_rejected(db, monkeypatch):
    _enable_oauth(monkeypatch)
    _stub_github(
        monkeypatch,
        token="tok-ghu-x",
        gh_id=641,
        login="attacker",
        installations=[{"id": "ix", "account": "attacker", "type": "User"}],
    )
    u = make_user(db, email="state0@example.com")
    docs = {"cs": session_cookies(db, u)}
    c = TestClient(app)
    # Forged state parameter (no matching cookie) -> error redirect, no persist.
    r = c.get(
        "/api/github/install/callback?code=code-ok&state=forged.value",
        cookies=docs["cs"],
        follow_redirects=False,
    )
    assert r.status_code == 302 and "github_connect=error" in r.headers["location"]
    assert (
        db.query(GitHubAuthorization).filter(GitHubAuthorization.user_id == u.id).first()
        is None
    )
    # GitHub-side error (user denied): same handling.
    r2 = c.get(
        "/api/github/install/start", cookies=docs["cs"], follow_redirects=False
    )
    state2 = parse_qs(urlparse(r2.headers["location"]).query)["state"][0]
    r3 = c.get(
        "/api/github/install/callback?error=access_denied&state=" + state2,
        cookies=docs["cs"],
        follow_redirects=False,
    )
    assert r3.status_code == 302 and "github_connect=error" in r3.headers["location"]
    assert (
        db.query(GitHubAuthorization).filter(GitHubAuthorization.user_id == u.id).first()
        is None
    )


def test_unauthenticated_requests_rejected(db, monkeypatch):
    _enable_oauth(monkeypatch)
    c = TestClient(app)
    assert c.get("/api/github/installations").status_code == 401
    assert c.get("/api/github/repos").status_code == 401
    assert c.get("/api/github/install/start").status_code == 401
    assert (
        c.get("/api/github/install/callback?code=x&state=y").status_code == 401
    )
    assert c.post("/api/repositories/connect", json={}).status_code == 401


# --- one-time state + PKCE: Redis-compatible fake, REAL consume semantics -----
#
# ONLY the store factory is patched (_get_state_store): replay, concurrency
# and fail-safe tests all run install.py's real _state_nonce_consume logic.


class _FakeRedis:
    """Thread-safe Redis-compatible stand-in for the state-nonce store.

    Implements the exact SET surface install.py uses, with Redis semantics:
    `SET key val NX EX` returns "OK" only when the key was ABSENT (creation)
    and None when it existed; a module-level lock makes the NX decision
    atomic across threads, mirroring Redis's single-threaded execution.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}

    def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        with self._lock:
            if nx:
                if key in self._data:
                    return None  # NX on an existing key: no creation (Redis)
                self._data[key] = value
                return "OK"
            self._data[key] = value
            return "OK"

    def setex(self, key: str, seconds: int, value: str):
        with self._lock:
            self._data[key] = value
            return "OK"

    def exists(self, key: str) -> int:
        with self._lock:
            return int(key in self._data)


def _fake_store(monkeypatch, *, fail: bool = False) -> _FakeRedis:
    """Patch install._get_state_store to a deterministic fake store."""
    fake = _FakeRedis()
    if fail:

        def _broken():
            raise RuntimeError("redis unreachable")

        monkeypatch.setattr(_install, "_get_state_store", _broken)
    else:
        monkeypatch.setattr(_install, "_get_state_store", lambda: fake)
    return fake


def _start_flow(c, cookies) -> dict:
    """Run install/start and return its artifacts for granular assertions."""
    r = c.get("/api/github/install/start", cookies=cookies, follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    return {
        "location": r.headers["location"],
        "query": q,
        "state": q["state"][0],
        "cookie": r.cookies.get(_install.STATE_COOKIE),
    }


def _callback(c, cookies, state_cookie, state, code="code-ok"):
    """Hit install/callback with an explicit state cookie (no jar reliance)."""
    return c.get(
        f"/api/github/install/callback?code={code}&state={state}",
        cookies={**cookies, _install.STATE_COOKIE: state_cookie},
        follow_redirects=False,
    )


def test_first_valid_callback_succeeds(db, monkeypatch):
    """Allowed (one-time state): a fresh nonce + matching cookie complete."""
    fake = _fake_store(monkeypatch)
    _enable_oauth(monkeypatch)
    exchange_seen: list = []
    _stub_github(
        monkeypatch,
        token="tok-ghu-fresh",
        gh_id=661,
        login="fresh-user",
        installations=[{"id": "inst-f", "account": "fresh-user", "type": "User"}],
        exchange_record=exchange_seen,
    )
    u = make_user(db, email="fresh@example.com")
    cookies = session_cookies(db, u)
    c = TestClient(app)
    flow = _start_flow(c, cookies)
    assert flow["cookie"], "start must set the signed state cookie"
    r = _callback(c, cookies, flow["cookie"], flow["state"])
    assert r.status_code == 302, r.text
    assert r.headers["location"].endswith("github_connect=done")
    assert len(exchange_seen) == 1
    row = (
        db.query(GitHubAuthorization)
        .filter(GitHubAuthorization.user_id == u.id)
        .first()
    )
    assert row is not None and row.github_user_id == 661
    # the fake store holds exactly the one consumed nonce (created at callback)
    key = f"fixhub:gh_state:{_install._state_decode(flow['state'])['n']}"
    assert list(fake._data) == [key]


def test_consumed_state_nonce_replay_rejected(db, monkeypatch):
    """The SECOND callback with the exact same state + cookie fails.

    Runs the REAL one-time consumption logic against the compatible fake
    (only the store factory is patched — _state_nonce_consume itself is
    never stubbed). The stubbed exchange accepts ANY code, so only the
    server-side consumed-nonce check can stop the replayed callback.
    """
    _fake_store(monkeypatch)
    _enable_oauth(monkeypatch)
    exchange_seen: list = []
    _stub_github(
        monkeypatch,
        token="tok-ghu-r",
        gh_id=651,
        login="replay-user",
        installations=[{"id": "inst-r", "account": "replay-user", "type": "User"}],
        exchange_record=exchange_seen,
    )
    u = make_user(db, email="replay@example.com")
    cookies = session_cookies(db, u)
    c = TestClient(app)
    flow = _start_flow(c, cookies)
    r_ok = _callback(c, cookies, flow["cookie"], flow["state"], code="code-1")
    assert r_ok.status_code == 302, r_ok.text
    assert r_ok.headers["location"].endswith("github_connect=done")
    r_replay = _callback(c, cookies, flow["cookie"], flow["state"], code="code-2")
    assert r_replay.status_code == 302
    assert r_replay.headers["location"].endswith("github_connect=error")
    assert len(exchange_seen) == 1  # the code exchange was never re-run
    assert (
        db.query(GitHubAuthorization)
        .filter(GitHubAuthorization.user_id == u.id)
        .count()
        == 1
    )


def test_concurrent_callbacks_cannot_both_consume_same_nonce(db, monkeypatch):
    """Two threads racing ONE nonce: exactly one consumption wins."""
    _fake_store(monkeypatch)
    barrier = threading.Barrier(2)
    results: list = []

    def worker():
        barrier.wait()
        results.append(_install._state_nonce_consume("concurrent-nonce"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1, results  # single atomic creation
    assert results.count(False) == 1, results  # the loser sees NX refusal
    # a later caller is still refused: the nonce stays consumed
    assert _install._state_nonce_consume("concurrent-nonce") is False


def test_redis_failure_fails_safe_in_prod_degrades_in_dev(db, monkeypatch):
    """A down store must NOT silently allow production authorization.

    ENV=prod -> temporary 503, exchange never runs, nothing persists, and
    the state cookie is cleared. ENV=dev -> the documented degrade-to-
    stateless behavior (historical guarantees) may proceed.
    """
    _fake_store(monkeypatch, fail=True)
    _enable_oauth(monkeypatch)
    exchange_seen: list = []
    _stub_github(
        monkeypatch,
        token="tok-ghu-nd",
        gh_id=671,
        login="no-redis-user",
        installations=[
            {"id": "inst-nd", "account": "no-redis-user", "type": "User"}
        ],
        exchange_record=exchange_seen,
    )
    u = make_user(db, email="noredis@example.com")
    cookies = session_cookies(db, u)
    c = TestClient(app)
    flow = _start_flow(c, cookies)
    monkeypatch.setattr(settings, "ENV", "prod")
    r = _callback(c, cookies, flow["cookie"], flow["state"])
    assert r.status_code == 503, r.text
    cleared = (r.headers.get("set-cookie") or "").lower()
    assert "fixhub_gh_state=" in cleared
    assert "max-age=0" in cleared
    assert exchange_seen == []  # never reached the token exchange
    assert (
        db.query(GitHubAuthorization)
        .filter(GitHubAuthorization.user_id == u.id)
        .first()
        is None
    )
    # Dev/test companion: the documented allow-degradation still works.
    monkeypatch.setattr(settings, "ENV", "dev")
    r2 = _callback(c, cookies, flow["cookie"], flow["state"], code="code-dev")
    assert r2.status_code == 302, r2.text
    assert r2.headers["location"].endswith("github_connect=done")
    assert len(exchange_seen) == 1


def test_forged_state_and_mismatched_signed_pair_rejected(db, monkeypatch):
    """Forged state — and a real-but-mismatched state/cookie pair — fail.

    (b) is stronger than a plain forge: BOTH payloads bear valid FixHub
    signatures but come from two different start flows, so only the
    nonce-agreement check can reject the pairing.
    """
    _fake_store(monkeypatch)
    _enable_oauth(monkeypatch)
    _stub_github(
        monkeypatch,
        token="tok-ghu-fg",
        gh_id=681,
        login="forger",
        installations=[{"id": "inst-fg", "account": "forger", "type": "User"}],
    )
    u = make_user(db, email="forge@example.com")
    cookies = session_cookies(db, u)
    c = TestClient(app)
    # (a) a plain forged state parameter (invalid signature): rejected.
    r = _callback(c, cookies, "forged.value", "forged.state")
    assert r.status_code == 302
    assert r.headers["location"].endswith("github_connect=error")
    # (b) real signature A + real signature B from different flows: rejected.
    flow_a = _start_flow(c, cookies)
    flow_b = _start_flow(c, cookies)
    assert flow_a["cookie"] and flow_b["cookie"]
    r2 = _callback(c, cookies, flow_b["cookie"], flow_a["state"])
    assert r2.status_code == 302
    assert r2.headers["location"].endswith("github_connect=error")
    assert (
        db.query(GitHubAuthorization)
        .filter(GitHubAuthorization.user_id == u.id)
        .first()
        is None
    )


def test_callback_from_another_fixhub_session_fails(db, monkeypatch):
    """A state issued to session A is worthless when replayed by session B.

    The state payload binds to the SHA-256 of the issued session token, so
    B's callback (different session cookie) must be rejected BEFORE any
    token exchange or persistence.
    """
    _fake_store(monkeypatch)
    _enable_oauth(monkeypatch)
    exchange_seen: list = []
    _stub_github(
        monkeypatch,
        token="tok-ghu-victim",
        gh_id=691,
        login="owner-a",
        installations=[{"id": "inst-a", "account": "owner-a", "type": "User"}],
        exchange_record=exchange_seen,
    )
    ua = make_user(db, email="sess-a@example.com")
    ub = make_user(db, email="sess-b@example.com")
    cookies_a = session_cookies(db, ua)
    cookies_b = session_cookies(db, ub)
    c = TestClient(app)
    flow = _start_flow(c, cookies_a)
    foreign = _callback(c, cookies_b, flow["cookie"], flow["state"])
    assert foreign.status_code == 302
    assert foreign.headers["location"].endswith("github_connect=error")
    assert exchange_seen == []  # no exchange was attempted
    for target in (ua, ub):
        assert (
            db.query(GitHubAuthorization)
            .filter(GitHubAuthorization.user_id == target.id)
            .first()
            is None
        )


def test_pkce_verifier_absent_from_authorize_url(db, monkeypatch):
    """The authorization URL must never carry the PKCE verifier.

    The URL state decodes to the opaque v2 record {v,n,u,x,p} — no `k` —
    and the verifier value appears nowhere in the Location header (which
    is what lands in logs/referrers).
    """
    _fake_store(monkeypatch)
    _enable_oauth(monkeypatch)
    _stub_github(
        monkeypatch,
        token="tok-ghu-url",
        gh_id=701,
        login="url-user",
        installations=[],
    )
    u = make_user(db, email="urlck@example.com")
    cookies = session_cookies(db, u)
    c = TestClient(app)
    flow = _start_flow(c, cookies)
    url_payload = _install._state_decode(flow["state"])
    assert isinstance(url_payload, dict)
    assert set(url_payload) == {"v", "n", "u", "x", "p"}  # NO verifier field
    # the ONLY copy of the verifier is in the HttpOnly state cookie
    cookie_payload = _install._state_decode(flow["cookie"])
    assert isinstance(cookie_payload, dict)
    verifier = cookie_payload["k"]
    assert isinstance(verifier, str) and verifier
    assert verifier not in flow["location"]
    assert "code_verifier" not in flow["query"]
    # PKCE S256 survives: the URL challenge is S256(verifier)
    assert flow["query"]["code_challenge_method"] == ["S256"]
    assert flow["query"]["code_challenge"][0] == _install._b64url_encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    )


def test_exchange_verifier_matches_cookie_and_url_challenge(db, monkeypatch):
    """The verifier handed to the token exchange is EXACTLY the original.

    "Original" = the value minted at start, recoverable only from the
    signed cookie; the exchanged verifier must equal it and S256-derive
    to the challenge published in the URL.
    """
    _fake_store(monkeypatch)
    _enable_oauth(monkeypatch)
    exchange_seen: list = []
    _stub_github(
        monkeypatch,
        token="tok-ghu-pkce",
        gh_id=711,
        login="pkce-user",
        installations=[],
        exchange_record=exchange_seen,
    )
    u = make_user(db, email="pkce@example.com")
    cookies = session_cookies(db, u)
    c = TestClient(app)
    flow = _start_flow(c, cookies)
    r = _callback(c, cookies, flow["cookie"], flow["state"])
    assert r.status_code == 302, r.text
    assert r.headers["location"].endswith("github_connect=done")
    verifier = _install._state_decode(flow["cookie"])["k"]
    assert len(exchange_seen) == 1
    assert exchange_seen[0]["verifier"] == verifier  # exact value equality
    assert flow["query"]["code_challenge"][0] == _install._b64url_encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    )


def test_failed_exchange_clears_state_cookie(db, monkeypatch):
    """The 502 exchange-failure path must not leave a usable state cookie."""
    _fake_store(monkeypatch)
    _enable_oauth(monkeypatch)
    _stub_github(
        monkeypatch,
        token="tok-ghu-502",
        gh_id=652,
        login="user-502",
        installations=[],
    )
    from app.github import client as _gh

    def _exchange_boom(*args, **kwargs):
        raise RuntimeError("github unreachable")

    monkeypatch.setattr(_gh, "exchange_authorization_code", _exchange_boom)
    u = make_user(db, email="err502@example.com")
    cookies = session_cookies(db, u)
    c = TestClient(app)
    r_start = c.get(
        "/api/github/install/start", cookies=cookies, follow_redirects=False
    )
    state = parse_qs(urlparse(r_start.headers["location"]).query)["state"][0]
    state_cookie = r_start.cookies.get(_install.STATE_COOKIE)
    assert state_cookie
    r_err = c.get(
        f"/api/github/install/callback?code=code-9&state={state}",
        cookies={**cookies, _install.STATE_COOKIE: state_cookie},
        follow_redirects=False,
    )
    assert r_err.status_code == 502, r_err.text
    cleared = (r_err.headers.get("set-cookie") or "").lower()
    assert "fixhub_gh_state=" in cleared
    assert "max-age=0" in cleared


# --- audit-fix regressions: refresh-token rotation races ---------------------


def _seed_expiring_auth(db, user, *, access_expires_in_s: int) -> None:
    """Seed a verified identity whose user access token is at/near expiry."""
    from datetime import timedelta

    from app.db.database import utcnow
    from app.llm import crypto as _crypto

    now = utcnow()
    db.add(
        GitHubAuthorization(
            user_id=user.id,
            github_user_id=777,
            github_login="seed-rotator",
            encrypted_user_token=_crypto.encrypt_secret("ghu-old-access"),
            encrypted_refresh_token=_crypto.encrypt_secret("ghr-old-refresh"),
            token_expires_at=now + timedelta(seconds=access_expires_in_s),
            refresh_token_expires_at=now + timedelta(seconds=7200),
        )
    )
    db.commit()


def _stub_refresh(monkeypatch, calls: list) -> None:
    from app.github import client as _gh

    def fake_refresh(*, refresh_token: str):
        calls.append(refresh_token)
        return {
            "access_token": "ghu-new-access",
            "refresh_token": "ghr-new-refresh",
            "expires_in": 3600,
            "refresh_token_expires_in": 7200,
        }

    monkeypatch.setattr(_gh, "refresh_user_access_token", fake_refresh)


def test_expired_token_refresh_rotates_and_persists_both_tokens(db, monkeypatch):
    from datetime import timedelta

    from app.db.database import utcnow
    from app.github.install import get_user_token
    from app.llm import crypto as _crypto

    u = make_user(db, email="rotate@example.com")
    _seed_expiring_auth(db, u, access_expires_in_s=-10)
    calls: list = []
    _stub_refresh(monkeypatch, calls)
    ident = get_user_token(db, u)
    assert ident is not None and ident["token"] == "ghu-new-access"
    assert calls == ["ghr-old-refresh"]  # ONE refresh, keyed on the stored RT
    row = (
        db.query(GitHubAuthorization)
        .filter(GitHubAuthorization.user_id == u.id)
        .first()
    )
    # the rotated pair is persisted TOGETHER, encrypted, with fresh expiries
    from app.github.install import _aware as _ts

    assert _crypto.decrypt_secret(row.encrypted_user_token) == "ghu-new-access"
    assert _crypto.decrypt_secret(row.encrypted_refresh_token) == "ghr-new-refresh"
    assert (_ts(row.token_expires_at) or utcnow() - timedelta(seconds=1)) > utcnow()
    assert (
        _ts(row.refresh_token_expires_at) or utcnow() - timedelta(seconds=1)
    ) > utcnow() + timedelta(seconds=3600)


def test_call_within_fresh_margin_never_refreshes_again(db, monkeypatch):
    from app.github.install import get_user_token

    u = make_user(db, email="rotate2@example.com")
    _seed_expiring_auth(db, u, access_expires_in_s=-10)
    calls: list = []
    _stub_refresh(monkeypatch, calls)
    first = get_user_token(db, u)
    assert first is not None and first["token"] == "ghu-new-access"
    second = get_user_token(db, u)  # now comfortably fresh
    assert second is not None and second["token"] == "ghu-new-access"
    assert calls == ["ghr-old-refresh"]  # no second GitHub refresh happened


def test_failed_refresh_leaves_authorization_intact(db, monkeypatch):
    from app.github import client as _gh
    from app.github.install import get_user_token
    from app.llm import crypto as _crypto

    u = make_user(db, email="refresh-fail@example.com")
    _seed_expiring_auth(db, u, access_expires_in_s=-10)

    def boom(*, refresh_token: str):
        raise RuntimeError("github refresh refused")

    monkeypatch.setattr(_gh, "refresh_user_access_token", boom)
    assert get_user_token(db, u) is None
    row = (
        db.query(GitHubAuthorization)
        .filter(GitHubAuthorization.user_id == u.id)
        .first()
    )
    # neither the old access token nor the refresh token was erased/changed
    assert _crypto.decrypt_secret(row.encrypted_user_token) == "ghu-old-access"
    assert _crypto.decrypt_secret(row.encrypted_refresh_token) == "ghr-old-refresh"
    assert row.github_user_id == 777


def test_non_expiring_token_skips_refresh(db, monkeypatch):
    """Tokens minted without expiry need no refresh machinery at all."""
    from app.db.models import GitHubAuthorization as _GA
    from app.github.install import get_user_token
    from app.llm import crypto as _crypto

    u = make_user(db, email="noexp@example.com")
    db.add(
        _GA(
            user_id=u.id,
            github_user_id=778,
            github_login="no-exp",
            encrypted_user_token=_crypto.encrypt_secret("ghu-permanent"),
            encrypted_refresh_token="",
            token_expires_at=None,
            refresh_token_expires_at=None,
        )
    )
    db.commit()
    calls: list = []
    _stub_refresh(monkeypatch, calls)
    ident = get_user_token(db, u)
    assert ident is not None and ident["token"] == "ghu-permanent"
    assert calls == []  # no refresh attempted for a non-expiring token



def test_connect_without_verified_identity_explains_oauth(db, monkeypatch):
    _enable_oauth(monkeypatch)
    u = make_user(db, email="noauth-connect@example.com")
    docs = {"cs": session_cookies(db, u)}
    c = TestClient(app)
    _fail_no_mint(monkeypatch)
    r = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "any/repo", "installation_id": "i-any"},
        cookies=docs["cs"],
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["github_authorization_required"] is True
    assert body["start_url"] == _install.INSTALL_START_URL
    assert db.query(GitHubConnection).filter(GitHubConnection.user_id == u.id).count() == 0


# --- repository-level authorization: mint-time permission revalidation --------
#
# The browser gate (app.github.install.installation_verified_for_user) no
# longer trusts the persisted github_user_id alone: before ANY installation
# token is minted it must reconfirm against GitHub's CURRENT grants
# (/user/installations AND the per-installation user-accessible repository
# listing). These tests stub ONLY github.com's HTTP surface (the shared
# registry) — FixHub's own gate/refresh/consume logic always runs for real.


def _recording_mint(monkeypatch) -> list[str]:
    """Accepted paths append here; rejected paths must leave it EMPTY."""
    from app.github import app_auth as _auth

    mints: list[str] = []

    def _mint(iid):
        mints.append(str(iid))
        return "minted-" + str(iid)

    monkeypatch.setattr(_auth, "installation_token", _mint)
    return mints


def _reconciled_row(db, monkeypatch, c, docs, *, token, gh_id, login, iid, full_name):
    """Create a legacy repo row and let the OAuth reconcile verify it for real.

    Returns (user, persisted Repository row) after the passing callback.
    """
    u = make_user(db, email=f"{login}@example.com")
    db.add(
        Repository(
            github_full_name=full_name,
            installation_id=iid,
            owner_id=u.id,
            github_user_id=None,  # legacy: decided by GitHub, never by the client
        )
    )
    db.commit()
    docs["cs"] = session_cookies(db, u)
    r = _drive_authorization(
        monkeypatch,
        c,
        docs["cs"],
        token=token,
        gh_id=gh_id,
        login=login,
        installations=[{"id": iid, "account": login, "type": "User"}],
        repos_by_installation={
            iid: [{"full_name": full_name, "private": False, "default_branch": "main"}]
        },
    )
    assert r.status_code == 302, r.text
    row = (
        db.query(Repository).filter(Repository.github_full_name == full_name).first()
    )
    assert row is not None and row.github_user_id == gh_id
    return u, row


def test_reconcile_verifies_only_github_listed_repositories(db, monkeypatch):
    """Legacy repos: verified ONLY when the exact full_name appears in the
    user's accessible listing; installation membership alone is NOT enough,
    and unlisted rows are preserved (never deleted)."""
    u = make_user(db, email="recon-owner@example.com")
    db.add(
        Repository(
            github_full_name="recon/listed",
            installation_id="ri-1",
            owner_id=u.id,
            github_user_id=None,
        )
    )
    db.add(
        Repository(
            github_full_name="recon/hidden",
            installation_id="ri-1",
            owner_id=u.id,
            github_user_id=None,
        )
    )
    db.commit()
    docs = {"cs": session_cookies(db, u)}
    c = TestClient(app)
    r = _drive_authorization(
        monkeypatch,
        c,
        docs["cs"],
        token="tok-recon",
        gh_id=651,
        login="recon-user",
        installations=[{"id": "ri-1", "account": "recon-user", "type": "User"}],
        repos_by_installation={
            "ri-1": [
                {"full_name": "recon/listed", "private": False, "default_branch": "main"}
            ]
        },
    )
    assert r.status_code == 302, r.text
    listed = (
        db.query(Repository)
        .filter(Repository.github_full_name == "recon/listed")
        .first()
    )
    hidden = (
        db.query(Repository)
        .filter(Repository.github_full_name == "recon/hidden")
        .first()
    )
    # A repository the user can access IS verified.
    assert listed is not None
    assert listed.github_user_id == 651 and listed.verified_at is not None
    # Valid installation but absent from the listing: NOT verified, and the
    # row is preserved rather than deleted.
    assert hidden is not None
    assert hidden.github_user_id is None and hidden.verified_at is None


def test_reconcile_listing_failure_never_verifies(db, monkeypatch):
    """A GitHub API failure during reconcile is not verification: the row
    stays unverified while the installation reconcile still succeeds."""
    from app.github import client as _gh

    u = make_user(db, email="recon-flaky@example.com")
    db.add(
        Repository(
            github_full_name="recon/real",
            installation_id="rf-1",
            owner_id=u.id,
            github_user_id=None,
        )
    )
    db.commit()
    docs = {"cs": session_cookies(db, u)}
    c = TestClient(app)
    _enable_oauth(monkeypatch)
    _stub_github(
        monkeypatch,
        token="tok-flaky",
        gh_id=653,
        login="flaky-user",
        installations=[{"id": "rf-1", "account": "flaky-user", "type": "User"}],
        repos_by_installation={"rf-1": [{"full_name": "recon/real"}]},
    )

    def _github_500(*, token, installation_id):
        raise RuntimeError("github 502")

    monkeypatch.setattr(_gh, "list_user_installation_repos", _github_500)
    flow = _start_flow(c, docs["cs"])
    r = _callback(c, docs["cs"], flow["cookie"], flow["state"])
    # The OAuth flow still succeeds overall (the reconcile is resilient).
    assert r.status_code == 302, r.text
    row = (
        db.query(Repository).filter(Repository.github_full_name == "recon/real").first()
    )
    # Broken listing != verified: the row is never stamped on API failure.
    assert row is not None
    assert row.github_user_id is None and row.verified_at is None
    # The installation WAS confirmed; browser access stays refused meanwhile.
    insts = [i["id"] for i in c.get("/api/github/installations", cookies=docs["cs"]).json()]
    assert insts == ["rf-1"]
    _fail_no_mint(monkeypatch)
    refused = c.get("/api/github/issues?repo=recon/real", cookies=docs["cs"])
    assert refused.status_code == 400, refused.text


def test_repo_removed_from_user_listing_denied_without_mint(db, monkeypatch):
    """When the repo vanishes from the user's accessible listing, the next
    browser request is refused with NO token minted; the row is preserved."""
    c = TestClient(app)
    docs: dict = {}
    u, _row = _reconciled_row(
        db,
        monkeypatch,
        c,
        docs,
        token="tok-live",
        gh_id=661,
        login="live-owner",
        iid="li-1",
        full_name="live/repo",
    )
    from app.github import client as _gh

    mints = _recording_mint(monkeypatch)
    monkeypatch.setattr(
        _gh,
        "list_issues",
        lambda *, token, full_name, state="open", limit=50: [
            {"number": 1, "title": "t", "body": "b", "url": "", "labels": []}
        ],
    )
    ok = c.get("/api/github/issues?repo=live/repo", cookies=docs["cs"])
    assert ok.status_code == 200, ok.text
    assert mints == ["li-1"]
    # GitHub's truth changes: the user can no longer see this repository.
    monkeypatch._fixhub_gh_registry["repos"]["li-1"] = []
    _install._clear_user_perm_cache()  # the designed invalidation hook
    mints.clear()
    denied = c.get("/api/github/issues?repo=live/repo", cookies=docs["cs"])
    assert denied.status_code == 400, denied.text
    assert denied.json()["github_authorization_required"] is True
    assert mints == []  # no installation token for a rejected request
    # The repository row is preserved (usable again once access returns).
    preserved = (
        db.query(Repository).filter(Repository.github_full_name == "live/repo").first()
    )
    assert preserved is not None and preserved.owner_id == u.id


def test_installation_removed_denied_without_mint(db, monkeypatch):
    """When the installation leaves the user's installation listing, minting
    is refused for every repo under it — no token, row preserved."""
    c = TestClient(app)
    docs: dict = {}
    u, _row = _reconciled_row(
        db,
        monkeypatch,
        c,
        docs,
        token="tok-uninst",
        gh_id=663,
        login="uninst-owner",
        iid="ui-1",
        full_name="uninst/repo",
    )
    mints = _recording_mint(monkeypatch)
    monkeypatch._fixhub_gh_registry["tokens"]["tok-uninst"]["installations"] = []
    _install._clear_user_perm_cache()
    r = c.get("/api/github/issues?repo=uninst/repo", cookies=docs["cs"])
    assert r.status_code == 400, r.text
    assert r.json()["github_authorization_required"] is True
    assert mints == []
    preserved = (
        db.query(Repository)
        .filter(Repository.github_full_name == "uninst/repo")
        .first()
    )
    assert preserved is not None and preserved.owner_id == u.id


def test_revoked_user_authorization_cannot_mint_tokens(db, monkeypatch):
    """Revoked/cleared stored tokens stop minting immediately; and even a
    fresh DB-side token cannot mint while GitHub cannot vouch (fail closed)."""
    c = TestClient(app)
    docs: dict = {}
    u, _row = _reconciled_row(
        db,
        monkeypatch,
        c,
        docs,
        token="tok-revoke",
        gh_id=665,
        login="revoke-owner",
        iid="rv-1",
        full_name="revoke/repo",
    )
    registry = monkeypatch._fixhub_gh_registry
    _fail_no_mint(monkeypatch)  # any mint during refusal fails the test
    from app.github import client as _gh
    from app.llm import crypto as _crypto

    auth_row = (
        db.query(GitHubAuthorization).filter(GitHubAuthorization.user_id == u.id).first()
    )
    # 1) The stored user token is revoked in DB reality (empty ciphertext):
    #    get_user_token refuses, so the gate denies with no GitHub call.
    auth_row.encrypted_user_token = ""
    db.commit()
    r = c.get("/api/github/issues?repo=revoke/repo", cookies=docs["cs"])
    assert r.status_code == 400, r.text
    assert r.json()["github_authorization_required"] is True
    # 2) A fresh stored token is not enough while GitHub cannot establish
    #    permission: a failing listing must fail CLOSED (never "pass").
    auth_row.encrypted_user_token = _crypto.encrypt_secret("tok-fresh")
    db.commit()
    registry["tokens"]["tok-fresh"] = {
        "gh_id": 665,
        "login": "revoke-owner",
        "installations": [{"id": "rv-1", "account": "revoke-owner", "type": "User"}],
    }

    def _github_down(*, token):
        raise RuntimeError("github unreachable")

    monkeypatch.setattr(_gh, "list_user_installations", _github_down)
    _install._clear_user_perm_cache()
    r2 = c.get("/api/github/issues?repo=revoke/repo", cookies=docs["cs"])
    assert r2.status_code == 400, r2.text
    # 3) Once GitHub answers truthfully again, the SAME fresh token proves
    #    access and minting resumes — recovery requires current GitHub truth.
    monkeypatch.setattr(
        _gh,
        "list_user_installations",
        lambda *, token: (
            list(registry["tokens"][token]["installations"])
            if token in registry["tokens"]
            else []
        ),
    )
    minted = _recording_mint(monkeypatch)
    monkeypatch.setattr(
        _gh,
        "list_issues",
        lambda *, token, full_name, state="open", limit=50: [
            {"number": 1, "title": "t", "body": "b", "url": "", "labels": []}
        ],
    )
    monkeypatch.setattr(
        _gh,
        "list_user_installation_repos",
        lambda *, token, installation_id: (
            list(registry["repos"].get(str(installation_id), []))
            if token in registry["tokens"]
            else []
        ),
    )
    _install._clear_user_perm_cache()
    r3 = c.get("/api/github/issues?repo=revoke/repo", cookies=docs["cs"])
    assert r3.status_code == 200, r3.text
    assert minted == ["rv-1"]


def test_legitimate_access_keeps_working_and_cache_is_short_lived(db, monkeypatch):
    """The exact verified repository keeps working end to end; within the
    short TTL the gate does NOT re-query GitHub, after expiry it re-asks."""
    c = TestClient(app)
    docs: dict = {}
    u, _row = _reconciled_row(
        db,
        monkeypatch,
        c,
        docs,
        token="tok-legit",
        gh_id=667,
        login="legit-owner",
        iid="lg-1",
        full_name="legit/app",
    )
    from app.github import client as _gh

    registry = monkeypatch._fixhub_gh_registry
    repo_calls: list[str] = []

    def _watch_repos(*, token, installation_id):
        repo_calls.append(str(installation_id))
        if token not in registry["tokens"]:
            return []
        return list(registry["repos"].get(str(installation_id), []))

    monkeypatch.setattr(_gh, "list_user_installation_repos", _watch_repos)
    mints = _recording_mint(monkeypatch)
    monkeypatch.setattr(
        _gh,
        "list_issues",
        lambda *, token, full_name, state="open", limit=50: [
            {"number": 7, "title": "t", "body": "b", "url": "", "labels": []}
        ],
    )
    r1 = c.get("/api/github/issues?repo=legit/app", cookies=docs["cs"])
    assert r1.status_code == 200, r1.text
    assert r1.json()[0]["number"] == 7
    assert mints == ["lg-1"]
    first = len(repo_calls)
    # Second request inside the TTL: cached listing, GitHub not re-queried…
    r2 = c.get("/api/github/issues?repo=legit/app", cookies=docs["cs"])
    assert r2.status_code == 200, r2.text
    assert len(repo_calls) == first
    # …and after expiry (the TTL is read at call time) the gate re-asks
    # GitHub instead of extending a stale decision: every subsequent request
    # re-queries because a TTL-0 entry is born already expired.
    monkeypatch.setattr(_install, "PERM_CACHE_TTL_S", 0)
    _install._clear_user_perm_cache()
    r3 = c.get("/api/github/issues?repo=legit/app", cookies=docs["cs"])
    assert r3.status_code == 200, r3.text
    assert len(repo_calls) == first + 1
    r4 = c.get("/api/github/issues?repo=legit/app", cookies=docs["cs"])
    assert r4.status_code == 200, r4.text
    assert len(repo_calls) == first + 2
    # Restore the normal TTL: one re-query after the clear, then cache hits.
    monkeypatch.setattr(_install, "PERM_CACHE_TTL_S", 60)
    _install._clear_user_perm_cache()
    r5 = c.get("/api/github/issues?repo=legit/app", cookies=docs["cs"])
    assert r5.status_code == 200, r5.text
    assert len(repo_calls) == first + 3
    r6 = c.get("/api/github/issues?repo=legit/app", cookies=docs["cs"])
    assert r6.status_code == 200, r6.text
    assert len(repo_calls) == first + 3  # back inside the TTL: cache hit


def _webhook_sig(body: bytes) -> str:
    """Same contract as tests/test_github_ownership.py (conftest secret)."""
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def _post_webhook(c, payload, delivery, event="issues"):
    body = json.dumps(payload).encode()
    return c.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _webhook_sig(body),
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
        },
    )


def test_webhook_automation_ignores_user_permission_state(db, monkeypatch):
    """A repo whose user-facing authorization is severed (legacy row, no
    listing, no usable user token) still accepts webhook-driven tasks."""
    ua = make_user(db, email="hook-owner@example.com")
    db.add(GitHubConnection(user_id=ua.id, installation_id="4242"))
    db.add(
        Repository(
            github_full_name="acme/hooked",
            installation_id="4242",
            owner_id=ua.id,
            github_user_id=None,  # browser gate refuses this row
        )
    )
    db.commit()
    docs = {"cs": session_cookies(db, ua)}
    _fail_no_mint(monkeypatch)  # and the webhook path must not mint either
    c = TestClient(app)
    # The browser path is closed for this repo…
    r_browser = c.get("/api/github/issues?repo=acme/hooked", cookies=docs["cs"])
    assert r_browser.status_code == 400, r_browser.text
    # …while signature-validated webhook automation still creates tasks.
    payload = {
        "action": "opened",
        "installation": {"id": 4242},
        "repository": {"full_name": "acme/hooked"},
        "issue": {
            "number": 421,
            "title": "t",
            "body": "b",
            "html_url": "",
            "labels": [],
        },
    }
    r = _post_webhook(c, payload, "d-hook-1")
    assert r.status_code == 200, r.text
    assert r.json()["task_id"]
    t = db.query(Task).filter(Task.issue_number == 421).first()
    assert t is not None and t.owner_id == ua.id
    # Sever the stored user token (authorization revoked) and try again: the
    # next webhook keeps working, still independent of the browser gate.
    auth_row = (
        db.query(GitHubAuthorization).filter(GitHubAuthorization.user_id == ua.id).first()
    )
    if auth_row is not None:
        auth_row.encrypted_user_token = ""
        db.commit()
    payload["issue"]["number"] = 422
    r2 = _post_webhook(c, payload, "d-hook-2")
    assert r2.status_code == 200, r2.text
    assert r2.json()["task_id"]
    t2 = db.query(Task).filter(Task.issue_number == 422).first()
    assert t2 is not None and t2.owner_id == ua.id

# --- public GitHub connection config + browser-friendly callback failures ---


def _public_github_settings(monkeypatch, *, slug="", install_url="") -> None:
    monkeypatch.setattr(settings, "GITHUB_APP_SLUG", slug)
    monkeypatch.setattr(settings, "GITHUB_APP_INSTALL_URL", install_url)
    monkeypatch.setattr(settings, "GITHUB_APP_CLIENT_ID", "")
    monkeypatch.setattr(settings, "GITHUB_APP_CLIENT_SECRET", "")


def test_github_config_requires_fixhub_session(db):
    r = TestClient(app).get("/api/github/config")
    assert r.status_code == 401


def test_github_config_exposes_only_public_install_settings(db, monkeypatch):
    _public_github_settings(monkeypatch, slug="fixhub-app")
    _enable_oauth(monkeypatch)
    u = make_user(db, email="gh-config@example.com")

    r = TestClient(app).get(
        "/api/github/config", cookies=session_cookies(db, u)
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "install_url": "https://github.com/apps/fixhub-app/installations/new",
        "oauth_enabled": True,
        "start_url": "/api/github/install/start",
    }
    assert set(body) == {"install_url", "oauth_enabled", "start_url"}
    lowered = r.text.lower()
    for secret in ("client_secret", "private_key", "t45t-5ecret", "access_token"):
        assert secret not in lowered


def test_github_config_explicit_https_install_url_overrides_slug(db, monkeypatch):
    _public_github_settings(
        monkeypatch,
        slug="ignored-slug",
        install_url="https://github.example.com/apps/fixhub/installations/new",
    )
    u = make_user(db, email="gh-config-url@example.com")

    body = TestClient(app).get(
        "/api/github/config", cookies=session_cookies(db, u)
    ).json()

    assert (
        body["install_url"]
        == "https://github.example.com/apps/fixhub/installations/new"
    )
    assert body["oauth_enabled"] is False


def test_github_config_never_exposes_unsafe_install_values(db, monkeypatch):
    _public_github_settings(monkeypatch)
    u = make_user(db, email="gh-config-unsafe@example.com")
    c = TestClient(app)
    cookies = session_cookies(db, u)

    unsafe = [
        "javascript:alert(1)",
        "http://github.com/apps/fixhub/installations/new",
        "//github.com/apps/fixhub/installations/new",
        "https://user:pass@github.com/apps/fixhub/installations/new",
        "not a url",
    ]
    for value in unsafe:
        monkeypatch.setattr(settings, "GITHUB_APP_INSTALL_URL", value)
        body = c.get("/api/github/config", cookies=cookies).json()
        assert body["install_url"] == "", value

    # A slug is only accepted when it is a single safe path segment.
    monkeypatch.setattr(settings, "GITHUB_APP_INSTALL_URL", "")
    for slug in ("../evil", "two/segments", "bad slug", "-leading-dash"):
        monkeypatch.setattr(settings, "GITHUB_APP_SLUG", slug)
        body = c.get("/api/github/config", cookies=cookies).json()
        assert body["install_url"] == "", slug


def test_callback_exchange_failure_redirects_browser_navigation(db, monkeypatch):
    """A top-level browser callback must land back in the app, not on JSON."""
    _fake_store(monkeypatch)
    _enable_oauth(monkeypatch)
    _stub_github(
        monkeypatch,
        token="tok-html-502",
        gh_id=731,
        login="browser-502",
        installations=[],
    )
    from app.github import client as _gh

    def _exchange_boom(*args, **kwargs):
        raise RuntimeError("github unreachable")

    monkeypatch.setattr(_gh, "exchange_authorization_code", _exchange_boom)
    u = make_user(db, email="browser-502@example.com")
    cookies = session_cookies(db, u)
    c = TestClient(app)
    flow = _start_flow(c, cookies)

    r = c.get(
        f"/api/github/install/callback?code=code-html&state={flow['state']}",
        cookies={**cookies, _install.STATE_COOKIE: flow["cookie"]},
        headers={"accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )

    assert r.status_code == 302, r.text
    assert r.headers["location"] == "/?github_connect=error"
    cleared = (r.headers.get("set-cookie") or "").lower()
    assert "fixhub_gh_state=" in cleared and "max-age=0" in cleared


def test_callback_state_store_outage_redirects_browser_navigation(db, monkeypatch):
    """The production 503 fail-safe is a redirect for browser navigations."""
    _fake_store(monkeypatch, fail=True)
    _enable_oauth(monkeypatch)
    monkeypatch.setattr(settings, "ENV", "prod")
    u = make_user(db, email="browser-503@example.com")
    cookies = session_cookies(db, u)
    c = TestClient(app)
    flow = _start_flow(c, cookies)

    r = c.get(
        f"/api/github/install/callback?code=code-html&state={flow['state']}",
        cookies={**cookies, _install.STATE_COOKIE: flow["cookie"]},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert r.status_code == 302, r.text
    assert r.headers["location"] == "/?github_connect=error"
    cleared = (r.headers.get("set-cookie") or "").lower()
    assert "fixhub_gh_state=" in cleared and "max-age=0" in cleared
