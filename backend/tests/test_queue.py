"""Queue (Phase 1B): enqueue fallback, job wrapper, webhook auto-enqueue gate."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.config import settings as _settings
from app.db.models import GitHubConnection, Task, TaskEvent
from app.main import app
from app.tasks import queue as _queue
from tests.conftest import make_user, session_cookies


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def test_enqueue_fallback_when_no_redis(db, monkeypatch):
    # Phase 4: unknown task fails first (caps/lookup precede Redis contact).
    monkeypatch.setattr(_queue, "get_queue", lambda: None)
    out = _queue.enqueue_task(123)
    assert out["enqueued"] is False
    assert "task not found" in out["error"].lower()


def test_enqueue_redis_down_fails_clearly(db, monkeypatch):
    # Known task + Redis down: clear infrastructure error, never silent inline.
    monkeypatch.setattr(_queue, "get_queue", lambda: None)
    db.add(
        Task(
            repository="acme/queue",
            trigger_type="issue",
            issue_number=91,
            issue_title="t",
            issue_body="b",
            status="QUEUED",
        )
    )
    db.commit()
    t = db.query(Task).filter(Task.issue_number == 91).first()
    out = _queue.enqueue_task(t.id)
    assert out["enqueued"] is False
    assert "redis" in out["error"].lower()


def test_job_failed_marks_task_failed(db):
    from types import SimpleNamespace

    db.add(
        Task(
            repository="acme/queue",
            trigger_type="issue",
            issue_number=90,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
        )
    )
    db.commit()
    t = db.query(Task).filter(Task.issue_number == 90).first()
    _queue.job_failed(
        SimpleNamespace(args=[t.id]), RuntimeError("no such table: tasks")
    )
    db.expire_all()
    t2 = db.query(Task).filter(Task.id == t.id).first()
    assert t2.status == "FAILED"
    assert "worker job crashed" in t2.error
    assert (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == t.id, TaskEvent.type == "FAILED")
        .count()
        == 1
    )


def test_job_failed_rq_five_arg_convention(db):
    """RQ >= 1.x calls on_failure(job, conn, exc_type, exc_value, tb)."""
    from types import SimpleNamespace

    db.add(
        Task(
            repository="acme/queue",
            trigger_type="ci",
            ci_sha="abc",
            issue_title="",
            issue_body="",
            status="RUNNING",
        )
    )
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

    def fake_inline(task_id, source="", base="main", **kw):
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


def _post_issue(c, delivery="q-1", number=21, installation_id=777):
    payload = {
        "action": "opened",
        "installation": {"id": installation_id},
        "repository": {"full_name": "acme/queue"},
        "issue": {
            "number": number,
            "title": "t",
            "body": "b",
            "html_url": "",
            "labels": [],
        },
    }
    body = json.dumps(payload).encode()
    return c.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sig(body),
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": "issues",
        },
    )


def _queue_owner(db, email="q@example.com", installation_id=777, repo="acme/queue"):
    from app.db.models import Repository

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


def test_webhook_auto_enqueues(db, monkeypatch):
    monkeypatch.setattr(
        _queue, "enqueue_task", lambda task_id, **kw: {"enqueued": True, "job_id": "j1"}
    )
    _queue_owner(db)
    c = TestClient(app)
    r = _post_issue(c, delivery="q-enq", number=31)
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is True
    t = db.query(Task).filter(Task.issue_number == 31).first()
    assert t is not None
    types = [
        e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == t.id).all()
    ]
    assert "QUEUED" in types


def test_webhook_ignores_deprecated_auto_run_flag(db, monkeypatch):
    """AUTO_RUN_ON_WEBHOOK=0 used to silently park tasks; creation always starts."""
    monkeypatch.setattr(_settings, "AUTO_RUN_ON_WEBHOOK", 0)
    called = {"n": 0}

    def fake_enqueue(task_id, **kw):
        called["n"] += 1
        return {"enqueued": True, "job_id": "j-off"}

    monkeypatch.setattr(_queue, "enqueue_task", fake_enqueue)
    _queue_owner(db, email="q2@example.com", installation_id=778)
    c = TestClient(app)
    r = _post_issue(c, delivery="q-off", number=32, installation_id=778)
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is True
    assert called["n"] == 1


def test_enqueue_failure_is_visible_on_task(db, monkeypatch):
    monkeypatch.setattr(
        _queue,
        "enqueue_task",
        lambda task_id, **kw: {"enqueued": False, "error": "redis unavailable"},
    )
    _queue_owner(db, email="q5@example.com", installation_id=779)
    c = TestClient(app)
    r = _post_issue(c, delivery="q-fail", number=33, installation_id=779)
    assert r.json()["queued"] is False
    t = db.query(Task).filter(Task.issue_number == 33).first()
    assert t is not None and "not started" in t.error
    types = [
        e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == t.id).all()
    ]
    assert "QUEUE_FAILED" in types


def test_reconciler_requeues_orphaned_queued_task(db, monkeypatch):
    from datetime import timedelta

    from app.db.database import utcnow
    from app.db.models import Repository
    from app.tasks import reconciler as _rec

    u = make_user(db, email="q6@example.com")
    db.add(
        Repository(
            github_full_name="acme/queue", installation_id="780", owner_id=u.id
        )
    )
    t = Task(
        repository="acme/queue",
        trigger_type="issue",
        issue_number=50,
        issue_title="t",
        issue_body="b",
        owner_id=u.id,
        status="QUEUED",
        created_at=utcnow() - timedelta(seconds=120),
    )
    db.add(t)
    db.commit()

    class _FakeQ:
        def fetch_job(self, job_id):
            return None

    monkeypatch.setattr(_queue, "get_queue", lambda: _FakeQ())
    monkeypatch.setattr(
        _queue, "enqueue_task", lambda task_id, **kw: {"enqueued": True, "job_id": "j"}
    )
    out = _rec.requeue_orphaned()
    assert out["requeued"] == 1
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.queue_job_id == "j"
    assert (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == t.id, TaskEvent.type == "QUEUE_RETRIED")
        .count()
        == 1
    )


def test_reconciler_skips_unconnected_repo(db, monkeypatch):
    from datetime import timedelta

    from app.db.database import utcnow
    from app.tasks import reconciler as _rec

    t = Task(
        repository="acme/never-connected",
        trigger_type="ci",
        ci_sha="orphan",
        status="QUEUED",
        created_at=utcnow() - timedelta(seconds=120),
    )
    db.add(t)
    db.commit()
    called = {"n": 0}

    class _FakeQ:
        def fetch_job(self, job_id):
            return None

    def fake_enqueue(task_id, **kw):
        called["n"] += 1
        return {"enqueued": True, "job_id": "j"}

    monkeypatch.setattr(_queue, "get_queue", lambda: _FakeQ())
    monkeypatch.setattr(_queue, "enqueue_task", fake_enqueue)
    out = _rec.requeue_orphaned()
    assert out["requeued"] == 0
    assert called["n"] == 0
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "QUEUED"


def test_reconciler_skips_live_job(db, monkeypatch):
    from datetime import timedelta

    from app.db.database import utcnow
    from app.db.models import Repository
    from app.tasks import reconciler as _rec

    u = make_user(db, email="q7@example.com")
    db.add(
        Repository(
            github_full_name="acme/queue", installation_id="781", owner_id=u.id
        )
    )
    t = Task(
        repository="acme/queue",
        trigger_type="issue",
        issue_number=51,
        issue_title="t",
        issue_body="b",
        owner_id=u.id,
        status="QUEUED",
        created_at=utcnow() - timedelta(seconds=120),
    )
    db.add(t)
    db.commit()

    class _LiveJob:
        def get_status(self, refresh=False):
            return "started"

    class _FakeQ:
        def fetch_job(self, job_id):
            return _LiveJob()

    called = {"n": 0}

    def fake_enqueue(task_id, **kw):
        called["n"] += 1
        return {"enqueued": True, "job_id": "j"}

    monkeypatch.setattr(_queue, "get_queue", lambda: _FakeQ())
    monkeypatch.setattr(_queue, "enqueue_task", fake_enqueue)
    out = _rec.requeue_orphaned()
    assert out["requeued"] == 0
    assert called["n"] == 0


def test_run_endpoint_enqueues_by_default(db, monkeypatch):
    from app.db.models import Repository

    u = make_user(db, email="q3@example.com")
    cookies = session_cookies(db, u)
    db.add(Repository(github_full_name="acme/queue", installation_id="", owner_id=u.id))
    db.add(
        Task(
            repository="acme/queue",
            trigger_type="issue",
            issue_number=40,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            owner_id=u.id,
        )
    )
    db.commit()
    t = db.query(Task).filter(Task.issue_number == 40).first()
    monkeypatch.setattr(
        _queue, "enqueue_task", lambda task_id, **kw: {"enqueued": True, "job_id": "jj"}
    )
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/run", cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is True


def test_run_endpoint_sync_falls_back_inline(db, monkeypatch):
    from app.db.models import Repository

    u = make_user(db, email="q4@example.com")
    cookies = session_cookies(db, u)
    db.add(Repository(github_full_name="acme/queue", installation_id="", owner_id=u.id))
    db.add(
        Task(
            repository="acme/queue",
            trigger_type="issue",
            issue_number=41,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            owner_id=u.id,
        )
    )
    db.commit()
    t = db.query(Task).filter(Task.issue_number == 41).first()
    import app.tasks.service as _svc

    monkeypatch.setattr(
        _svc,
        "run_task_inline",
        lambda task_id, **kw: {"status": "BLOCKED", "error": "no source"},
    )
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/run?sync=true", cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["sync"] is True


def test_cancel_unconnected_only_touches_unconnected_queued(db):
    """Legacy cleanup: cancel QUEUED rows for repos never connected in FixHub."""
    from app.db.models import Repository

    u = make_user(db, email="cleanup@example.com")
    u.is_admin = 1
    db.add(
        Repository(
            github_full_name="acme/connected", installation_id="900", owner_id=u.id
        )
    )
    connected = Task(
        repository="acme/connected",
        trigger_type="ci",
        ci_sha="c1",
        status="QUEUED",
        owner_id=u.id,
    )
    orphan = Task(
        repository="acme/legacy",
        trigger_type="ci",
        ci_sha="l1",
        status="QUEUED",
        owner_id=u.id,
    )
    running_orphan = Task(
        repository="acme/legacy",
        trigger_type="ci",
        ci_sha="l2",
        status="RUNNING",
        owner_id=u.id,
    )
    db.add_all([connected, orphan, running_orphan])
    db.commit()

    c = TestClient(app)
    r = c.post("/api/tasks/cancel-unconnected", cookies=session_cookies(db, u))
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] == 1
    assert r.json()["task_ids"] == [orphan.id]
    db.expire_all()
    assert db.query(Task).filter(Task.id == connected.id).first().status == "QUEUED"
    assert db.query(Task).filter(Task.id == orphan.id).first().status == "CANCELLED"
    # Running work is never killed by the cleanup endpoint.
    assert (
        db.query(Task).filter(Task.id == running_orphan.id).first().status == "RUNNING"
    )


def test_cancel_unconnected_requires_admin(db):
    u = make_user(db, email="plain-cleanup@example.com")
    c = TestClient(app)
    r = c.post("/api/tasks/cancel-unconnected", cookies=session_cookies(db, u))
    assert r.status_code == 403
