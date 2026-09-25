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


def test_uncloned_real_repo_fails_loudly_instead_of_demo():
    """A real repo with no local workspace must FAIL with a clone-first
    message — never silently run against another repo instead."""
    db = _db()
    repo = Repository(
        full_name=f"acme/noclone-{uuid.uuid4().hex[:8]}",
        local_path="",
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = _task(db, repo, state="CREATED", title="should not touch demo")
    tid = task.id
    db.close()  # run_task_sync owns its own session; re-read after it commits
    out = run_task_sync(tid, force=True)
    assert out.get("status_code") == 400
    assert "clone it first" in out.get("error", "")
    db2 = SessionLocal()
    try:
        assert db2.query(Task).filter_by(id=tid).first().state == "FAILED"
    finally:
        db2.close()


def test_bogus_local_path_falls_back_to_demo():
    # Demo fixtures deleted: a bad local_path now 400s with clone-first.
    from fastapi import HTTPException

    from app.chat.router import resolve_workdir

    db = _db()
    repo = _repo(db)
    repo.local_path = "/nonexistent-fixhub-ws-xyz"
    db.commit()
    try:
        resolve_workdir(repo)
        assert False, "expected HTTPException for missing workspace"
    except HTTPException as e:
        assert e.status_code == 400
        assert "clone it first" in e.detail
    finally:
        db.close()


def test_approve_task_local_path_records_auto_approval():
    from app.models import VerificationRun

    db = _db()
    repo = _repo(db)
    task = _task(db, repo, state="REVIEWING", title="auto pr work")
    db.add(Patch(task_id=task.id, diff="diff --git a/x b/x", branch=""))
    # Approval requires recorded VERIFIED evidence — seed it.
    db.add(
        VerificationRun(
            task_id=task.id, check="suite", passed=True, status="PASS", required=True
        )
    )
    db.add(
        VerificationRun(
            task_id=task.id,
            check="regression",
            passed=True,
            status="PASS",
            required=True,
        )
    )
    db.commit()
    tid = task.id
    out = approve_task(db, task, approver="auto", decision="AUTO_APPROVED")
    assert out["status"] == "approved"  # no installation -> local record
    assert out["branch"] == f"fixhub/task-{tid}"  # custom tasks get unique branches
    assert db.query(Task).filter_by(id=tid).first().state == "COMMITTED"
    row = db.query(Approval).filter_by(task_id=tid).first()
    assert row.decision == "AUTO_APPROVED"
    db.close()


def test_approve_task_refuses_unverified():
    """Phase 7/8: a real diff with FAILED verification must not publish."""
    from app.models import VerificationRun

    db = _db()
    repo = _repo(db)
    task = _task(db, repo, state="REVIEWING", title="unverified work")
    db.add(Patch(task_id=task.id, diff="diff --git a/x b/x", branch=""))
    db.add(
        VerificationRun(
            task_id=task.id, check="suite", passed=False, status="FAIL", required=True
        )
    )
    db.add(
        VerificationRun(
            task_id=task.id,
            check="regression",
            passed=False,
            status="FAIL",
            required=True,
        )
    )
    db.commit()
    try:
        approve_task(db, task)
        raise AssertionError("should have refused unverified")
    except ApproveError as e:
        assert e.status_code == 409
        assert "VERIFIED" in e.detail or "verification" in e.detail
    assert db.query(Task).filter_by(id=task.id).first().state == "REVIEWING"
    db.close()


def test_approve_task_refuses_default_branch():
    """Phase 7: publishing to the repo's ACTUAL default branch is denied —
    including non-main defaults like trunk."""
    from app.models import VerificationRun

    db = _db()
    repo = _repo(db)
    repo.default_branch = "trunk"
    db.commit()
    task = _task(db, repo, state="REVIEWING", title="trunk work")
    # Force the task branch onto the default branch.
    db.add(Patch(task_id=task.id, diff="diff --git a/x b/x", branch="trunk"))
    db.add(
        VerificationRun(
            task_id=task.id, check="suite", passed=True, status="PASS", required=True
        )
    )
    db.add(
        VerificationRun(
            task_id=task.id,
            check="regression",
            passed=True,
            status="PASS",
            required=True,
        )
    )
    db.commit()
    try:
        approve_task(db, task)
        raise AssertionError("should have refused default branch")
    except ApproveError as e:
        assert e.status_code == 409
        assert "default branch" in e.detail
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


def test_run_slot_blocks_double_execution():
    """The sync /run endpoint and background launcher share one slot: while
    a run holds it, any second trigger is refused (never two runs on one
    task row — the failure seen live as VERIFYING→ANALYZING)."""
    from fastapi.testclient import TestClient

    import app.automation as automation_mod
    from app.main import create_app

    assert automation_mod.try_acquire(424243) is True
    try:
        assert automation_mod.try_acquire(424243) is False
        assert automation_mod.is_running(424243) is True
        client = TestClient(create_app(), raise_server_exceptions=False)
        r = client.post("/api/tasks/424243/run")
        assert r.status_code == 409
    finally:
        automation_mod.release(424243)
    assert automation_mod.is_running(424243) is False


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


def test_recover_interrupted_tasks_marks_stale_in_flight_failed():
    """Live failure (task 71): the server restarted mid-run and the task sat
    in ROOT_CAUSE_FOUND forever — no thread owns it, no trigger resumes it.
    Startup recovery must move such states to FAILED (audited, re-runnable)
    while leaving real rest points (DEBUGGING/CREATED/terminal) alone."""
    from app.automation import recover_interrupted_tasks
    from app.models import TaskEvent

    db = _db()
    repo = _repo(db, prefix="recover")
    stuck = _task(db, repo, state="ROOT_CAUSE_FOUND", title="stuck mid-run")
    rest = _task(db, repo, state="DEBUGGING", title="rest point")
    fresh = _task(db, repo, state="CREATED", title="never started")
    done = _task(db, repo, state="REVIEWING", title="finished")
    ids = (stuck.id, rest.id, fresh.id, done.id)
    try:
        n = recover_interrupted_tasks(db)
        # >= 1: the suite shares one DB, so an unrelated leftover from an
        # aborted run may be recovered too — what matters is OUR rows.
        assert n >= 1
        states = {t.id: t.state for t in db.query(Task).filter(Task.id.in_(ids)).all()}
        assert states[stuck.id] == "FAILED"
        assert states[rest.id] == "DEBUGGING"
        assert states[fresh.id] == "CREATED"
        assert states[done.id] == "REVIEWING"
        ev = (
            db.query(TaskEvent)
            .filter_by(task_id=stuck.id)
            .order_by(TaskEvent.id.desc())
            .first()
        )
        assert ev is not None and ev.stage == "FAILED" and "restart" in ev.message
    finally:
        db.query(TaskEvent).filter(TaskEvent.task_id.in_(ids)).delete(
            synchronize_session=False
        )
        db.query(Task).filter(Task.id.in_(ids)).delete(synchronize_session=False)
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()


def test_retryable_error_fails_fast_on_404_model_not_found():
    """Live failure (task 70): the gateway returned 404 'Model does not
    exist' and the runner burned all 3 attempts on an error that can never
    succeed by retrying. 404 must fail fast like 400/401/403."""
    from app.automation import _is_retryable_error

    assert _is_retryable_error(None) is True
    assert _is_retryable_error("provider 500: boom") is True
    assert _is_retryable_error("provider 429: rate limited") is True
    assert _is_retryable_error("verification FAIL") is True
    assert (
        _is_retryable_error(
            'provider 404: {"error":{"message":"Model '
            '\\"z-ai/glm-5.3-free\\" does not exist.",'
            '"type":"not_found_error","code":"not_found"}} (after 1 attempts)'
        )
        is False
    )
    assert _is_retryable_error("provider 400: bad request") is False
    assert _is_retryable_error("provider 401: unauthorized") is False
    assert _is_retryable_error("provider 403: forbidden") is False


def test_thin_issue_heuristic():
    """Vague issues (URL-only titles like task 71, one-worders like tasks
    117/122) must be detected before any LLM burn."""
    from app.automation import is_thin_issue

    for thin in [
        "",
        "   ",
        "t",
        "cap",
        "https://github.com/Fazalsh2909/nexus-mcp-intelligence",
        "issue #12",
        "Issue #7",
        # Task 239: a "Fix #N:" prefix must not launder a URL-only issue.
        "Fix #1: https://github.com/Fazalsh2909/nexus-mcp-intelligence",
        "issue #3: https://example.com/x",
        "Fix #9",
    ]:
        assert is_thin_issue(thin) is True, thin
    for ok in [
        "fix them",
        "Fix #1: login loop",
        "fix #12 login redirect loop",
        "add dark mode toggle",
        "make it production ready",
        "delete this line from .env.example",
        "JWT bug",
        "login redirect loop",
    ]:
        assert is_thin_issue(ok) is False, ok


def _failfast_repo(db, prefix="failfast"):
    import tempfile
    from pathlib import Path as _P

    # Automation now requires a real workspace for every repo (demo
    # fallback deleted): give the fixture a real dir so tests reach
    # engineer_issue instead of 400 clone-first.
    tmpbase = _P(tempfile.mkdtemp(prefix="fixhub-failfast-"))
    repo = Repository(
        full_name=f"demo/{prefix}-{uuid.uuid4().hex[:8]}",
        local_path=str(tmpbase),
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def test_run_task_sync_runs_simple_path_exactly_once(monkeypatch):
    """New contract: run_task_sync delegates to run_issue exactly once —
    no retry loops, no shared LLM-call budget, no RETRYING events."""
    calls: list[int] = []
    monkeypatch.setattr(
        "app.agent.issue_worker.run_issue",
        lambda rid, **k: calls.append(rid) or {"run_id": rid, "state": "FAILED"},
    )
    db = _db()
    repo = _failfast_repo(db)
    task = _task(db, repo, state="CREATED", title="simple delegation")
    tid, rid = task.id, repo.id
    db.close()
    try:
        out = run_task_sync(tid, force=True)
        assert out["state"] == "FAILED"
        assert calls == [tid], f"simple path must run exactly once, ran {calls}"
    finally:
        db2 = SessionLocal()
        try:
            from app.models import Memory, Patch, TaskEvent

            db2.query(TaskEvent).filter_by(task_id=tid).delete(
                synchronize_session=False
            )
            db2.query(Patch).filter_by(task_id=tid).delete(synchronize_session=False)
            db2.query(Memory).filter_by(repo_id=rid).delete(synchronize_session=False)
            db2.query(Task).filter_by(id=tid).delete(synchronize_session=False)
            db2.query(Repository).filter_by(id=rid).delete(synchronize_session=False)
            db2.commit()
        finally:
            db2.close()


def test_launch_task_refuses_needs_info_without_force(monkeypatch):
    """NEEDS_INFO tasks never auto-start; explicit force still works."""
    import app.automation as automation_mod

    db = _db()
    repo = _failfast_repo(db, prefix="needsinfo")
    task = _task(db, repo, state="NEEDS_INFO", title="t")
    tid, rid = task.id, repo.id
    db.close()
    try:
        assert launch_task(tid) == "needs-info"
        assert automation_mod.is_running(tid) is False
        done = threading.Event()

        def fake_sync(task_id, force=False):
            done.set()
            return {"task_id": task_id, "state": "DEBUGGING"}

        monkeypatch.setattr(automation_mod, "run_task_sync", fake_sync)
        assert launch_task(tid, force=True) == "started"
        assert done.wait(timeout=10)
    finally:
        automation_mod.release(tid)
        db2 = SessionLocal()
        try:
            from app.models import TaskEvent

            db2.query(TaskEvent).filter_by(task_id=tid).delete(
                synchronize_session=False
            )
            db2.query(Task).filter_by(id=tid).delete(synchronize_session=False)
            db2.query(Repository).filter_by(id=rid).delete(synchronize_session=False)
            db2.commit()
        finally:
            db2.close()


def test_run_task_sync_skips_needs_info_without_force():
    """The queue worker path (force=False) must not burn calls on NEEDS_INFO."""
    db = _db()
    repo = _failfast_repo(db, prefix="needsinfosync")
    task = _task(db, repo, state="NEEDS_INFO", title="t")
    tid, rid = task.id, repo.id
    db.close()
    try:
        out = run_task_sync(tid, force=False)
        assert out.get("skipped") is True
        db3 = SessionLocal()
        try:
            t = db3.query(Task).filter_by(id=tid).first()
            assert t.state == "NEEDS_INFO"
            assert not t.workspace_path
        finally:
            db3.close()
    finally:
        db2 = SessionLocal()
        try:
            from app.models import TaskEvent

            db2.query(TaskEvent).filter_by(task_id=tid).delete(
                synchronize_session=False
            )
            db2.query(Task).filter_by(id=tid).delete(synchronize_session=False)
            db2.query(Repository).filter_by(id=rid).delete(synchronize_session=False)
            db2.commit()
        finally:
            db2.close()


def test_chat_agent_task_runs_with_instruction_stored(monkeypatch):
    """Even a thin chat instruction starts a RUNNING run with the message
    stored — the agent (not a gate) decides what to do with it."""
    import app.chat.router as chat_router
    from app.models import ChatMessage, TaskEvent

    monkeypatch.setattr(chat_router, "launch_task", lambda *a, **k: "started")
    db = _db()
    repo = _failfast_repo(db, prefix="needsinfochat")
    rid = repo.id
    try:
        body = chat_router.ChatBody(repo=repo.full_name, message="t")
        reply, tid = chat_router._handle_agent_task(
            db, body, repo, {"kind": "agent_task", "instruction": "t"}
        )
        assert tid is not None
        db.refresh(repo)
        task = db.query(Task).filter_by(id=tid).first()
        assert task.state == "RUNNING"
        stored = (
            db.query(ChatMessage).filter_by(task_id=tid).order_by(ChatMessage.id).all()
        )
        assert stored and stored[0].content == "t"
    finally:
        db.query(ChatMessage).filter_by(task_id=tid).delete(
            synchronize_session=False
        ) if tid else None
        db.query(TaskEvent).filter_by(task_id=tid).delete(
            synchronize_session=False
        ) if tid else None
        db.query(Task).filter_by(repo_id=rid).delete(synchronize_session=False)
        db.query(Repository).filter_by(id=rid).delete(synchronize_session=False)
        db.commit()
        db.close()
