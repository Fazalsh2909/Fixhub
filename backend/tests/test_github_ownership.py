"""Phase 2 GitHub ownership tests: user->installation->repository mapping.

- A connects installation A: visible to A, invisible to B.
- B cannot claim installation A by submitting its ID.
- Webhook for installation A creates a task owned by A.
- Webhook for unknown/unconnected installation creates no task.
"""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.db.models import GitHubConnection, Repository, Task
from app.main import app
from tests.conftest import make_user, session_cookies


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def _post_webhook(c, payload, delivery, event="issues"):
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


def test_connection_visible_to_owner_only(db, monkeypatch):
    from app.github import app_auth as _auth
    from app.github import client as _gh

    monkeypatch.setattr(_auth, "installation_token", lambda iid: "tok")
    monkeypatch.setattr(
        _gh,
        "list_installations",
        lambda app_jwt: [{"id": "inst-1", "account": "acme", "type": "Organization"}],
    )
    monkeypatch.setattr(
        _gh,
        "list_installation_repos",
        lambda token: [
            {"full_name": "acme/repo", "private": False, "default_branch": "main"}
        ],
    )
    ua = make_user(db, email="ga@example.com")
    ca = session_cookies(db, ua)
    ub = make_user(db, email="gb@example.com")
    cb = session_cookies(db, ub)
    c = TestClient(app)
    r = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "acme/repo", "installation_id": "inst-1"},
        cookies=ca,
    )
    assert r.status_code == 200, r.text
    names_a = [
        x["github_full_name"] for x in c.get("/api/repositories", cookies=ca).json()
    ]
    names_b = [
        x["github_full_name"] for x in c.get("/api/repositories", cookies=cb).json()
    ]
    assert "acme/repo" in names_a
    assert "acme/repo" not in names_b


def test_cannot_claim_anothers_installation(db):
    ua = make_user(db, email="gc@example.com")
    ub = make_user(db, email="gd@example.com")
    cb = session_cookies(db, ub)
    db.add(GitHubConnection(user_id=ua.id, installation_id="inst-9"))
    db.add(
        Repository(
            github_full_name="acme/claimed", installation_id="inst-9", owner_id=ua.id
        )
    )
    db.commit()
    c = TestClient(app)
    # B tries to steal the repo row.
    r = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "acme/claimed", "installation_id": "inst-9"},
        cookies=cb,
    )
    assert r.status_code == 404, r.text
    # B tries to claim the installation id on a fresh repo.
    r = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "acme/other", "installation_id": "inst-9"},
        cookies=cb,
    )
    assert r.status_code == 404, r.text


def test_webhook_installation_creates_owned_task(db):
    ua = make_user(db, email="ge@example.com")
    db.add(GitHubConnection(user_id=ua.id, installation_id="4242"))
    db.commit()
    c = TestClient(app)
    payload = {
        "action": "opened",
        "installation": {"id": 4242},
        "repository": {"full_name": "acme/hooked"},
        "issue": {
            "number": 11,
            "title": "t",
            "body": "b",
            "html_url": "",
            "labels": [],
        },
    }
    r = _post_webhook(c, payload, "d-own-1")
    assert r.status_code == 200, r.text
    assert r.json()["task_id"]
    t = db.query(Task).filter(Task.issue_number == 11).first()
    assert t and t.owner_id == ua.id


def test_webhook_unknown_installation_creates_no_task(db):
    make_user(db, email="gf@example.com")
    c = TestClient(app)
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/stranger"},
        "issue": {
            "number": 12,
            "title": "t",
            "body": "b",
            "html_url": "",
            "labels": [],
        },
    }
    r = _post_webhook(c, payload, "d-own-2")
    assert r.status_code == 200, r.text
    assert r.json().get("ignored") == "unconnected_installation"
    assert db.query(Task).filter(Task.issue_number == 12).count() == 0


def test_webhook_ci_unknown_installation_creates_no_task(db):
    make_user(db, email="gg@example.com")
    c = TestClient(app)
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/ci-stranger"},
        "workflow_run": {
            "id": 4243,
            "name": "CI",
            "conclusion": "failure",
            "head_sha": "zzz",
            "head_branch": "main",
            "html_url": "",
        },
    }
    r = _post_webhook(c, payload, "d-own-3", event="workflow_run")
    assert r.status_code == 200, r.text
    assert r.json().get("ignored") == "unconnected_installation"
    assert db.query(Task).filter(Task.ci_run_id == "4243").count() == 0


def test_webhook_repo_owner_fallback(db):
    # No connection row, but the repo row already has an owner (adopted
    # pre-auth repo): the task still lands with that user.
    ua = make_user(db, email="gh@example.com")
    db.add(
        Repository(
            github_full_name="acme/legacy", installation_id="inst-old", owner_id=ua.id
        )
    )
    db.commit()
    c = TestClient(app)
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/legacy"},
        "issue": {
            "number": 13,
            "title": "t",
            "body": "b",
            "html_url": "",
            "labels": [],
        },
    }
    r = _post_webhook(c, payload, "d-own-4")
    assert r.json().get("task_id"), r.text
    t = db.query(Task).filter(Task.issue_number == 13).first()
    assert t and t.owner_id == ua.id
