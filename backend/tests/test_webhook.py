"""Webhook signature, connected-repo gate, trigger policy, dedupe, task creation."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.config import settings
from app.db.models import GitHubConnection, Repository, Task, WebhookDelivery
from app.github.webhook import verify_signature
from app.main import app
from tests.conftest import make_user


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def _own(db, installation_id=555, email="wh@example.com", repo="acme/demo"):
    """Give the webhook an owner AND an explicitly connected repository."""
    u = make_user(db, email=email)
    db.add(GitHubConnection(user_id=u.id, installation_id=str(installation_id)))
    db.add(
        Repository(
            github_full_name=repo,
            installation_id=str(installation_id),
            owner_id=u.id,
        )
    )
    db.commit()
    return u


def _post(c, payload, delivery, event):
    body = json.dumps(payload).encode()
    return c.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sig(body),
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
        },
    )


def _issue(number=7, repo="acme/demo", installation_id=555, action="opened"):
    return {
        "action": action,
        "installation": {"id": installation_id},
        "repository": {"full_name": repo},
        "issue": {
            "number": number,
            "title": "login broken",
            "body": "500 on login",
            "html_url": "http://x/7",
            "labels": [],
        },
    }


def _workflow(
    run_id=99,
    sha="abc123",
    branch="main",
    name="CI",
    path=".github/workflows/ci.yml",
    conclusion="failure",
    repo="acme/demo",
    installation_id=555,
):
    return {
        "action": "completed",
        "installation": {"id": installation_id},
        "repository": {"full_name": repo},
        "workflow_run": {
            "id": run_id,
            "name": name,
            "path": path,
            "conclusion": conclusion,
            "head_sha": sha,
            "head_branch": branch,
            "html_url": f"http://x/{run_id}",
        },
    }


def _check(
    sha="def456",
    job="backend",
    branch="main",
    app_slug="github-actions",
    repo="acme/demo",
    installation_id=555,
):
    return {
        "action": "completed",
        "installation": {"id": installation_id},
        "repository": {"full_name": repo},
        "check_run": {
            "conclusion": "failure",
            "head_sha": sha,
            "head_branch": branch,
            "name": job,
            "app": {"slug": app_slug},
            "html_url": "http://x/actions/runs/4242/job/99",
        },
    }


def _checks_on(monkeypatch):
    """check_run is opt-in; enable it for its own tests."""
    monkeypatch.setattr(settings, "CI_TRIGGER_EVENTS", "workflow_run,check_run")


def test_verify_signature_ok_and_bad():
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("test-secret", body, good) is True
    assert verify_signature("test-secret", body, "sha256=deadbeef") is False
    assert verify_signature("", body, good) is False
    assert verify_signature("test-secret", body, "") is False


def test_issue_trigger_creates_task(db):
    _own(db)
    c = TestClient(app)
    r = _post(c, _issue(), "d-1", "issues")
    assert r.status_code == 200, r.text
    assert r.json()["task_id"]
    t = db.query(Task).filter(Task.issue_number == 7).first()
    # Phase 4: tasks are born QUEUED; a worker claims QUEUED -> RUNNING.
    assert t and t.trigger_type == "issue" and t.status == "QUEUED"
    assert t.owner_id is not None
    assert t.repository_id is not None


def test_issue_unconnected_repo_creates_no_task(db):
    """Account-wide App install is not consent: only connected repos trigger."""
    _own(db)  # connected repo is acme/demo
    c = TestClient(app)
    r = _post(c, _issue(number=70, repo="acme/not-connected"), "d-unconn", "issues")
    assert r.status_code == 200, r.text
    assert r.json().get("ignored") == "repository_not_connected"
    assert db.query(Task).count() == 0


def test_issue_repo_row_without_installation_ignored(db):
    u = make_user(db, email="loose@example.com")
    db.add(
        Repository(
            github_full_name="acme/loose", installation_id="", owner_id=u.id
        )
    )
    db.commit()
    c = TestClient(app)
    r = _post(c, _issue(number=71, repo="acme/loose"), "d-loose", "issues")
    assert r.json().get("ignored") == "repository_not_connected"
    assert db.query(Task).count() == 0


def test_issue_installation_mismatch_ignored(db):
    _own(db)  # acme/demo belongs to installation 555
    c = TestClient(app)
    r = _post(c, _issue(number=72, installation_id=999), "d-mismatch", "issues")
    assert r.json().get("ignored") == "installation_mismatch"
    assert db.query(Task).count() == 0


def test_duplicate_delivery_ignored(db):
    _own(db)
    c = TestClient(app)
    payload = _issue(number=8)
    body = json.dumps(payload).encode()
    h = {
        "X-Hub-Signature-256": _sig(body),
        "X-GitHub-Delivery": "d-dup",
        "X-GitHub-Event": "issues",
    }
    assert c.post("/webhooks/github", content=body, headers=h).json()["task_id"]
    r2 = c.post("/webhooks/github", content=body, headers=h)
    assert r2.json().get("duplicate") is True
    assert db.query(Task).filter(Task.issue_number == 8).count() == 1
    assert (
        db.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == "d-dup").count()
        == 1
    )


def test_push_event_ignored(db):
    _own(db)
    c = TestClient(app)
    payload = {
        "ref": "refs/heads/main",
        "installation": {"id": 555},
        "repository": {"full_name": "acme/demo"},
    }
    r = _post(c, payload, "d-push", "push")
    assert r.json().get("ignored") == "push"
    assert db.query(Task).count() == 0


def test_ci_failure_trigger(db):
    _own(db)
    c = TestClient(app)
    r = _post(c, _workflow(), "d-ci", "workflow_run")
    assert r.json()["task_id"]
    t = db.query(Task).filter(Task.ci_run_id == "99").first()
    assert t and t.trigger_type == "ci" and t.ci_sha == "abc123"
    assert t.dedupe_key == "ci:acme/demo:abc123"


def test_ci_success_ignored(db):
    _own(db)
    c = TestClient(app)
    r = _post(
        c, _workflow(run_id=100, sha="x", conclusion="success"), "d-ok", "workflow_run"
    )
    assert r.json().get("ignored")
    assert db.query(Task).count() == 0


def test_ci_failure_on_unconnected_repo_ignored(db):
    _own(db)
    c = TestClient(app)
    r = _post(
        c, _workflow(run_id=101, repo="acme/not-connected"), "d-ci-un", "workflow_run"
    )
    assert r.json().get("ignored") == "repository_not_connected"
    assert db.query(Task).count() == 0


def test_fixhub_repair_branch_ignored(db):
    _own(db)
    c = TestClient(app)
    r = _post(
        c,
        _workflow(run_id=102, sha="repair1", branch="fixhub-fixes/acme-demo-1"),
        "d-repair",
        "workflow_run",
    )
    assert r.json().get("ignored") == "fixhub_branch"
    assert db.query(Task).count() == 0


def test_workflow_allowlist_enforced(db, monkeypatch):
    _own(db)
    monkeypatch.setattr(settings, "CI_WORKFLOW_ALLOWLIST", "ci.yml")
    c = TestClient(app)
    r = _post(
        c,
        _workflow(
            run_id=103,
            sha="deploy1",
            name="Deploy",
            path=".github/workflows/deploy.yml",
        ),
        "d-wf-block",
        "workflow_run",
    )
    assert r.json().get("ignored") == "workflow_not_allowed"
    r = _post(
        c,
        _workflow(run_id=104, sha="ci1", name="CI", path=".github/workflows/ci.yml"),
        "d-wf-allow",
        "workflow_run",
    )
    assert r.json().get("task_id")
    assert db.query(Task).count() == 1


def test_default_branch_policy(db, monkeypatch):
    _own(db)
    monkeypatch.setattr(settings, "CI_BRANCH_POLICY", "default_only")
    c = TestClient(app)
    r = _post(
        c,
        _workflow(run_id=105, sha="feat1", branch="feature/x"),
        "d-branch-block",
        "workflow_run",
    )
    assert r.json().get("ignored") == "branch_not_allowed"
    r = _post(c, _workflow(run_id=106, sha="main1"), "d-branch-ok", "workflow_run")
    assert r.json().get("task_id")


def test_check_run_disabled_by_default(db):
    _own(db)
    c = TestClient(app)
    r = _post(c, _check(), "d-cr-off", "check_run")
    assert r.json().get("ignored") == "check_run_disabled"
    assert db.query(Task).count() == 0


def test_check_run_non_actions_ignored(db, monkeypatch):
    _own(db)
    _checks_on(monkeypatch)
    c = TestClient(app)
    r = _post(c, _check(sha="vercel1", app_slug="vercel"), "d-cr-3p", "check_run")
    assert r.json().get("ignored") == "non_actions_check"
    assert db.query(Task).count() == 0


def test_check_run_enriches_excerpt(db, monkeypatch):
    import app.github.webhook as _wh

    monkeypatch.setattr(
        _wh,
        "_ci_log_excerpt",
        lambda *,
        repo_name,
        run_id,
        check_name: "JOB Lint failed\n  step ruff conclusion=failure",
    )
    _checks_on(monkeypatch)
    _own(db)
    c = TestClient(app)
    r = _post(c, _check(), "d-cr", "check_run")
    assert r.json()["task_id"]
    t = db.query(Task).filter(Task.ci_job == "backend").first()
    assert t and "failing logs" in t.ci_excerpt and "ruff" in t.ci_excerpt


def test_check_run_enrichment_failure_falls_back(db, monkeypatch):
    import app.github.webhook as _wh

    # No installation id / no logs available -> plain one-line excerpt, still triggers.
    monkeypatch.setattr(
        _wh, "_ci_log_excerpt", lambda *, repo_name, run_id, check_name: ""
    )
    _checks_on(monkeypatch)
    # Ownership still required: seed a connected repo + owner row.
    u = make_user(db, email="wh2@example.com")
    db.add(
        Repository(
            github_full_name="acme/demo", installation_id="555", owner_id=u.id
        )
    )
    db.commit()
    c = TestClient(app)
    payload = _check(sha="abc")
    payload.pop("installation")  # repo-owner fallback path
    r = _post(c, payload, "d-cr2", "check_run")
    assert r.json()["task_id"]
    t = db.query(Task).filter(Task.ci_job == "backend").first()
    assert t and t.ci_excerpt == "check backend failed"


def test_run_id_from_url():
    from app.github.webhook import _run_id_from_url

    assert (
        _run_id_from_url(
            "https://github.com/o/r/actions/runs/36275862356/job/108498366867"
        )
        == "36275862356"
    )
    assert _run_id_from_url("") == ""
    assert _run_id_from_url("http://x/99") == ""


def test_ci_duplicate_active_task(db, monkeypatch):
    import app.github.webhook as _wh

    monkeypatch.setattr(
        _wh, "_ci_log_excerpt", lambda *, repo_name, run_id, check_name: ""
    )
    _checks_on(monkeypatch)
    _own(db)
    c = TestClient(app)

    r1 = _post(c, _check(sha="sha9"), "dup-a", "check_run")
    r2 = _post(c, _check(sha="sha9"), "dup-b", "check_run")
    assert r1.json()["task_id"] == r2.json()["task_id"]
    assert r2.json().get("duplicate") == "dedupe_key"
    assert db.query(Task).filter(Task.ci_sha == "sha9").count() == 1


def test_ci_different_jobs_same_sha_one_task(db, monkeypatch):
    """One failing commit = one task, no matter how many jobs report it."""
    import app.github.webhook as _wh

    monkeypatch.setattr(
        _wh, "_ci_log_excerpt", lambda *, repo_name, run_id, check_name: ""
    )
    _checks_on(monkeypatch)
    _own(db)
    c = TestClient(app)
    for delivery, job in (("dj-1", "backend"), ("dj-2", "frontend")):
        _post(c, _check(sha="sha10", job=job), delivery, "check_run")
    assert db.query(Task).filter(Task.ci_sha == "sha10").count() == 1


def test_workflow_and_check_run_same_sha_one_task(db, monkeypatch):
    import app.github.webhook as _wh

    monkeypatch.setattr(
        _wh, "_ci_log_excerpt", lambda *, repo_name, run_id, check_name: ""
    )
    _checks_on(monkeypatch)
    _own(db)
    c = TestClient(app)
    r1 = _post(c, _workflow(run_id=200, sha="same1"), "mix-1", "workflow_run")
    r2 = _post(c, _check(sha="same1", job="sandbox"), "mix-2", "check_run")
    assert r1.json()["task_id"] == r2.json()["task_id"]
    assert db.query(Task).filter(Task.ci_sha == "same1").count() == 1


def test_new_sha_creates_second_task(db):
    _own(db)
    c = TestClient(app)
    _post(c, _workflow(run_id=201, sha="one"), "new-1", "workflow_run")
    _post(c, _workflow(run_id=202, sha="two"), "new-2", "workflow_run")
    assert db.query(Task).count() == 2


def test_bad_signature_rejected(db):
    c = TestClient(app)
    r = c.post(
        "/webhooks/github",
        content=b"{}",
        headers={
            "X-Hub-Signature-256": "sha256=nope",
            "X-GitHub-Delivery": "d-x",
            "X-GitHub-Event": "issues",
        },
    )
    assert r.status_code == 401
