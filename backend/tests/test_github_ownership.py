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
    """A connects a verified installation: visible to A, invisible to B."""
    from tests.conftest import make_github_identity

    from app.github import client as _gh

    ua = make_user(db, email="ga@example.com")
    ca = session_cookies(db, ua)
    ub = make_user(db, email="gb@example.com")
    cb = session_cookies(db, ub)
    # GitHub-truthful identities: A verified for inst-1 (acme), B for inst-2.
    make_github_identity(
        db, ua, github_user_id=701, login="acme", token="tok-ghu-a",
        installations=("inst-1",),
    )
    make_github_identity(
        db, ub, github_user_id=702, login="other", token="tok-ghu-b",
        installations=("inst-2",),
    )
    monkeypatch.setattr(
        _gh,
        "list_user_installations",
        lambda *, token: (
            [{"id": "inst-1", "account": "acme", "type": "User"}]
            if token == "tok-ghu-a"
            else [{"id": "inst-2", "account": "other", "type": "User"}]
        ),
    )
    monkeypatch.setattr(
        _gh,
        "list_user_installation_repos",
        lambda *, token, installation_id: (
            [{"full_name": "acme/repo", "private": False, "default_branch": "main"}]
            if token == "tok-ghu-a"
            else []
        ),
    )
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


def _fail_no_mint(monkeypatch):
    """Installation tokens must never be minted from these request paths."""
    from app.github import app_auth as _auth

    def _boom(iid):
        raise AssertionError(f"installation token minted for {iid!r} without verification")

    monkeypatch.setattr(_auth, "installation_token", _boom)


def test_spoofed_installation_id_rejected(db, monkeypatch):
    """B (verified for inst-9's neighbor only) cannot claim inst-9 by id;
    the pre-fix 'connect your installation_id' theft is dead."""
    from tests.conftest import make_github_identity

    from app.github import client as _gh

    ua = make_user(db, email="gc@example.com")
    ub = make_user(db, email="gd@example.com")
    cb = session_cookies(db, ub)
    make_github_identity(
        db, ua, github_user_id=801, login="acme", token="tok-ghu-a",
        installations=("inst-9",),
    )
    make_github_identity(
        db, ub, github_user_id=802, login="mallory", token="tok-ghu-b",
        installations=("inst-m",),
    )
    db.add(
        Repository(
            github_full_name="acme/claimed",
            installation_id="inst-9",
            owner_id=ua.id,
            github_user_id=801,
        )
    )
    db.commit()
    monkeypatch.setattr(
        _gh,
        "list_user_installations",
        lambda *, token: (
            [{"id": "inst-9", "account": "acme", "type": "User"}]
            if token == "tok-ghu-a"
            else [{"id": "inst-m", "account": "mallory", "type": "User"}]
        ),
    )
    monkeypatch.setattr(
        _gh, "list_user_installation_repos", lambda *, token, installation_id: []
    )
    _fail_no_mint(monkeypatch)
    c = TestClient(app)
    # B tries to steal the repo row (globally unique full_name): 404.
    r = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "acme/claimed", "installation_id": "inst-9"},
        cookies=cb,
    )
    assert r.status_code == 404, r.text
    # B tries to claim inst-9 on a fresh repo by changing installation_id: 404.
    r = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "acme/other", "installation_id": "inst-9"},
        cookies=cb,
    )
    assert r.status_code == 404, r.text
    # Nothing was created for B and nothing changed for A.
    assert db.query(Repository).filter(Repository.github_full_name == "acme/other").first() is None
    claimed = db.query(Repository).filter(Repository.github_full_name == "acme/claimed").first()
    assert claimed.owner_id == ua.id


def test_connect_requires_verified_github_identity(db):
    """Without the OAuth flow, connect explicitly asks for authorization."""
    from tests.conftest import make_github_identity

    ua = make_user(db, email="uj@example.com")
    ub = make_user(db, email="uk@example.com")
    cb = session_cookies(db, ub)
    make_github_identity(
        db, ua, github_user_id=901, login="owner", token="tok-ghu-a",
        installations=("inst-5",),
    )
    db.add(Repository(github_full_name="own/locked", installation_id="inst-5", owner_id=ua.id))
    db.commit()
    c = TestClient(app)
    r = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "own/locked", "installation_id": "inst-5"},
        cookies=cb,
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["github_authorization_required"] is True
    assert body["start_url"] == "/api/github/install/start"
    # The locked repo row stays untouched by the failed attempt.
    assert db.query(Repository).filter(Repository.github_full_name == "own/locked").first().owner_id == ua.id


def test_webhook_installation_creates_owned_task(db):
    ua = make_user(db, email="ge@example.com")
    db.add(GitHubConnection(user_id=ua.id, installation_id="4242"))
    db.add(
        Repository(
            github_full_name="acme/hooked", installation_id="4242", owner_id=ua.id
        )
    )
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
    assert r.json().get("ignored") == "repository_not_connected"
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
    assert r.json().get("ignored") == "repository_not_connected"
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
