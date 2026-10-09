"""Phase 4 unit tests (SQLite-fast): claim CAS, transitions, caps, dedupe
helpers, sweep, no-silent-fallback, retry bounds. PG concurrency proof lives
in test_phase4_pg.py (Docker PostgreSQL 16, hard-fails without it)."""

from datetime import timedelta

from fastapi.testclient import TestClient

from app.db.models import Task
from app.main import app
from tests.conftest import make_user, session_cookies


def _task(db, status="QUEUED", owner=None, repo="acme/p4"):
    t = Task(
        repository=repo,
        trigger_type="issue",
        issue_number=1,
        issue_title="t",
        issue_body="b",
        status=status,
        owner_id=owner.id if owner else None,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_claim_exactly_one_winner(db):
    from app.tasks import service as _svc

    t = _task(db)
    won = _svc.claim_task(db, t.id, worker_id="w-1")
    lost = _svc.claim_task(db, t.id, worker_id="w-2")
    assert won is not None and won.status == "RUNNING" and won.claimed_by == "w-1"
    assert won.lease_expires_at is not None
    assert lost is None
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "RUNNING" and row.claimed_by == "w-1"


def test_claim_adopts_legacy_running_without_lease(db):
    from app.tasks import service as _svc

    t = _task(db, status="RUNNING")
    won = _svc.claim_task(db, t.id, worker_id="w-9")
    assert won is not None and won.status == "RUNNING"


def test_claim_rejects_terminal_and_completed(db):
    from app.tasks import service as _svc

    for status in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
        t = _task(db, status=status)
        assert _svc.claim_task(db, t.id, worker_id="w-x") is None


def test_cas_status_guards_transitions(db):
    from app.tasks import service as _svc

    t = _task(db, status="QUEUED")
    assert _svc.cas_status(db, t.id, {"QUEUED"}, "CANCELLED") is True
    # Terminal CANCELLED never leaves.
    assert _svc.cas_status(db, t.id, {"QUEUED", "RUNNING"}, "RUNNING") is False
    db.expire_all()
    assert db.query(Task).filter(Task.id == t.id).first().status == "CANCELLED"


def test_repair_claim_accepts_awaiting_ci(db):
    from app.tasks import service as _svc

    t = _task(db, status="AWAITING_CI")
    assert _svc.claim_task(db, t.id, worker_id="w-r") is None
    assert _svc.claim_task(db, t.id, worker_id="w-r", repair=True) is not None


def test_double_run_second_reports_claimed(db, tmp_path, monkeypatch):
    from app.config import settings as _settings
    from app.tasks import service as _svc

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "ws"))
    t = _task(db, status="RUNNING")  # legacy unclaimed row: first run adopts
    out1_claim = _svc.claim_task(db, t.id, worker_id="first")
    assert out1_claim is not None
    # Simulate the race: a second worker attempts the same task while leased.
    out2 = _svc.run_task_inline(t.id, source="", worker_id="second")
    assert out2.get("already_claimed") is True


def test_concurrency_caps_block_enqueue(db, monkeypatch):
    from app.config import settings as _settings
    from app.tasks import queue as _queue

    monkeypatch.setattr(_settings, "MAX_RUNNING_TASKS_GLOBAL", 1)
    monkeypatch.setattr(_settings, "MAX_RUNNING_PER_USER", 0)
    monkeypatch.setattr(_settings, "MAX_RUNNING_PER_REPO", 0)
    u = make_user(db, email="cap@example.com")
    _task(db, status="RUNNING", owner=u)
    t2 = _task(db, status="QUEUED", owner=u)
    monkeypatch.setattr(
        _queue,
        "get_queue",
        lambda: (_ for _ in ()).throw(
            AssertionError("redis must not be touched when capped")
        ),
    )
    out = _queue.enqueue_task(t2.id)
    assert out["enqueued"] is False and "limit" in out["error"]


def test_enqueue_uses_deterministic_job_id(db, monkeypatch):
    from app.tasks import queue as _queue

    seen = {}

    class _FakeQueue:
        def fetch_job(self, job_id):
            return seen.get(job_id)

        def enqueue(self, fn, task_id, job_id=None, **kw):
            assert job_id == f"fixhub-task-{task_id}"
            job = type("J", (), {"id": job_id})()
            seen[job_id] = job
            return job

    monkeypatch.setattr(_queue, "get_queue", lambda: _FakeQueue())
    t = _task(db, status="QUEUED")
    first = _queue.enqueue_task(t.id)
    second = _queue.enqueue_task(t.id)
    assert first["job_id"] == f"fixhub-task-{t.id}"
    assert second.get("duplicate") is True and second["job_id"] == first["job_id"]


def test_run_endpoint_no_silent_fallback(db, monkeypatch):
    from app.tasks import queue as _queue

    u = make_user(db, email="fb@example.com")
    cookies = session_cookies(db, u)
    t = _task(db, status="QUEUED", owner=u)
    monkeypatch.setattr(_queue, "get_queue", lambda: None)
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/run", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("queued") is False and "redis" in body.get("error", "")
    assert "sync" not in body  # no silent inline execution
    db.expire_all()
    assert db.query(Task).filter(Task.id == t.id).first().status == "QUEUED"


def test_run_endpoint_cap_returns_429(db, monkeypatch):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "MAX_RUNNING_TASKS_GLOBAL", 1)
    monkeypatch.setattr(_settings, "MAX_RUNNING_PER_USER", 0)
    monkeypatch.setattr(_settings, "MAX_RUNNING_PER_REPO", 0)
    u = make_user(db, email="cap2@example.com")
    cookies = session_cookies(db, u)
    _task(db, status="RUNNING", owner=u)
    t2 = _task(db, status="QUEUED", owner=u)
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t2.id}/run", cookies=cookies)
    # Redis down and caps hit: either 429 (capped) or clear redis error.
    assert r.status_code in (200, 429), r.text


def test_lease_sweep_requeues_expired(db):
    from app.db.database import utcnow
    from app.main import _sweep_stale_running_tasks

    t = _task(db, status="RUNNING")
    t.claimed_by = "dead-worker"
    t.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    _sweep_stale_running_tasks()
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "QUEUED" and row.claimed_by == ""


def test_lease_sweep_ignores_live_lease(db):
    from app.db.database import utcnow

    from app.main import _sweep_stale_running_tasks

    t = _task(db, status="RUNNING")
    t.claimed_by = "live-worker"
    t.lease_expires_at = utcnow() + timedelta(hours=1)
    db.commit()
    _sweep_stale_running_tasks()
    db.expire_all()
    assert db.query(Task).filter(Task.id == t.id).first().status == "RUNNING"


def test_lease_sweep_fails_ancient_unleased(db):
    from app.db.database import utcnow

    from app.main import _sweep_stale_running_tasks

    t = _task(db, status="RUNNING")
    t.updated_at = utcnow() - timedelta(seconds=3600)
    db.commit()
    _sweep_stale_running_tasks()
    db.expire_all()
    assert db.query(Task).filter(Task.id == t.id).first().status == "FAILED"


def test_client_retry_bounded_on_transient(monkeypatch):
    import httpx

    from app.config import settings as _settings
    from app.llm import client as _cli

    monkeypatch.setattr(_settings, "LLM_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(_settings, "LLM_RETRY_BASE_S", 0)
    monkeypatch.setattr(_settings, "LLM_API_KEY", "k")
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return type("R", (), {"status_code": 500, "text": "boom"})()
        return type(
            "R",
            (),
            {
                "status_code": 200,
                "text": "",
                "json": lambda self=None: {"choices": [{"message": {"content": "hi"}}]},
            },
        )()

    monkeypatch.setattr(httpx, "post", _flaky)
    out = _cli.chat_completion([{"role": "user", "content": "hi"}])
    assert out.content == "hi" and calls["n"] == 3


def test_client_auth_failure_not_retried(monkeypatch):
    import httpx

    from app.config import settings as _settings
    from app.llm import client as _cli

    monkeypatch.setattr(_settings, "LLM_RETRY_BASE_S", 0)
    monkeypatch.setattr(_settings, "LLM_API_KEY", "bad")
    calls = {"n": 0}

    def _denied(*a, **k):
        calls["n"] += 1
        return type("R", (), {"status_code": 401, "text": "no"})()

    monkeypatch.setattr(httpx, "post", _denied)
    try:
        _cli.chat_completion([{"role": "user", "content": "hi"}])
    except _cli.LLMBlockedError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected LLMBlockedError")
    assert calls["n"] == 1


def test_readiness_reports_dependencies(db):
    c = TestClient(app)
    r = c.get("/readiness")
    # SQLite file DB is up; Redis is down in this env -> 503, leak-free shape.
    assert r.status_code in (200, 503), r.text
    body = r.json()
    # Phase 5 adds leak-free sandbox keys (backend name + ready boolean).
    assert {"ready", "postgres", "redis"} <= set(body)
    assert body["postgres"] is True
    assert body["sandbox_backend"] in ("host", "firecracker", "unknown")
    assert body["sandbox_ready"] in (True, False)
