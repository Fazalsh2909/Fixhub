"""Queue (Phase 1B): enqueue fallback, job wrapper, webhook auto-enqueue gate."""
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.config import settings as _settings
from app.db.models import Task, TaskEvent
from app.main import app
from app.tasks import queue as _queue


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def test_enqueue_fallback_when_no_redis(monkeypatch):
    monkeypatch.setattr(_queue, "get_queue", lambda: None)
    out = _queue.enqueue_task(123)
    assert out["enqueued"] is False
    assert "redis" in out["error"].lower()


def test_job_failed_marks_task_failed(db):
    from types import SimpleNamespace

    db.add(Task(repository="acme/queue", trigger_type="issue", issue_number=90,
                issue_title="t", issue_body="b", status="RUNNING"))
    db.commit()
    t = db.query(Task).filter(Task.issue_number == 90).first()
    _queue.job_failed(SimpleNamespace(args=[t.id]), RuntimeError("no such table: tasks"))
    db.expire_all()
    t2 = db.query(Task).filter(Task.id == t.id).first()
    assert t2.status == "FAILED"
    assert "worker job crashed" in t2.error
    assert db.query(TaskEvent).filter(TaskEvent.task_id == t.id,
                                      TaskEvent.type == "FAILED").count() == 1


def test_job_failed_rq_five_arg_convention(db):
    """RQ >= 1.x calls on_failure(job, conn, exc_type, exc_value, tb)."""
    from types import SimpleNamespace

    db.add(Task(repository="acme/queue", trigger_type="ci", ci_sha="abc",
                issue_title="", issue_body="", status="RUNNING"))
    db.commit()
    t = db.query(Task).filter(Task.ci_sha == "abc").first()
    err = RuntimeError("boom")
    _queue.job_failed(SimpleNamespace(args=[t.id]), None, None, err, None)
    db.expire_all()
    t2 = db.query(Task).filter(Task.id == t.id).first()
    assert t2.status == "FAILED"
    assert "boom" in t2.error


def test_run_task_job_calls_service(monkeypatch):
    called = {}

    def fake_inline(task_id, source="", base="main"):
        called["task_id"] = task_id
        return {"status": "COMPLETED"}

    import app.tasks.service as _svc

    monkeypatch.setattr(_svc, "run_task_inline", fake_inline)
    out = _queue.run_task_job(7)
    assert out == {"status": "COMPLETED"}
    assert called["task_id"] == 7


def test_queue_health_no_redis(monkeypatch):
    monkeypatch.setattr(_queue, "get_queue", lambda: None)
    out = _queue.queue_health()
    assert out["ok"] is False


def _post_issue(c, delivery="q-1", number=21):
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/queue"},
        "issue": {"number": number, "title": "t", "body": "b", "html_url": "", "labels": []},
    }
    body = json.dumps(payload).encode()
    return c.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": _sig(body), "X-GitHub-Delivery": delivery, "X-GitHub-Event": "issues"},
    )


def test_webhook_auto_enqueues(db, monkeypatch):
    monkeypatch.setattr(_settings, "AUTO_RUN_ON_WEBHOOK", 1)
    monkeypatch.setattr(_queue, "enqueue_task", lambda task_id, **kw: {"enqueued": True, "job_id": "j1"})
    c = TestClient(app)
    r = _post_issue(c, delivery="q-enq", number=31)
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is True
    t = db.query(Task).filter(Task.issue_number == 31).first()
    assert t is not None
    types = [e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == t.id).all()]
    assert "QUEUED" in types


def test_webhook_disabled_skips_enqueue(db, monkeypatch):
    monkeypatch.setattr(_settings, "AUTO_RUN_ON_WEBHOOK", 0)
    called = {"n": 0}

    def fake_enqueue(task_id, **kw):
        called["n"] += 1
        return {"enqueued": True}

    monkeypatch.setattr(_queue, "enqueue_task", fake_enqueue)
    c = TestClient(app)
    r = _post_issue(c, delivery="q-off", number=32)
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is False
    assert called["n"] == 0


def test_run_endpoint_enqueues_by_default(db, monkeypatch):
    from app.db.models import Repository

    db.add(Repository(github_full_name="acme/queue", installation_id=""))
    db.add(Task(repository="acme/queue", trigger_type="issue", issue_number=40,
                issue_title="t", issue_body="b", status="RUNNING"))
    db.commit()
    t = db.query(Task).filter(Task.issue_number == 40).first()
    monkeypatch.setattr(_queue, "enqueue_task", lambda task_id, **kw: {"enqueued": True, "job_id": "jj"})
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/run")
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is True


def test_run_endpoint_sync_falls_back_inline(db, monkeypatch):
    from app.db.models import Repository

    db.add(Repository(github_full_name="acme/queue", installation_id=""))
    db.add(Task(repository="acme/queue", trigger_type="issue", issue_number=41,
                issue_title="t", issue_body="b", status="RUNNING"))
    db.commit()
    t = db.query(Task).filter(Task.issue_number == 41).first()
    import app.tasks.service as _svc

    monkeypatch.setattr(_svc, "run_task_inline",
                        lambda task_id, **kw: {"status": "BLOCKED", "error": "no source"})
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/run?sync=true")
    assert r.status_code == 200, r.text
    assert r.json()["sync"] is True
