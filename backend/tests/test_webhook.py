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
    payload = {
        "action": "opened",
        "issue": {"labels": [], "number": 1},
        "repository": {"full_name": "a/b"},
    }
    ok, _ = _wants_fix("issues", payload)
    assert ok is False


def test_fix_comment_trigger():
    payload = {
        "comment": {"body": "/fix please"},
        "issue": {"number": 2},
        "repository": {"full_name": "a/b"},
    }
    ok, reason = _wants_fix("issue_comment", payload)
    assert ok is True and reason == "fix-comment"


def test_auto_trigger_unlabeled_open_on_connected_repo(monkeypatch):
    """Original idea: any issue on a connected repo starts the agent, no label needed."""
    import app.github.webhook as wh

    init_db()
    db = SessionLocal()
    name = f"demo/auto-{uuid.uuid4().hex[:8]}"
    repo = Repository(full_name=name, connected=True)
    db.add(repo)
    db.commit()
    db.close()
    monkeypatch.setattr(wh.settings, "auto_trigger_on_issue", True)
    monkeypatch.setattr(wh, "launch_task", lambda *a, **k: "started")

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
