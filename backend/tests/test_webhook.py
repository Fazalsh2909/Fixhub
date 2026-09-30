"""Webhook signature, issue/CI triggers, duplicate prevention, task creation."""
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.config import settings
from app.db.models import Task, WebhookDelivery
from app.github.webhook import verify_signature
from app.main import app


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def test_verify_signature_ok_and_bad():
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("test-secret", body, good) is True
    assert verify_signature("test-secret", body, "sha256=deadbeef") is False
    assert verify_signature("", body, good) is False
    assert verify_signature("test-secret", body, "") is False


def test_issue_trigger_creates_task(db):
    c = TestClient(app)
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/demo"},
        "issue": {"number": 7, "title": "login broken", "body": "500 on login", "html_url": "http://x/7", "labels": []},
    }
    body = json.dumps(payload).encode()
    r = c.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": _sig(body), "X-GitHub-Delivery": "d-1", "X-GitHub-Event": "issues"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["task_id"]
    t = db.query(Task).filter(Task.issue_number == 7).first()
    assert t and t.trigger_type == "issue" and t.status == "RUNNING"


def test_duplicate_delivery_ignored(db):
    c = TestClient(app)
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/demo"},
        "issue": {"number": 8, "title": "t", "body": "b", "html_url": "", "labels": []},
    }
    body = json.dumps(payload).encode()
    h = {"X-Hub-Signature-256": _sig(body), "X-GitHub-Delivery": "d-dup", "X-GitHub-Event": "issues"}
    assert c.post("/webhooks/github", content=body, headers=h).json()["task_id"]
    r2 = c.post("/webhooks/github", content=body, headers=h)
    assert r2.json().get("duplicate") is True
    assert db.query(Task).filter(Task.issue_number == 8).count() == 1
    assert db.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == "d-dup").count() == 1


def test_ci_failure_trigger(db):
    c = TestClient(app)
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/demo"},
        "workflow_run": {"id": 99, "name": "CI", "conclusion": "failure", "head_sha": "abc123", "head_branch": "main", "html_url": "http://x/99"},
    }
    body = json.dumps(payload).encode()
    r = c.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": _sig(body), "X-GitHub-Delivery": "d-ci", "X-GitHub-Event": "workflow_run"},
    )
    assert r.json()["task_id"]
    t = db.query(Task).filter(Task.ci_run_id == "99").first()
    assert t and t.trigger_type == "ci" and t.ci_sha == "abc123"


def test_ci_success_ignored(db):
    c = TestClient(app)
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/demo"},
        "workflow_run": {"id": 100, "name": "CI", "conclusion": "success", "head_sha": "x", "head_branch": "main", "html_url": ""},
    }
    body = json.dumps(payload).encode()
    r = c.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": _sig(body), "X-GitHub-Delivery": "d-ok", "X-GitHub-Event": "workflow_run"},
    )
    assert r.json().get("ignored")
    assert db.query(Task).count() == 0


def test_check_run_enriches_excerpt(db, monkeypatch):
    import app.github.webhook as _wh

    monkeypatch.setattr(_wh, "_ci_log_excerpt",
                        lambda *, repo_name, run_id, check_name: "JOB Lint failed\n  step ruff conclusion=failure")
    c = TestClient(app)
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/demo"},
        "check_run": {"conclusion": "failure", "head_sha": "def456", "name": "backend",
                      "html_url": "http://x/actions/runs/4242/job/99"},
    }
    body = json.dumps(payload).encode()
    r = c.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": _sig(body), "X-GitHub-Delivery": "d-cr", "X-GitHub-Event": "check_run"},
    )
    assert r.json()["task_id"]
    t = db.query(Task).filter(Task.ci_job == "backend").first()
    assert t and "failing logs" in t.ci_excerpt and "ruff" in t.ci_excerpt


def test_check_run_enrichment_failure_falls_back(db, monkeypatch):
    import app.github.webhook as _wh

    # No installation id / no logs available -> plain one-line excerpt, still triggers.
    monkeypatch.setattr(_wh, "_ci_log_excerpt",
                        lambda *, repo_name, run_id, check_name: "")
    c = TestClient(app)
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/demo"},
        "check_run": {"conclusion": "failure", "head_sha": "abc", "name": "backend", "html_url": ""},
    }
    body = json.dumps(payload).encode()
    r = c.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": _sig(body), "X-GitHub-Delivery": "d-cr2", "X-GitHub-Event": "check_run"},
    )
    assert r.json()["task_id"]
    t = db.query(Task).filter(Task.ci_job == "backend").first()
    assert t and t.ci_excerpt == "check backend failed"


def test_run_id_from_url():
    from app.github.webhook import _run_id_from_url

    assert _run_id_from_url("https://github.com/o/r/actions/runs/36275862356/job/108498366867") == "36275862356"
    assert _run_id_from_url("") == ""
    assert _run_id_from_url("http://x/99") == ""


def test_ci_duplicate_active_task(db, monkeypatch):
    import app.github.webhook as _wh

    monkeypatch.setattr(_wh, "_ci_log_excerpt",
                        lambda *, repo_name, run_id, check_name: "")
    c = TestClient(app)

    def _post(delivery):
        payload = {
            "action": "completed",
            "repository": {"full_name": "acme/demo"},
            "check_run": {"conclusion": "failure", "head_sha": "sha9", "name": "backend",
                          "html_url": "http://x/actions/runs/1/job/1"},
        }
        body = json.dumps(payload).encode()
        return c.post(
            "/webhooks/github",
            content=body,
            headers={"X-Hub-Signature-256": _sig(body), "X-GitHub-Delivery": delivery,
                     "X-GitHub-Event": "check_run"},
        )

    r1 = _post("dup-a")
    r2 = _post("dup-b")
    assert r1.json()["task_id"] == r2.json()["task_id"]
    assert r2.json().get("duplicate") == "active_task"
    assert db.query(Task).filter(Task.ci_sha == "sha9").count() == 1


def test_ci_different_job_creates_new_task(db, monkeypatch):
    import app.github.webhook as _wh

    monkeypatch.setattr(_wh, "_ci_log_excerpt",
                        lambda *, repo_name, run_id, check_name: "")
    c = TestClient(app)
    for delivery, job in (("dj-1", "backend"), ("dj-2", "frontend")):
        payload = {
            "action": "completed",
            "repository": {"full_name": "acme/demo"},
            "check_run": {"conclusion": "failure", "head_sha": "sha10", "name": job,
                          "html_url": "http://x/actions/runs/1/job/1"},
        }
        body = json.dumps(payload).encode()
        c.post(
            "/webhooks/github",
            content=body,
            headers={"X-Hub-Signature-256": _sig(body), "X-GitHub-Delivery": delivery,
                     "X-GitHub-Event": "check_run"},
        )
    assert db.query(Task).filter(Task.ci_sha == "sha10").count() == 2


def test_bad_signature_rejected(db):
    c = TestClient(app)
    r = c.post(
        "/webhooks/github",
        content=b"{}",
        headers={"X-Hub-Signature-256": "sha256=nope", "X-GitHub-Delivery": "d-x", "X-GitHub-Event": "issues"},
    )
    assert r.status_code == 401
