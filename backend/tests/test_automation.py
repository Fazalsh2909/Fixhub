"""Automation core: shared approve gate, background run guards, chat wiring."""

import threading
import uuid

import app.automation as automation
from app.automation import ApproveError, approve_task, launch_task, run_task_sync
from app.db import SessionLocal, init_db
from app.models import Approval, Patch, Repository, Task


def _db():
    init_db()
    return SessionLocal()


def _repo(db, prefix="auto") -> Repository:
    repo = Repository(
        full_name=f"demo/{prefix}-{uuid.uuid4().hex[:8]}",
        # no installation_id: publish stays local, no network in tests
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def _task(db, repo, state="REVIEWING", title="auto work", issue=0) -> Task:
    task = Task(repo_id=repo.id, issue_number=issue, title=title, state=state)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def test_run_unknown_task_is_404():
    out = run_task_sync(999999999, force=True)
    assert out["status_code"] == 404


def test_run_skips_terminal_state_without_force():
    db = _db()
    repo = _repo(db)
    task = _task(db, repo, state="REVIEWING")
    tid = task.id
    db.close()
    out = run_task_sync(tid)
    assert out.get("skipped") is True
    assert out["state"] == "REVIEWING"


def test_bogus_local_path_falls_back_to_demo():
    # resolve_workdir falls back to the bundled demo (same rule the old
    # endpoint used) — a bad local_path never 400s, it runs on demo.
    from app.chat.router import resolve_workdir

    db = _db()
    repo = _repo(db)
    repo.local_path = "/nonexistent-fixhub-ws-xyz"
    db.commit()
    workdir = resolve_workdir(repo)
    assert workdir.name == "fastapi-jwt"
    assert workdir.is_dir()
    db.close()


def test_approve_task_local_path_records_auto_approval():
    db = _db()
    repo = _repo(db)
    task = _task(db, repo, state="REVIEWING", title="auto pr work")
    db.add(Patch(task_id=task.id, diff="diff --git a/x b/x", branch=""))
    db.commit()
    tid = task.id
    out = approve_task(db, task, approver="auto", decision="AUTO_APPROVED")
    assert out["status"] == "approved"  # no installation -> local record
    assert out["branch"] == f"fixhub/task-{tid}"  # custom tasks get unique branches
    assert db.query(Task).filter_by(id=tid).first().state == "COMMITTED"
    row = db.query(Approval).filter_by(task_id=tid).first()
    assert row.decision == "AUTO_APPROVED"
    db.close()


def test_approve_task_refuses_empty_diff_and_bad_state():
    db = _db()
    repo = _repo(db)
    bad_state = _task(db, repo, state="DEBUGGING")
    try:
        approve_task(db, bad_state)
        raise AssertionError("should have refused")
    except ApproveError as e:
        assert e.status_code == 409
    no_diff = _task(db, repo, state="REVIEWING")
    db.add(Patch(task_id=no_diff.id, diff="(no files changed)", branch=""))
    db.commit()
    try:
        approve_task(db, no_diff)
        raise AssertionError("should have refused")
    except ApproveError as e:
        assert e.status_code == 409
    db.close()


def test_launch_task_dedupes_concurrent_runs(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_run(task_id, force=False):
        started.set()
        assert release.wait(timeout=10)
        return {"task_id": task_id, "state": "DEBUGGING"}

    monkeypatch.setattr(automation, "run_task_sync", slow_run)
    assert launch_task(424242) == "started"
    assert started.wait(timeout=10)
    assert launch_task(424242) == "already-running"
    release.set()


def test_chat_agent_task_creates_and_launches(monkeypatch):
    import app.chat.router as chat_router

    calls = []
    monkeypatch.setattr(
        chat_router,
        "launch_task",
        lambda tid, force=False: calls.append(tid) or "started",
    )
    db = _db()
    repo = _repo(db, prefix="chat-agent")
    body = chat_router.ChatBody(repo=repo.full_name, message="add dark mode toggle")
    intent = chat_router.parse_intent(body.message)
    assert intent["kind"] == "agent_task"
    reply, tid = chat_router._handle_agent_task(db, body, repo, intent)
    assert tid is not None and calls == [tid]
    assert f"Task #{tid}" in reply
    assert db.query(Task).filter_by(id=tid).first().title == "add dark mode toggle"
    db.close()


def test_chat_run_request_without_tasks():
    import app.chat.router as chat_router

    db = _db()
    repo = _repo(db, prefix="chat-run-empty")
    body = chat_router.ChatBody(repo=repo.full_name, message="run")
    reply, tid = chat_router._handle_run_request(db, body, repo)
    assert tid is None
    assert "No tasks yet" in reply
    db.close()
