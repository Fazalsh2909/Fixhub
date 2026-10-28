"""Reliability remediation regressions (webhook inbox, dedupe, queue, guards).

Every test here corresponds to a verified production failure mode. They are
deterministic (SQLite + mocks) and never touch Redis, GitHub, or a sandbox.
"""

import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db.models import (
    GitHubConnection,
    Repository,
    Task,
    TaskEvent,
    WebhookDelivery,
)
from app.main import app
from tests.conftest import make_user, session_cookies


# --- helpers ------------------------------------------------------------------


def _own(db, installation_id=555, email="rel@example.com", repo="acme/rel"):
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


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


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


def _issue(number=7, repo="acme/rel", installation_id=555):
    return {
        "action": "opened",
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
    conclusion="failure",
    repo="acme/rel",
    installation_id=555,
    prs=None,
):
    payload = {
        "action": "completed",
        "installation": {"id": installation_id},
        "repository": {"full_name": repo},
        "workflow_run": {
            "id": run_id,
            "name": name,
            "path": ".github/workflows/ci.yml",
            "conclusion": conclusion,
            "head_sha": sha,
            "head_branch": branch,
            "html_url": f"http://x/{run_id}",
        },
    }
    if prs is not None:
        payload["workflow_run"]["pull_requests"] = prs
    return payload


def _ci_task(db, *, repo="acme/rel", sha="abc123", status="FAILED", **kw):
    t = Task(
        repository=repo,
        trigger_type="ci",
        ci_sha=sha,
        ci_workflow="CI",
        ci_run_id="99",
        status=status,
        **kw,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# --- webhook durability -------------------------------------------------------


def test_webhook_delivery_is_durable_and_replayable(db, monkeypatch):
    from app.github import webhook as _wh

    _own(db)
    db.add(
        WebhookDelivery(
            delivery_id="crash-1",
            state="received",
            event_type="issues",
            payload=json.dumps(_issue()),
            received_at=_wh._utcnow() - timedelta(minutes=10),
        )
    )
    db.commit()

    out = _wh.reprocess_pending_deliveries(stale_s=0)
    assert out["processed"] == 1 and out["failed"] == 0
    db.expire_all()
    row = db.query(WebhookDelivery).filter_by(delivery_id="crash-1").first()
    assert row.state == "processed" and row.processed_at is not None
    tasks = db.query(Task).filter(Task.repository == "acme/rel").all()
    assert len(tasks) == 1

    # Replay after success is a no-op (row is no longer `received`).
    again = _wh.reprocess_pending_deliveries(stale_s=0)
    assert again["checked"] == 0
    assert db.query(Task).filter(Task.repository == "acme/rel").count() == 1


def test_webhook_handler_failure_keeps_delivery_replayable(db, monkeypatch):
    from app.github import webhook as _wh

    _own(db)
    row = WebhookDelivery(
        delivery_id="boom-1",
        state="received",
        event_type="issues",
        payload=json.dumps(_issue(number=8)),
        received_at=_wh._utcnow(),
    )
    db.add(row)
    db.commit()

    real = _wh._handle_issue

    def boom(db_, payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(_wh, "_handle_issue", boom)
    resp = _wh._process_delivery(db, row)
    assert getattr(resp, "status_code", 200) == 500
    db.expire_all()
    row = db.query(WebhookDelivery).filter_by(delivery_id="boom-1").first()
    assert row.state == "received"  # effects never committed
    assert "boom" in row.error

    # Restored handler + retry finishes the delivery exactly once.
    monkeypatch.setattr(_wh, "_handle_issue", real)
    out = _wh._process_delivery(db, row)
    assert out.get("ok") is True
    db.expire_all()
    row = db.query(WebhookDelivery).filter_by(delivery_id="boom-1").first()
    assert row.state == "processed"
    assert db.query(Task).filter(Task.repository == "acme/rel").count() == 1


def test_webhook_ack_marks_delivery_processed_after_effects(db):
    _own(db)
    c = TestClient(app)
    r = _post(c, _issue(number=9), "ack-1", "issues")
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    db.expire_all()
    row = db.query(WebhookDelivery).filter_by(delivery_id="ack-1").first()
    assert row.state == "processed" and row.processed_at is not None
    assert db.query(Task).filter(Task.id == task_id).first() is not None


def test_webhook_stores_large_payload_without_corrupting_json(db):
    """A >64KB payload must stay parseable (truncation broke replay)."""
    _own(db)
    payload = _issue(number=10)
    payload["issue"]["body"] = "x" * 80_000
    c = TestClient(app)
    r = _post(c, payload, "large-1", "issues")
    assert r.status_code == 200, r.text
    db.expire_all()
    row = db.query(WebhookDelivery).filter_by(delivery_id="large-1").first()
    assert row.state == "processed"
    assert json.loads(row.payload)["issue"]["body"] == "x" * 80_000


def test_webhook_inbox_prunes_only_terminal_rows(db, monkeypatch):
    from app.github import webhook as _wh

    monkeypatch.setattr(settings, "WEBHOOK_DELIVERY_RETENTION_HOURS", 1)
    old = _wh._utcnow() - timedelta(hours=3)
    db.add_all(
        [
            WebhookDelivery(
                delivery_id="prune-done",
                state="processed",
                payload="{}",
                created_at=old,
            ),
            WebhookDelivery(
                delivery_id="prune-bad",
                state="rejected",
                payload="{}",
                created_at=old,
            ),
            WebhookDelivery(
                delivery_id="prune-stuck",
                state="received",
                payload="{}",
                created_at=old,
                received_at=old,
            ),
        ]
    )
    db.commit()

    # Simulate a delivery that still cannot be processed: it must survive
    # pruning so the next sweep can retry it.
    monkeypatch.setattr(_wh, "_process_delivery", lambda db_, row: object())
    out = _wh.reprocess_pending_deliveries(stale_s=0)
    assert out["pruned"] == 2
    db.expire_all()
    assert db.query(WebhookDelivery).filter_by(delivery_id="prune-done").first() is None
    assert db.query(WebhookDelivery).filter_by(delivery_id="prune-bad").first() is None
    assert (
        db.query(WebhookDelivery).filter_by(delivery_id="prune-stuck").first()
        is not None
    )


def test_webhook_does_not_fetch_ci_context_inline(db, monkeypatch):
    """The HTTP ack path must not call GitHub (durability/latency)."""
    from app.github import webhook as _wh

    _own(db)

    def must_not_run(**kw):
        raise AssertionError("webhook must not enrich CI context inline")

    monkeypatch.setattr(_wh, "build_ci_context", must_not_run)
    c = TestClient(app)
    r = _post(c, _workflow(), "fast-1", "workflow_run")
    assert r.status_code == 200, r.text
    task = db.query(Task).filter(Task.id == r.json()["task_id"]).first()
    assert task.ci_head_branch == "main"
    ev = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task.id, TaskEvent.type == "CI_CONTEXT_LOADED")
        .first()
    )
    data = json.loads(ev.data_json)
    assert data["deferred"] is True
    assert "workflow_content" not in data


# --- dedupe lifecycle ---------------------------------------------------------


def test_ci_dedupe_releases_after_terminal(db):
    _own(db)
    old = _ci_task(db, status="FAILED", dedupe_key="ci:acme/rel:abc123")
    c = TestClient(app)
    r = _post(c, _workflow(), "dedupe-1", "workflow_run")
    assert r.status_code == 200, r.text
    new_id = r.json()["task_id"]
    assert new_id != old.id
    db.expire_all()
    assert db.query(Task).filter(Task.id == old.id).first().dedupe_key is None

    # A second delivery while the new task is ACTIVE still dedupes.
    r2 = _post(c, _workflow(), "dedupe-2", "workflow_run")
    assert r2.json()["task_id"] == new_id
    assert r2.json().get("duplicate")


def test_issue_active_key_released_on_terminal(db):
    from app.tasks.service import cas_status

    _own(db)
    old = Task(
        repository="acme/rel",
        trigger_type="issue",
        issue_number=11,
        issue_title="t",
        issue_body="b",
        status="QUEUED",
        active_issue_key="issue:acme/rel:11",
    )
    db.add(old)
    db.commit()
    assert cas_status(db, old.id, {"QUEUED"}, "FAILED", error="nope") is True
    db.expire_all()
    old = db.query(Task).filter(Task.id == old.id).first()
    assert old.active_issue_key is None

    c = TestClient(app)
    r = _post(c, _issue(number=11), "issue-reopen-1", "issues")
    assert r.json()["task_id"] != old.id


def test_cas_status_is_single_winner(db):
    from app.tasks.service import cas_status

    _own(db)
    t = _ci_task(db, status="QUEUED", dedupe_key="ci:acme/rel:abc123")
    assert cas_status(db, t.id, {"QUEUED"}, "FAILED", error="x") is True
    assert cas_status(db, t.id, {"QUEUED"}, "FAILED", error="y") is False
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.dedupe_key is None


def test_active_keys_for_does_not_mutate_the_row(db):
    """Key reacquisition must be pure: autoflush could otherwise commit keys
    even when the CAS that was supposed to move the row loses."""
    from app.tasks.service import _active_keys_for

    t = _ci_task(db, status="FAILED", dedupe_key=None)
    keys = _active_keys_for(t)
    assert keys == {
        "dedupe_key": "ci:acme/rel:abc123",
        "active_issue_key": None,
    }
    assert t.dedupe_key is None and t.active_issue_key is None

    issue = Task(
        repository="acme/rel",
        trigger_type="issue",
        issue_number=42,
        status="FAILED",
        dedupe_key=None,
        active_issue_key=None,
    )
    db.add(issue)
    db.commit()
    keys = _active_keys_for(issue)
    assert keys == {
        "active_issue_key": "issue:acme/rel:42",
        "dedupe_key": None,
    }
    assert issue.active_issue_key is None


def test_issue_race_returns_existing_task(db, monkeypatch):
    """A concurrent delivery that loses the unique index returns the winner."""
    from app.github import webhook as _wh

    _own(db)
    winner = Task(
        repository="acme/rel",
        trigger_type="issue",
        issue_number=12,
        issue_title="w",
        issue_body="b",
        status="QUEUED",
        active_issue_key="issue:acme/rel:12",
    )
    db.add(winner)
    db.commit()

    real = _wh._active_issue_task
    state = {"first": True}

    def racing(db, *, repo_name, number):
        if state["first"]:
            state["first"] = False
            return None  # simulate the check missing the concurrent winner
        return real(db=db, repo_name=repo_name, number=number)

    monkeypatch.setattr(_wh, "_active_issue_task", racing)
    out = _wh._handle_issue(db, _issue(number=12))
    assert out.get("duplicate") == "active_task"
    assert out.get("task_id") == winner.id
    assert db.query(Task).filter(Task.issue_number == 12).count() == 1


# --- atomic capacity + queue --------------------------------------------------


def test_claim_capacity_leaves_task_queued(db, monkeypatch):
    from app.db.database import utcnow
    from app.tasks.service import claim_task

    monkeypatch.setattr(settings, "MAX_RUNNING_TASKS_GLOBAL", 1)
    monkeypatch.setattr(settings, "MAX_RUNNING_PER_USER", 0)
    monkeypatch.setattr(settings, "MAX_RUNNING_PER_REPO", 0)
    running = Task(
        repository="acme/rel",
        trigger_type="issue",
        issue_number=20,
        status="RUNNING",
        lease_expires_at=utcnow() + timedelta(seconds=600),
    )
    queued = Task(
        repository="acme/rel",
        trigger_type="issue",
        issue_number=21,
        status="QUEUED",
    )
    db.add_all([running, queued])
    db.commit()

    assert claim_task(db, queued.id, worker_id="w") is None
    db.expire_all()
    assert db.query(Task).filter(Task.id == queued.id).first().status == "QUEUED"

    # An expired lease is reclaimable and must not hold the only slot.
    running = db.query(Task).filter(Task.id == running.id).first()
    running.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert claim_task(db, queued.id, worker_id="w") is not None


def test_reconciler_filters_ineligible_before_limit(db, monkeypatch):
    from app.db.database import utcnow
    from app.tasks import queue as _queue
    from app.tasks import reconciler as _rec

    u = _own(db)
    old = utcnow() - timedelta(seconds=120)
    # 60 ineligible rows with LOWER ids than the one runnable task.
    for i in range(60):
        db.add(
            Task(
                repository="acme/never-connected",
                trigger_type="issue",
                issue_number=100 + i,
                status="QUEUED",
                created_at=old,
            )
        )
    eligible = Task(
        repository="acme/rel",
        trigger_type="issue",
        issue_number=999,
        status="QUEUED",
        created_at=old,
    )
    db.add(eligible)
    db.commit()

    class _FakeQ:
        def fetch_job(self, job_id):
            return None

    called = []
    monkeypatch.setattr(_queue, "get_queue", lambda: _FakeQ())
    monkeypatch.setattr(
        _queue,
        "enqueue_task",
        lambda task_id, **kw: (
            called.append(task_id) or {"enqueued": True, "job_id": "j"}
        ),
    )
    out = _rec.requeue_orphaned(limit=50)
    assert out["requeued"] == 1
    assert called == [eligible.id]


def test_reconciler_uses_repair_job_for_repair_round(db, monkeypatch):
    from app.db.database import utcnow
    from app.tasks import queue as _queue
    from app.tasks import reconciler as _rec

    _own(db)
    t = Task(
        repository="acme/rel",
        trigger_type="ci",
        ci_sha="abc123",
        ci_attempt_count=1,
        status="QUEUED",
        created_at=utcnow() - timedelta(seconds=120),
    )
    db.add(t)
    db.commit()

    probed = []

    class _FakeQ:
        def fetch_job(self, job_id):
            probed.append(job_id)
            return None

    repairs = []
    monkeypatch.setattr(_queue, "get_queue", lambda: _FakeQ())
    monkeypatch.setattr(
        _queue,
        "enqueue_repair",
        lambda task_id: (
            repairs.append(task_id) or {"enqueued": True, "job_id": "repair-j"}
        ),
    )
    monkeypatch.setattr(
        _queue,
        "enqueue_task",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("repair round must not enqueue an initial run")
        ),
    )
    out = _rec.requeue_orphaned()
    assert out["requeued"] == 1
    assert probed == [f"fixhub-repair-{t.id}-1"]
    assert repairs == [t.id]


def test_reconciler_skips_live_repair_job(db, monkeypatch):
    from app.db.database import utcnow
    from app.tasks import queue as _queue
    from app.tasks import reconciler as _rec

    _own(db)
    db.add(
        Task(
            repository="acme/rel",
            trigger_type="ci",
            ci_sha="abc123",
            ci_attempt_count=2,
            status="QUEUED",
            created_at=utcnow() - timedelta(seconds=120),
        )
    )
    db.commit()

    class _Live:
        def get_status(self, refresh=False):
            return "started"

    class _FakeQ:
        def fetch_job(self, job_id):
            return _Live()

    monkeypatch.setattr(_queue, "get_queue", lambda: _FakeQ())
    monkeypatch.setattr(
        _queue,
        "enqueue_repair",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("a live repair job must not be duplicated")
        ),
    )
    out = _rec.requeue_orphaned()
    assert out["requeued"] == 0


def test_enqueue_replaces_terminal_leftover_job():
    """A finished RQ job with the same id must not make a retry a silent no-op."""
    from app.tasks import queue as _queue

    deleted = []

    class _Finished:
        id = "fixhub-task-1"

        def get_status(self, refresh=False):
            return "finished"

        def delete(self):
            deleted.append(self.id)

    class _FakeQ:
        def __init__(self):
            self.jobs = {"fixhub-task-1": _Finished()}

        def fetch_job(self, job_id):
            return self.jobs.get(job_id)

        def enqueue(self, fn, task_id, job_id=None, **kw):
            return type("J", (), {"id": job_id})()

    q = _FakeQ()
    out = _queue._enqueue_deduped(q, object(), 1, "fixhub-task-1")
    assert out["enqueued"] is True and not out.get("duplicate")
    assert deleted == ["fixhub-task-1"]


# --- rerun endpoint -----------------------------------------------------------


def test_rerun_failed_task_requeues_with_cas(db, monkeypatch):
    from app.tasks import queue as _queue

    u = make_user(db, email="rerun@example.com")
    cookies = session_cookies(db, u)
    t = Task(
        repository="acme/rel",
        trigger_type="issue",
        issue_number=30,
        issue_title="t",
        issue_body="b",
        owner_id=u.id,
        status="FAILED",
        error="previous failure",
    )
    db.add(t)
    db.commit()

    seen = []
    monkeypatch.setattr(
        _queue,
        "enqueue_task",
        lambda task_id, **kw: (
            seen.append(task_id) or {"enqueued": True, "job_id": "rerun-j"}
        ),
    )
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/run", cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is True
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "QUEUED" and row.error == ""
    assert row.active_issue_key == f"issue:acme/rel:30"
    assert seen == [t.id]


def test_rerun_repair_round_uses_repair_job(db, monkeypatch):
    from app.tasks import queue as _queue

    u = make_user(db, email="rerun-repair@example.com")
    cookies = session_cookies(db, u)
    t = _ci_task(
        db,
        status="FAILED",
        ci_attempt_count=2,
        owner_id=u.id,
        dedupe_key=None,
    )
    seen = []
    monkeypatch.setattr(
        _queue,
        "enqueue_repair",
        lambda task_id: (
            seen.append(task_id) or {"enqueued": True, "job_id": "repair-j"}
        ),
    )
    monkeypatch.setattr(
        _queue,
        "enqueue_task",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("repair rerun must use enqueue_repair")
        ),
    )
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/run", cookies=cookies)
    assert r.status_code == 200, r.text
    assert seen == [t.id]
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "QUEUED"
    assert row.dedupe_key == "ci:acme/rel:abc123"


def test_rerun_completed_task_is_conflict(db):
    u = make_user(db, email="rerun-done@example.com")
    cookies = session_cookies(db, u)
    t = Task(
        repository="acme/rel",
        trigger_type="issue",
        issue_number=31,
        status="COMPLETED",
        owner_id=u.id,
    )
    db.add(t)
    db.commit()
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/run", cookies=cookies)
    assert r.status_code == 409
    assert r.json()["queued"] is False


# --- usage guard fail-closed + agent governor ---------------------------------


def test_fail_closed_request_guard_stops_loop(monkeypatch):
    import app.agent.loop as _loop

    calls = []

    def _fake_chat(messages, tools=None, **kw):
        calls.append(1)
        raise AssertionError("provider must not be called when the guard fails")

    def _broken_guard():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(_loop._llm, "chat_completion", _fake_chat)
    out = _loop.run_agent(
        workspace=".",
        trigger_type="issue",
        repository="acme/rel",
        issue_title="t",
        issue_body="b",
        request_guard=_broken_guard,
    )
    assert out.usage_limited is True and out.finished is False
    assert "budget check failed" in out.usage_limit_reason
    assert calls == []


def test_governor_caches_reads_and_suppresses_identical_repeats(monkeypatch):
    import app.agent.loop as _loop

    monkeypatch.setattr(settings, "LLM_REPEAT_SUPPRESS_READS", 1)
    g = _loop._Governor()
    args = {"path": "a.py"}
    assert g.before("read_file", args) is None
    assert g.cached("read_file", args) is None
    g.after("read_file", args, "content", True)
    cached = g.cached("read_file", args)
    assert cached and "cached" in cached
    assert g.before("read_file", args).startswith("ERROR")  # cap exceeded

    # A real mutation invalidates the cache.
    g.after("edit_file", {"path": "a.py"}, "ok", True)
    assert g.cached("read_file", args) is None


def test_governor_command_invalidates_cached_reads(monkeypatch):
    """A command can change files, so a cached read must not survive it."""
    import app.agent.loop as _loop

    g = _loop._Governor()
    args = {"path": "a.py"}
    g.after("read_file", args, "old content", True)
    assert g.cached("read_file", args) is not None
    g.after("run_command", {"command": "sed -i s/a/b/ a.py"}, "ok", True)
    assert g.cached("read_file", args) is None


def test_governor_counts_blocked_steps_toward_stall(monkeypatch):
    import app.agent.loop as _loop

    monkeypatch.setattr(settings, "LLM_STALL_WINDOW", 2)
    g = _loop._Governor()
    for _ in range(4):
        g.blocked_step()
    assert g.stalled() is True
    assert g.nudge() is not None
    assert g.nudge() is not None
    assert g.nudge() is None


def test_governor_repeat_window_nudges_on_novel_interleaved_cycles():
    """Cycling through seen reads with novel interludes must trip the repeat
    guard: production task #66 re-explored the repo after a successful edit
    and the consecutive no-evidence counter kept resetting on novel keys."""
    import app.agent.loop as _loop

    g = _loop._Governor()
    keys = ["a", "b", "c", "d", "a", "b", "c", "d", "a", "b", "c", "a"]
    for i, k in enumerate(keys):
        g.after("read_file", {"path": f"{k}.py"}, f"content {i}", True)
    repeats, total = g._repeat_count(_loop.REPEAT_NUDGE_WINDOW)
    assert total == _loop.REPEAT_NUDGE_WINDOW
    assert repeats >= _loop.REPEAT_NUDGE_LIMIT
    assert g.nudge() is not None


def test_governor_repeat_window_ignores_linear_novel_work():
    """A linear investigation (all unique keys) must never trip the guard."""
    import app.agent.loop as _loop

    g = _loop._Governor()
    for i in range(25):
        g.after("run_command", {"command": f"unique-cmd-{i}"}, f"out {i}", True)
    assert g.nudge() is None
    assert g.stalled() is False


def test_governor_repeat_window_hard_stops_on_cycles():
    import app.agent.loop as _loop

    g = _loop._Governor()
    for i, k in enumerate(["a", "b", "c", "d"] * 5):
        g.after("read_file", {"path": f"{k}.py"}, f"content {i}", True)
    assert g.stalled() is True


def test_cached_read_returns_bounded_digest():
    """A repeat read must not re-inject the full file into context."""
    import app.agent.loop as _loop

    g = _loop._Governor()
    content = "\n".join(f"line {i}" for i in range(200))
    g.after("read_file", {"path": "big.yml"}, content, True)
    out = g.cached("read_file", {"path": "big.yml"})
    assert out is not None
    assert len(out) < 1600
    assert "cached" in out
    assert "line 0" in out and "line 199" in out


def test_prompt_documents_sandbox_git():
    """The model must be told raw git fails in the sandbox before it tries."""
    from app.agent.prompt import SYSTEM_PROMPT

    assert "no `.git`" in SYSTEM_PROMPT
    assert "git_diff" in SYSTEM_PROMPT
    assert "offset/limit" in SYSTEM_PROMPT


def test_governor_caps_reads_per_path(monkeypatch):
    """Jittered offset/limit must not defeat read accounting: after the cap,
    further reads of the same path are blocked while other paths stay open."""
    import app.agent.loop as _loop

    monkeypatch.setattr(settings, "LLM_MAX_READS_PER_PATH", 2)
    g = _loop._Governor()
    for off in ("1", "30"):
        args = {"path": "ci.yml", "offset": off, "limit": "20"}
        assert g.before("read_file", args) is None
        g.after("read_file", args, "content", True)
    blocked = g.before("read_file", {"path": "ci.yml", "offset": "60", "limit": "20"})
    assert blocked is not None and blocked.startswith("ERROR")
    assert "act on what you have" in blocked
    # A different path is unaffected.
    assert g.before("read_file", {"path": "other.yml"}) is None


def test_governor_window_key_treats_jittered_reads_as_repeats():
    """Path-normalized window keys: re-reading one path with varying
    offset/limit must trip the repeat guard (production task #68 paged one
    77-line file ~30 times, each a 'new' exact-args key)."""
    import app.agent.loop as _loop

    g = _loop._Governor()
    for i in range(_loop.REPEAT_STOP_WINDOW):
        g.after(
            "read_file",
            {"path": "ci.yml", "offset": str(i), "limit": str(10 + i)},
            "content",
            True,
        )
    assert g.stalled() is True


def test_preserve_partial_work_keeps_changed_workspace(db, tmp_path):
    """A stopped run with real changes must land in NEEDS_REVIEW, not be
    wiped (production task #68: correct edit at step 8, then budget burn)."""
    import subprocess

    from app.tasks import service as svc

    def _git(*args):
        subprocess.run(
            ["git", *args], cwd=str(tmp_path), check=True, capture_output=True
        )

    _git("init")
    (tmp_path / "ci.yml").write_text("name: CI\n")
    _git("add", ".")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    (tmp_path / "ci.yml").write_text("name: CI-fixed\n")

    u = _own(db)
    task = Task(
        repository="acme/rel",
        trigger_type="ci",
        owner_id=u.id,
        status="RUNNING",
        workspace=str(tmp_path),
    )
    db.add(task)
    db.commit()

    out = svc._preserve_partial_work(db, task, str(tmp_path), "agent_stopped")
    assert out is not None and out["status"] == "NEEDS_REVIEW"
    db.expire_all()
    row = db.query(Task).filter(Task.id == task.id).first()
    assert row.status == "NEEDS_REVIEW"
    assert "work preserved" in (row.error or "")

    # A workspace with nothing uncommitted must not be preserved (the normal
    # failure path stays FAILED).
    _git("checkout", "--", "ci.yml")
    assert svc._preserve_partial_work(db, task, str(tmp_path), "agent_stopped") is None


def test_agent_loop_stops_when_actions_stall(monkeypatch, tmp_path):
    import app.agent.loop as _loop
    from app.llm.client import AssistantMessage, ToolCall

    monkeypatch.setattr(settings, "LLM_STALL_WINDOW", 2)
    monkeypatch.setattr(settings, "LLM_MAX_ITERATIONS", 50)
    monkeypatch.setattr(settings, "LLM_REPEAT_SUPPRESS_READS", 100)
    calls = {"n": 0}

    def fake_chat(messages, tools=None, **kw):
        calls["n"] += 1
        return AssistantMessage(
            "", [ToolCall(str(calls["n"]), "read_file", {"path": "same.py"})]
        )

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)
    monkeypatch.setattr(_loop, "_execute", lambda ws, name, args: "same content")
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="issue",
        repository="acme/rel",
        issue_title="t",
        issue_body="b",
    )
    assert out.finished is False
    assert "stalled" in out.summary
    assert calls["n"] < 50


# --- publication policy -------------------------------------------------------


def test_ci_publish_target_repairs_originating_pr():
    from app.tasks.service import _ci_publish_target

    same_repo = Task(
        repository="acme/rel",
        trigger_type="ci",
        ci_sha="abc123",
        ci_pr_number=7,
        ci_head_branch="feature/x",
        ci_base_branch="develop",
        ci_is_fork=0,
    )
    target = _ci_publish_target(same_repo, "main", "fixhub-fixes/issue-1-task-1")
    assert target == {
        "repair_pr": True,
        "branch": "feature/x",
        "base": "develop",
    }

    fork = Task(
        repository="acme/rel",
        trigger_type="ci",
        ci_sha="abc123",
        ci_pr_number=8,
        ci_head_branch="feature/y",
        ci_base_branch="main",
        ci_is_fork=1,
    )
    target = _ci_publish_target(fork, "main", "fixhub-fixes/issue-1-task-1")
    assert target == {
        "repair_pr": False,
        "branch": "fixhub-fixes/issue-1-task-1",
        "base": "main",
    }

    legacy = Task(repository="acme/rel", trigger_type="ci", ci_sha="abc123")
    target = _ci_publish_target(legacy, "main", "fixhub-fixes/issue-1-task-1")
    assert target["repair_pr"] is False and target["base"] == "main"


def test_ci_weakening_signal_classifies_diffs(monkeypatch):
    from app.github import publisher as _pub
    from app.tasks import service as _svc

    monkeypatch.setattr(_pub, "diff_paths", lambda path, *p: "")
    assert _svc._ci_weakening_signal(".") == (False, "")

    deleted = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "deleted file mode 100644\n"
    )
    monkeypatch.setattr(_pub, "diff_paths", lambda path, *p: deleted)
    edited, reason = _svc._ci_weakening_signal(".")
    assert edited is True and "deleted" in reason

    removed_test = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "-      - run: python -m pytest tests/\n"
        "+      - run: echo skipped\n"
    )
    monkeypatch.setattr(_pub, "diff_paths", lambda path, *p: removed_test)
    edited, reason = _svc._ci_weakening_signal(".")
    assert edited is True and "pytest" in reason

    benign = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "-  schedule: cron: '0 1 * * *'\n"
        "+  schedule: cron: '0 2 * * *'\n"
    )
    monkeypatch.setattr(_pub, "diff_paths", lambda path, *p: benign)
    assert _svc._ci_weakening_signal(".") == (True, "")


def test_publish_blocks_a_fix_that_deletes_ci(db, tmp_path, monkeypatch):
    """A CI task whose fix deletes the workflow is BLOCKED, never published."""
    import subprocess

    from app.tasks import service as _svc

    work = tmp_path / "repo"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    wf_dir = work / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    workflow = wf_dir / "ci.yml"
    workflow.write_text(
        "name: ci\njobs:\n  t:\n    steps:\n      - run: python -m pytest tests/\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=work, check=True)
    workflow.unlink()

    t = Task(
        repository="acme/gate",
        trigger_type="ci",
        ci_sha="abc123",
        status="RUNNING",
        branch="fixhub-fixes/ci-1-task-1",
    )
    db.add(t)
    db.commit()
    monkeypatch.setattr(settings, "CI_WEAKENING_GATE", "block")

    out = _svc._publish_task(
        db,
        t,
        path=str(work),
        branch=t.branch,
        summary="s",
        title="t",
        body="b",
    )
    assert out["status"] == "BLOCKED", out
    assert "ci_weakening_detected" in out["error"]
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "BLOCKED"
    types = [
        e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == t.id).all()
    ]
    assert "CI_WEAKENING_WARNING" in types
    assert "BLOCKED" in types


def test_no_changes_ci_red_is_needs_review(db, monkeypatch):
    from app.github import client as _gh
    from app.tasks import service as _svc

    _own(db)
    t = _ci_task(db, status="RUNNING")
    monkeypatch.setattr(_svc, "_repo_token", lambda db_, task_: "t")
    monkeypatch.setattr(
        _gh,
        "sha_check_conclusion",
        lambda *, token, full_name, sha: "failure",
    )
    out = _svc._no_changes_outcome(db, t)
    assert out["status"] == "NEEDS_REVIEW"
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "NEEDS_REVIEW"

    t2 = _ci_task(db, status="RUNNING", sha="def456")
    monkeypatch.setattr(
        _gh,
        "sha_check_conclusion",
        lambda *, token, full_name, sha: "success",
    )
    out2 = _svc._no_changes_outcome(db, t2)
    assert out2["status"] == "COMPLETED"


# --- CI watcher patience + evidence selection ---------------------------------


def test_ci_unobservable_patience_fails_honestly(db, monkeypatch):
    from app.github import client as _gh
    from app.tasks import ciwatch as _cw

    monkeypatch.setattr(settings, "CI_UNOBSERVABLE_MAX_TICKS", 2)
    monkeypatch.setattr(_cw, "_repo_token", lambda db_, repo: "t")
    monkeypatch.setattr(
        _gh,
        "sha_check_conclusion",
        lambda *, token, full_name, sha: "unknown",
    )
    t = Task(
        repository="acme/rel",
        trigger_type="ci",
        ci_sha="abc123",
        commit_sha="deadbeef",
        status="AWAITING_CI",
        dedupe_key="ci:acme/rel:abc123",
    )
    db.add(t)
    db.commit()
    assert _cw._check_one(db, t) == "pending"
    assert t.ci_unobservable_ticks == 1
    assert _cw._check_one(db, t) == "failed"
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "FAILED" and "could not be observed" in row.error
    assert row.dedupe_key is None
    assert (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == t.id, TaskEvent.type == "CI_UNOBSERVABLE")
        .count()
        == 1
    )


def test_ci_pending_does_not_consume_unobservable_patience(db, monkeypatch):
    from app.github import client as _gh
    from app.tasks import ciwatch as _cw

    monkeypatch.setattr(settings, "CI_UNOBSERVABLE_MAX_TICKS", 2)
    monkeypatch.setattr(_cw, "_repo_token", lambda db_, repo: "t")
    monkeypatch.setattr(
        _gh,
        "sha_check_conclusion",
        lambda *, token, full_name, sha: "pending",
    )
    t = Task(
        repository="acme/rel",
        trigger_type="ci",
        ci_sha="abc123",
        commit_sha="deadbeef",
        status="AWAITING_CI",
        ci_unobservable_ticks=1,
    )
    db.add(t)
    db.commit()
    assert _cw._check_one(db, t) == "pending"
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "AWAITING_CI" and row.ci_unobservable_ticks == 0


def test_ci_repair_round_resets_unobservable_patience(db, monkeypatch):
    """Old unknown-tick counts must not fail the fresh repair revision."""
    from app.github import client as _gh
    from app.tasks import ciwatch as _cw
    from app.tasks import queue as _queue

    monkeypatch.setattr(_cw, "_repo_token", lambda db_, repo: "t")
    monkeypatch.setattr(
        _gh, "sha_check_conclusion", lambda *, token, full_name, sha: "failure"
    )
    monkeypatch.setattr(_gh, "failed_log_tail", lambda **kw: "boom")
    monkeypatch.setattr(
        _queue,
        "enqueue_repair",
        lambda task_id: {"enqueued": True, "job_id": "repair-j"},
    )
    t = Task(
        repository="acme/rel",
        trigger_type="ci",
        ci_sha="abc123",
        commit_sha="deadbeef",
        status="AWAITING_CI",
        ci_attempt_count=1,
        ci_unobservable_ticks=25,
    )
    db.add(t)
    db.commit()
    assert _cw._check_one(db, t) == "repair_enqueued"
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "QUEUED"
    assert row.ci_attempt_count == 2
    assert row.ci_unobservable_ticks == 0


def test_failed_log_tail_selects_actual_failed_job(monkeypatch):
    from app.github import client as _gh

    jobs = [
        {"id": 1, "name": "setup", "conclusion": "success", "steps": []},
        {
            "id": 2,
            "name": "test",
            "conclusion": "failure",
            "steps": [{"name": "pytest", "conclusion": "failure"}],
        },
        {
            "id": 3,
            "name": "lint",
            "conclusion": "failure",
            "steps": [{"name": "ruff", "conclusion": "failure"}],
        },
    ]

    class _Resp:
        def __init__(self, payload=None, text=""):
            self._payload = payload
            self.text = text

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None, follow_redirects=False):
        if url.endswith("/jobs"):
            return _Resp(payload={"jobs": jobs})
        for job in jobs:
            if f"/jobs/{job['id']}/logs" in url:
                return _Resp(text=f"{job['name']} log\nFAILED {job['name']}\n")
        return _Resp(text="unknown log")

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "get", fake_get)

    out = _gh.failed_log_tail(token="t", full_name="o/r", run_id="1")
    assert "JOB test" in out and "test log" in out

    # A workflow name passed by mistake matches no job: still the failed one.
    out = _gh.failed_log_tail(token="t", full_name="o/r", run_id="1", job_name="CI")
    assert "JOB test" in out

    # An explicit failing job identity wins.
    out = _gh.failed_log_tail(token="t", full_name="o/r", run_id="1", job_name="lint")
    assert "JOB lint" in out

    # No failed job at all: fail honestly instead of reading jobs[0].
    jobs[:] = [{"id": 1, "name": "setup", "conclusion": "success", "steps": []}]
    with pytest.raises(RuntimeError):
        _gh.failed_log_tail(token="t", full_name="o/r", run_id="1")


# --- environment guards -------------------------------------------------------


def test_env_guard_rejects_unknown_value(monkeypatch):
    from app import main as _main

    monkeypatch.setattr(settings, "ENV", "production")
    with pytest.raises(RuntimeError, match="unknown ENV"):
        _main._enforce_production_guards()


def test_env_guard_covers_staging(monkeypatch):
    from app import main as _main

    monkeypatch.setattr(settings, "ENV", "staging")
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///./fixhub.db")
    with pytest.raises(RuntimeError, match="SQLite"):
        _main._enforce_production_guards()


def test_env_guard_staging_requires_firecracker(monkeypatch):
    from app import main as _main

    monkeypatch.setattr(settings, "ENV", "staging")
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", 1)
    monkeypatch.setattr(settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "x")
    monkeypatch.setattr(settings, "SANDBOX_BACKEND", "host")
    with pytest.raises(RuntimeError, match="SANDBOX_BACKEND"):
        _main._enforce_production_guards()
