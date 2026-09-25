"""Webhook signature + idempotency + auto-trigger tests."""

import hashlib
import hmac
import json
import uuid

from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, init_db
from app.github.webhook import _wants_fix, verify_signature
from app.main import create_app
from app.models import Repository

app = create_app()
client = TestClient(app, raise_server_exceptions=False)


def test_verify_signature_roundtrip():
    import hashlib
    import hmac

    secret = "s3cret"
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, secret) is True
    assert verify_signature(body, "sha256=dead", secret) is False


def test_wants_fix_on_label():
    payload = {
        "action": "labeled",
        "label": {"name": "fixhub-fix"},
        "issue": {"number": 1},
        "repository": {"full_name": "a/b"},
    }
    ok, _ = _wants_fix("issues", payload)
    assert ok is True


def test_ignores_unlabeled_open():
    # Simple flow: any opened issue is trigger intent; connectedness is
    # checked in the handler (needs DB), not in _wants_fix.
    payload = {
        "action": "opened",
        "issue": {"labels": [], "number": 1},
        "repository": {"full_name": "a/b"},
    }
    ok, reason = _wants_fix("issues", payload)
    assert ok is True
    assert "auto-opened" in reason


def test_fix_comment_trigger():
    payload = {
        "comment": {"body": "/fix please"},
        "issue": {"number": 2},
        "repository": {"full_name": "a/b"},
    }
    ok, reason = _wants_fix("issue_comment", payload)
    assert ok is True and reason == "fix-comment"


def test_auto_trigger_unlabeled_open_on_connected_repo(monkeypatch):
    """Simple flow: any opened issue on a connected repo starts a RUNNING run."""
    import app.github.webhook as wh

    init_db()
    db = SessionLocal()
    name = f"demo/auto-{uuid.uuid4().hex[:8]}"
    repo = Repository(full_name=name, connected=True)
    db.add(repo)
    db.commit()
    db.close()
    monkeypatch.setattr(wh.settings, "auto_trigger_on_issue", True)

    payload = {
        "action": "opened",
        "issue": {"number": 7, "title": "login redirect loop", "labels": []},
        "repository": {"full_name": name},
        "installation": {"id": 123},
    }
    raw = json.dumps(payload).encode()
    sig = (
        "sha256="
        + hmac.new(
            settings.github_webhook_secret.encode(), raw, hashlib.sha256
        ).hexdigest()
    )
    r = client.post(
        "/webhooks/github",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": f"auto-{uuid.uuid4().hex[:8]}",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["task_id"] is not None


def _signed_post(payload: dict, event: str = "issues"):
    raw = json.dumps(payload).encode()
    sig = (
        "sha256="
        + hmac.new(
            settings.github_webhook_secret.encode(), raw, hashlib.sha256
        ).hexdigest()
    )
    return client.post(
        "/webhooks/github",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": f"skip-{uuid.uuid4().hex[:8]}",
        },
    )


def test_skipped_delivery_says_why_not_connected(monkeypatch):
    """Silent no-ops confused users: an unconnected repo gets triggered:false
    plus the exact reason instead of a bare null task_id."""
    import app.github.webhook as wh

    init_db()
    db = SessionLocal()
    name = f"demo/skip-{uuid.uuid4().hex[:8]}"
    db.add(Repository(full_name=name, connected=False))
    db.commit()
    db.close()
    monkeypatch.setattr(wh.settings, "auto_trigger_on_issue", True)
    r = _signed_post(
        {
            "action": "opened",
            "issue": {"number": 3, "title": "t", "labels": []},
            "repository": {"full_name": name},
            "installation": {"id": 1},
        }
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] is None
    assert body["triggered"] is False
    assert "not connected" in body["reason"]


def test_skipped_delivery_says_why_flag_off(monkeypatch):
    import app.github.webhook as wh

    init_db()
    db = SessionLocal()
    name = f"demo/skipoff-{uuid.uuid4().hex[:8]}"
    db.add(Repository(full_name=name, connected=True))
    db.commit()
    db.close()
    monkeypatch.setattr(wh.settings, "auto_trigger_on_issue", False)
    r = _signed_post(
        {
            "action": "opened",
            "issue": {"number": 4, "title": "t", "labels": []},
            "repository": {"full_name": name},
            "installation": {"id": 1},
        }
    )
    assert r.json()["triggered"] is False
    assert "AUTO_TRIGGER_ON_ISSUE" in r.json()["reason"]


def _comment_post(
    payload: dict, delivery_suffix: str = "c1", event: str = "issue_comment"
):
    raw = json.dumps(payload).encode()
    sig = (
        "sha256="
        + hmac.new(
            settings.github_webhook_secret.encode(), raw, hashlib.sha256
        ).hexdigest()
    )
    return client.post(
        "/webhooks/github",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": f"cmt-{delivery_suffix}-{uuid.uuid4().hex[:8]}",
        },
    )


def _connected_repo(name: str):
    from app.models import Repository as _R

    init_db()
    db = SessionLocal()
    repo = _R(full_name=name, connected=True)
    db.add(repo)
    db.commit()
    rid = repo.id
    db.close()
    return rid


def test_bot_comment_never_triggers(monkeypatch):
    import app.github.webhook as wh

    name = f"demo/bot-{uuid.uuid4().hex[:8]}"
    _connected_repo(name)
    monkeypatch.setattr(wh.settings, "auto_trigger_on_issue", True)
    r = _comment_post(
        {
            "action": "created",
            "issue": {"number": 1, "title": "t"},
            "comment": {
                "body": "I tried to fix #1 but ...",
                "user": {"login": "fixhub[bot]", "type": "Bot"},
            },
            "repository": {"full_name": name},
        }
    )
    assert r.status_code == 200, r.text
    assert r.json()["triggered"] is False


def test_human_reply_after_ask_back_retriggers(monkeypatch):
    """Agent asked → human replies → new RUNNING run carrying the reply."""
    import app.github.webhook as wh
    from app.models import ChatMessage, Task, TaskEvent

    name = f"demo/reply-{uuid.uuid4().hex[:8]}"
    rid = _connected_repo(name)
    monkeypatch.setattr(wh.settings, "auto_trigger_on_issue", True)
    db = SessionLocal()
    t = Task(repo_id=rid, issue_number=5, title="mystery", state="COMPLETED")
    db.add(t)
    db.flush()
    db.add(TaskEvent(task_id=t.id, stage="COMMENT_POSTED", message="please clarify"))
    db.commit()
    db.close()
    r = _comment_post(
        {
            "action": "created",
            "issue": {"number": 5, "title": "mystery", "body": "it breaks"},
            "comment": {
                "body": "expected 200, got 500 on /login",
                "user": {"login": "human", "type": "User"},
            },
            "repository": {"full_name": name},
        },
        delivery_suffix="reply1",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["triggered"] is True, body
    db2 = SessionLocal()
    try:
        nt = db2.query(Task).filter_by(id=body["task_id"]).first()
        assert nt.state == "RUNNING"
        msgs = [m.content for m in db2.query(ChatMessage).filter_by(task_id=nt.id)]
        assert any("expected 200" in m for m in msgs)
    finally:
        db2.close()


def test_comment_ignored_without_prior_ask_back(monkeypatch):
    import app.github.webhook as wh

    name = f"demo/noprior-{uuid.uuid4().hex[:8]}"
    _connected_repo(name)
    monkeypatch.setattr(wh.settings, "auto_trigger_on_issue", True)
    r = _comment_post(
        {
            "action": "created",
            "issue": {"number": 2, "title": "t"},
            "comment": {
                "body": "+1 same here",
                "user": {"login": "human", "type": "User"},
            },
            "repository": {"full_name": name},
        },
        delivery_suffix="noprior",
    )
    assert r.json()["triggered"] is False


def test_comment_ignored_after_pr_exists(monkeypatch):
    import app.github.webhook as wh
    from app.models import PullRequest, Task, TaskEvent

    name = f"demo/prdone-{uuid.uuid4().hex[:8]}"
    rid = _connected_repo(name)
    monkeypatch.setattr(wh.settings, "auto_trigger_on_issue", True)
    db = SessionLocal()
    t = Task(repo_id=rid, issue_number=3, title="done", state="COMPLETED")
    db.add(t)
    db.flush()
    db.add(TaskEvent(task_id=t.id, stage="COMMENT_POSTED", message="q?"))
    db.add(PullRequest(task_id=t.id, url="http://x/pr/9", number=9, commit_sha="abc"))
    db.commit()
    db.close()
    r = _comment_post(
        {
            "action": "created",
            "issue": {"number": 3, "title": "done"},
            "comment": {"body": "thanks!", "user": {"login": "human", "type": "User"}},
            "repository": {"full_name": name},
        },
        delivery_suffix="prdone",
    )
    assert r.json()["triggered"] is False


def test_from_issue_stores_body_and_runs(monkeypatch):
    """from-issue keeps the fetched body+comments and starts RUNNING."""
    import app.github.api as gapi
    from app.models import ChatMessage, Task

    name = f"demo/fromissue-{uuid.uuid4().hex[:8]}"
    _connected_repo(name)

    class _FakeClient:
        def get_issue(self, full_name, number):
            assert (full_name, number) == (name, 9)
            return {"title": "Real title here", "body": "Real body with repro steps"}

        def get_issue_comments(self, full_name, number):
            return [{"user": {"login": "r"}, "body": "more context"}]

    monkeypatch.setattr(gapi, "_client_for", lambda inst: _FakeClient())
    r = client.post(
        "/api/github/from-issue",
        json={"full_name": name, "issue_number": 9, "installation_id": "inst-1"},
    )
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    db = SessionLocal()
    try:
        t = db.query(Task).filter_by(id=tid).first()
        assert t.state == "RUNNING"
        assert t.title == "Real title here"
        bodies = " ".join(
            m.content for m in db.query(ChatMessage).filter_by(task_id=tid)
        )
        assert "Real body with repro steps" in bodies
        assert "more context" in bodies
    finally:
        db.close()


def test_thin_issue_still_creates_simple_run(monkeypatch):
    """Simple flow: even a thin issue creates a RUNNING run — the agent
    itself decides INSUFFICIENT_INFO; the webhook never gates on content."""
    import app.github.webhook as wh
    from app.models import Task

    init_db()
    db = SessionLocal()
    name = f"demo/thin-{uuid.uuid4().hex[:8]}"
    db.add(Repository(full_name=name, connected=True))
    db.commit()
    db.close()
    monkeypatch.setattr(wh.settings, "auto_trigger_on_issue", True)
    r = _signed_post(
        {
            "action": "opened",
            "issue": {
                "number": 9,
                "title": "https://github.com/example/repo",
                "labels": [],
            },
            "repository": {"full_name": name},
            "installation": {"id": 1},
        }
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] is not None
    assert body["triggered"] is True
    db2 = SessionLocal()
    try:
        task = db2.query(Task).filter_by(id=body["task_id"]).first()
        assert task.state == "RUNNING"
    finally:
        from app.models import TaskEvent

        db2.query(TaskEvent).filter_by(task_id=body["task_id"]).delete(
            synchronize_session=False
        )
        db2.query(Task).filter_by(id=body["task_id"]).delete(synchronize_session=False)
        db2.query(Repository).filter_by(full_name=name).delete(
            synchronize_session=False
        )
        db2.commit()
        db2.close()
