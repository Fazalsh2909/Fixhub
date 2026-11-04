"""Task 1.8: browser-triggered task execution/publishing authorization.

Contract under test:
- Browser actions (from-issue, manual rerun, manual approve) persist explicit
  provenance (`run_origin='user'` + the authenticated `run_actor_id`) and are
  refused whenever GitHub CURRENTLY says the user lost the installation or
  the exact repository.
- The worker revalidates the same provenance/actor before minting an
  installation token, cloning, or publishing — an API-time check alone is
  not enough.
- Signature-validated webhook tasks and CI repair scheduling keep running on
  the GitHub App installation identity without an interactive user token.
- Refusals happen before any installation-token mint or publish side effect.

Only github.com's HTTP surface is stubbed (`list_user_installations`,
`list_user_installation_repos`); FixHub's real gate, refresh/decrypt path,
worker revalidation, service layer, and routes always run. Minting is either
recorded or forced to raise, so a refused path that attempts a mint fails the
test instead of passing silently.
"""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

import app.github.install as _install
import app.tasks.service as _svc
from app.db.models import GitHubConnection, Repository, Task
from app.main import app
from tests.conftest import make_github_identity, make_user, session_cookies

IID = "i18"
FULL = "acme/task-auth"
TOKEN = "ghu-task-auth"
GH_ID = 1800


class _CloneReached(RuntimeError):
    """Sentinel: the worker got past authorization and reached the clone."""


def _truth(monkeypatch, *, installations=(IID,), repo_listing=None):
    """Stub github.com's per-user CURRENT grants (no FixHub logic stubbed)."""
    from app.github import client as _gh

    state = {
        "token": TOKEN,
        "tokens": {
            TOKEN: {
                "gh_id": GH_ID,
                "installations": [
                    {"id": i, "account": "auth-owner", "type": "User"}
                    for i in installations
                ],
            }
        },
        "repos": {str(IID): list(repo_listing or [])},
    }

    def _installs(*, token):
        info = state["tokens"].get(token)
        return list(info["installations"]) if info else []

    def _repos(*, token, installation_id):
        if token not in state["tokens"]:
            return []
        return list(state["repos"].get(str(installation_id), []))

    monkeypatch.setattr(_gh, "list_user_installations", _installs)
    monkeypatch.setattr(_gh, "list_user_installation_repos", _repos)
    return state


def _revoke_installation(state):
    state["tokens"][state["token"]]["installations"] = []
    _install._clear_user_perm_cache()


def _revoke_repository(state):
    state["repos"][str(IID)] = []
    _install._clear_user_perm_cache()


def _mint_recorder(monkeypatch):
    from app.github import app_auth as _auth

    mints: list[str] = []

    def _mint(iid):
        mints.append(str(iid))
        return "minted-" + str(iid)

    monkeypatch.setattr(_auth, "installation_token", _mint)
    return mints


def _no_mint(monkeypatch):
    """Any installation-token mint attempt from a refused path fails the test."""
    from app.github import app_auth as _auth

    def _boom(iid):
        raise AssertionError(f"installation token minted for {iid!r}")

    monkeypatch.setattr(_auth, "installation_token", _boom)


def _seed(db, monkeypatch, *, login="auth-owner", verified=True):
    u = make_user(db, email=f"{login}@example.com")
    gh_id = make_github_identity(
        db,
        u,
        github_user_id=GH_ID,
        login=login,
        installations=(IID,),
        token=TOKEN,
    )
    db.add(
        Repository(
            github_full_name=FULL,
            installation_id=IID,
            owner_id=u.id,
            github_user_id=gh_id if verified else None,
        )
    )
    db.commit()
    repo = (
        db.query(Repository).filter(Repository.github_full_name == FULL).first()
    )
    state = _truth(
        monkeypatch, repo_listing=[{"full_name": FULL, "private": True}]
    )
    return u, repo, state


def _task(db, user, repo, *, status="QUEUED", origin="user", actor_id=None, **kw):
    t = Task(
        repository=repo.github_full_name,
        repository_id=repo.id,
        owner_id=user.id,
        trigger_type=kw.pop("trigger_type", "issue"),
        issue_number=kw.pop("issue_number", 18),
        issue_title="t",
        issue_body="b",
        status=status,
        run_origin=origin,
        run_actor_id=user.id if actor_id is None else actor_id,
        **kw,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _enqueue_recorder(monkeypatch):
    from app.tasks import queue as _queue

    calls: list[int] = []

    def _enqueue(task_id):
        calls.append(task_id)
        return {"enqueued": True, "job_id": f"job-{task_id}"}

    monkeypatch.setattr(_queue, "enqueue_for_task", _enqueue)
    return calls


def _block_clone(monkeypatch):
    calls: list[tuple] = []

    def _clone(src, task_id, branch=""):
        calls.append((src, task_id, branch))
        raise _CloneReached("clone reached")

    monkeypatch.setattr(_svc._ws, "clone_repo", _clone)
    return calls


def _reload(db, task_id):
    db.expire_all()
    return db.query(Task).filter(Task.id == task_id).first()


# --- 1. valid current permissions trigger + worker revalidates and proceeds ---


def test_valid_user_can_trigger_task_and_worker_revalidates_actor(db, monkeypatch):
    u, repo, _state = _seed(db, monkeypatch)
    t = _task(db, u, repo, status="FAILED", error="previous failure")
    enqueued = _enqueue_recorder(monkeypatch)
    c = TestClient(app)

    r = c.post(f"/api/tasks/{t.id}/run", cookies=session_cookies(db, u))
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is True
    assert enqueued == [t.id]

    row = _reload(db, t.id)
    assert row.status == "QUEUED"
    assert row.run_origin == "user"
    assert row.run_actor_id == u.id

    # Service boundary: the real worker gate runs the real GitHub gate and
    # then proceeds to mint + clone (the sentinel stops it there).
    mints = _mint_recorder(monkeypatch)
    clones = _block_clone(monkeypatch)
    out = _svc.run_task_inline(t.id)
    assert out["status"] == "BLOCKED", out
    assert "clone reached" in out.get("error", "")
    assert mints == [IID]
    assert clones and clones[0][1] == t.id


# --- 2. lost installation access blocks a manual rerun ----------------------


def test_lost_installation_access_blocks_rerun(db, monkeypatch):
    u, repo, state = _seed(db, monkeypatch)
    t = _task(db, u, repo, status="FAILED")
    _revoke_installation(state)
    _no_mint(monkeypatch)

    from app.tasks import queue as _queue

    monkeypatch.setattr(
        _queue,
        "enqueue_for_task",
        lambda task_id: (_ for _ in ()).throw(
            AssertionError("refused rerun must not enqueue")
        ),
    )
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/run", cookies=session_cookies(db, u))
    assert r.status_code == 400, r.text
    assert r.json()["github_authorization_required"] is True
    assert r.json()["start_url"] == _install.INSTALL_START_URL
    assert _reload(db, t.id).status == "FAILED"  # never re-queued

    # Worker boundary: a row already QUEUED is refused before any side effect.
    q = _task(db, u, repo, status="QUEUED", issue_number=19)
    clones = _block_clone(monkeypatch)
    out = _svc.run_task_inline(q.id)
    assert out["status"] == "FAILED", out
    assert out.get("github_authorization_required") is True
    assert "github_authorization_required" in out["error"]
    assert clones == []
    assert _reload(db, q.id).status == "FAILED"


# --- 3. lost exact-repository access blocks a manual rerun -------------------


def test_lost_repository_access_blocks_rerun(db, monkeypatch):
    u, repo, state = _seed(db, monkeypatch)
    t = _task(db, u, repo, status="FAILED")
    _revoke_repository(state)
    _no_mint(monkeypatch)
    c = TestClient(app)

    r = c.post(f"/api/tasks/{t.id}/run", cookies=session_cookies(db, u))
    assert r.status_code == 400, r.text
    assert r.json()["github_authorization_required"] is True
    assert _reload(db, t.id).status == "FAILED"

    q = _task(db, u, repo, status="QUEUED", issue_number=20)
    clones = _block_clone(monkeypatch)
    out = _svc.run_task_inline(q.id)
    assert out["status"] == "FAILED", out
    assert out.get("github_authorization_required") is True
    assert clones == []


# --- 4. queued while valid, revoked before the worker claims it --------------


def test_queued_then_revoked_refused_by_worker(db, monkeypatch):
    u, repo, state = _seed(db, monkeypatch)
    t = _task(db, u, repo, status="QUEUED")
    # Authorization was valid when the task was queued.
    assert _install.task_github_authorization(db, t, u) == (True, "")

    _revoke_installation(state)
    _no_mint(monkeypatch)
    clones = _block_clone(monkeypatch)
    out = _svc.run_task_inline(t.id)
    assert out["status"] == "FAILED", out
    assert out.get("github_authorization_required") is True
    assert "GitHub access" in out["error"]
    assert clones == []
    row = _reload(db, t.id)
    assert row.status == "FAILED"
    assert row.branch == "" and row.pr_url == ""
    assert (
        db.query(Task)
        .filter(Task.id == t.id)
        .first()
        .error.startswith("github_authorization_required:")
    )


# --- 5. manual approval rejected after revocation ----------------------------


def test_manual_approval_rejected_after_revocation(db, monkeypatch):
    u, repo, state = _seed(db, monkeypatch)
    t = _task(db, u, repo, status="NEEDS_REVIEW", branch="fixhub-fixes/issue-18")
    _revoke_repository(state)
    _no_mint(monkeypatch)

    # The HTTP route refuses before the service layer is reached.
    real_approve = _svc.approve_task
    monkeypatch.setattr(
        _svc,
        "approve_task",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("refused approval must not reach the service")
        ),
    )
    c = TestClient(app)
    r = c.post(
        f"/api/tasks/{t.id}/approve",
        json={"title": "t"},
        cookies=session_cookies(db, u),
    )
    assert r.status_code == 400, r.text
    assert r.json()["github_authorization_required"] is True
    monkeypatch.setattr(_svc, "approve_task", real_approve)

    # Service boundary: the authenticated actor is validated again, refused
    # before workspace access or publish.
    publish_calls: list = []
    monkeypatch.setattr(
        _svc,
        "_publish_task",
        lambda *a, **k: publish_calls.append((a, k)) or {"status": "COMPLETED"},
    )
    out = _svc.approve_task(t.id, actor_id=u.id)
    assert out["status"] == "FAILED", out
    assert out.get("github_authorization_required") is True
    assert publish_calls == []


# --- 6. rejected paths never mint an installation token ----------------------


def test_rejected_paths_never_mint_installation_tokens(db, monkeypatch):
    u, repo, state = _seed(db, monkeypatch)
    _revoke_installation(state)
    _no_mint(monkeypatch)  # any mint attempt raises AssertionError
    c = TestClient(app)

    failed = _task(db, u, repo, status="FAILED", issue_number=21)
    r = c.post(f"/api/tasks/{failed.id}/run", cookies=session_cookies(db, u))
    assert r.status_code == 400, r.text

    queued = _task(db, u, repo, status="QUEUED", issue_number=22)
    _block_clone(monkeypatch)
    assert _svc.run_task_inline(queued.id)["status"] == "FAILED"

    review = _task(db, u, repo, status="NEEDS_REVIEW", issue_number=23)
    assert _svc.approve_task(review.id, actor_id=u.id)["status"] == "FAILED"


# --- 7. rejected paths publish no branch or pull request ---------------------


def test_rejected_operations_publish_nothing(db, monkeypatch):
    u, repo, state = _seed(db, monkeypatch)
    _revoke_installation(state)
    _no_mint(monkeypatch)

    publish_calls: list = []
    local_push_calls: list = []
    changed_calls: list = []
    monkeypatch.setattr(
        _svc,
        "_publish_task",
        lambda *a, **k: publish_calls.append((a, k)) or {"status": "COMPLETED"},
    )
    monkeypatch.setattr(
        _svc, "_local_commit_push", lambda *a, **k: local_push_calls.append(a)
    )
    monkeypatch.setattr(
        _svc._pub, "changed_files", lambda path: changed_calls.append(path) or []
    )

    review = _task(db, u, repo, status="NEEDS_REVIEW", issue_number=24)
    out = _svc.approve_task(review.id, actor_id=u.id)
    assert out["status"] == "FAILED", out
    assert publish_calls == []
    assert local_push_calls == []
    assert changed_calls == []
    row = _reload(db, review.id)
    assert row.branch == ""
    assert row.pr_url == "" and row.pr_number is None

    queued = _task(db, u, repo, status="QUEUED", issue_number=25)
    _block_clone(monkeypatch)
    out2 = _svc.run_task_inline(queued.id)
    assert out2["status"] == "FAILED", out2
    assert publish_calls == []


# --- 8. user A cannot operate on user B's task -------------------------------


def test_user_a_cannot_operate_on_user_b_task(db, monkeypatch):
    b, repo, _state = _seed(db, monkeypatch, login="owner-b")
    a = make_user(db, email="intruder-a@example.com")
    t = _task(db, b, repo, status="NEEDS_REVIEW", issue_number=26)
    _no_mint(monkeypatch)
    c = TestClient(app)
    cookies_a = session_cookies(db, a)

    assert c.post(f"/api/tasks/{t.id}/run", cookies=cookies_a).status_code == 404
    # A payload claiming the owner is ignored: the session identity decides.
    assert (
        c.post(
            f"/api/tasks/{t.id}/approve",
            json={"owner_id": b.id, "title": "t"},
            cookies=cookies_a,
        ).status_code
        == 404
    )

    # Service boundary: a tampered actor id is refused by owner validation
    # before any GitHub call or publish.
    out = _svc.approve_task(t.id, actor_id=a.id)
    assert out["status"] == "FAILED", out
    assert out.get("github_authorization_required") is True

    # Worker boundary: even if provenance/actor were tampered in the row, the
    # actor must be the task owner and no token is minted.
    t.status = "QUEUED"
    t.run_origin = "user"
    t.run_actor_id = a.id
    db.commit()
    clones = _block_clone(monkeypatch)
    out2 = _svc.run_task_inline(t.id)
    assert out2["status"] == "FAILED", out2
    assert clones == []


# --- 9. webhook-created tasks execute through trusted automation -------------


def _webhook_sig(body: bytes) -> str:
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


def test_webhook_task_executes_via_trusted_automation(db, monkeypatch):
    u = make_user(db, email="hook-task-owner@example.com")
    db.add(GitHubConnection(user_id=u.id, installation_id="4242"))
    db.add(
        Repository(
            github_full_name="acme/hooked-task",
            installation_id="4242",
            owner_id=u.id,
            github_user_id=None,  # browser authorization is severed
        )
    )
    db.commit()

    from app.tasks import queue as _queue

    monkeypatch.setattr(
        _queue,
        "enqueue_task",
        lambda task_id, **kw: {"enqueued": True, "job_id": "hook-job"},
    )
    c = TestClient(app)
    payload = {
        "action": "opened",
        "installation": {"id": 4242},
        "repository": {"full_name": "acme/hooked-task"},
        "issue": {
            "number": 918,
            "title": "t",
            "body": "b",
            "html_url": "",
            "labels": [],
        },
    }
    r = _post_webhook(c, payload, "d-task-auth-1")
    assert r.status_code == 200, r.text
    task = db.query(Task).filter(Task.issue_number == 918).first()
    assert task is not None
    assert task.run_origin == "automation"
    assert task.run_actor_id is None

    # Browser gate would refuse this repo (no verified identity), but the
    # signature-validated webhook task still executes on the App identity.
    assert _install.task_github_authorization(db, task, u)[0] is False

    # A manual rerun of a webhook-created task IS user-initiated: the browser
    # request is refused before enqueue and the automation row is untouched.
    denied = c.post(f"/api/tasks/{task.id}/run", cookies=session_cookies(db, u))
    assert denied.status_code == 400, denied.text
    assert denied.json()["github_authorization_required"] is True
    row = _reload(db, task.id)
    assert row.status == "QUEUED"
    assert row.run_origin == "automation" and row.run_actor_id is None

    mints = _mint_recorder(monkeypatch)
    clones = _block_clone(monkeypatch)
    out = _svc.run_task_inline(task.id)
    assert out["status"] == "BLOCKED", out
    assert "clone reached" in out.get("error", "")
    assert mints == ["4242"]
    assert clones and clones[0][1] == task.id


# --- 10. CI repair automation remains functional -----------------------------


def test_ci_repair_automation_remains_functional(db, monkeypatch):
    u = make_user(db, email="ci-repair-owner@example.com")
    db.add(
        Repository(
            github_full_name="acme/ci-repair",
            installation_id="ci-18",
            owner_id=u.id,
            github_user_id=None,  # no interactive user authorization at all
        )
    )
    t = Task(
        repository="acme/ci-repair",
        trigger_type="ci",
        ci_sha="abc123",
        ci_run_id="99",
        ci_workflow="CI",
        status="AWAITING_CI",
        commit_sha="deadbeef",
        owner_id=u.id,
        run_origin="automation",
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    from app.github import client as _gh
    from app.tasks import ciwatch as _ciwatch
    from app.tasks import queue as _queue

    mints = _mint_recorder(monkeypatch)
    monkeypatch.setattr(
        _gh,
        "sha_check_conclusion",
        lambda *, token, full_name, sha: "failure",
    )
    monkeypatch.setattr(
        _gh,
        "failed_log_tail",
        lambda *, token, full_name, run_id, job_name="": "boom",
    )
    repair_calls: list[int] = []
    monkeypatch.setattr(
        _queue,
        "enqueue_repair",
        lambda task_id: repair_calls.append(task_id)
        or {"enqueued": True, "job_id": "repair-job"},
    )

    out = _ciwatch.check_awaiting_ci()
    assert out["repair_enqueued"] == 1, out
    assert repair_calls == [t.id]
    assert mints == ["ci-18"]
    row = _reload(db, t.id)
    assert row.status == "QUEUED"
    assert row.ci_attempt_count == 1
    assert row.run_origin == "automation"

    # The repair worker keeps executing on the App identity (no user gate).
    mints.clear()
    monkeypatch.setattr(_svc, "_green_upstream_fix", lambda **kw: "")
    clones = _block_clone(monkeypatch)
    repair = _svc.run_task_inline(t.id, repair=True)
    assert repair["status"] == "BLOCKED", repair
    assert "clone reached" in repair.get("error", "")
    # Two App-identity mints, never a user token: the pre-agent green-upstream
    # check mints for its own read (the token argument is evaluated before the
    # stubbed helper is called), then the workspace clone mints again.
    assert mints == ["ci-18", "ci-18"]
    assert clones and clones[0][1] == t.id


# --- 11. from-issue re-driving a queued automation task is user-gated --------


def test_from_issue_duplicate_restart_becomes_user_gated(db, monkeypatch):
    u, repo, state = _seed(db, monkeypatch)
    existing = Task(
        repository=repo.github_full_name,
        repository_id=repo.id,
        owner_id=u.id,
        trigger_type="issue",
        issue_number=77,
        issue_title="webhook-created",
        issue_body="b",
        status="QUEUED",
        run_origin="automation",  # created by a signature-validated webhook
        run_actor_id=None,
    )
    db.add(existing)
    db.commit()
    db.refresh(existing)

    from app.tasks import queue as _queue

    enqueued: list[int] = []
    monkeypatch.setattr(
        _queue,
        "enqueue_task",
        lambda task_id, **kw: enqueued.append(task_id)
        or {"enqueued": True, "job_id": "fix-button"},
    )
    c = TestClient(app)
    r = c.post(
        "/api/tasks/from-issue",
        json={"repository": FULL, "issue_number": 77},
        cookies=session_cookies(db, u),
    )
    assert r.status_code == 200, r.text
    assert r.json()["task_id"] == existing.id
    assert r.json()["duplicate"] == "active_task"
    assert enqueued == [existing.id]

    # The browser re-drove the queued run: provenance flips before enqueue.
    row = _reload(db, existing.id)
    assert row.run_origin == "user" and row.run_actor_id == u.id

    # Access revoked after the click, before the worker claims it: the
    # re-driven run is refused with no mint and no clone.
    _revoke_installation(state)
    _no_mint(monkeypatch)
    clones = _block_clone(monkeypatch)
    out = _svc.run_task_inline(existing.id)
    assert out["status"] == "FAILED", out
    assert out.get("github_authorization_required") is True
    assert clones == []
