"""Orchestrator wiring: task + write_todos tools run inline with loop context."""

from pathlib import Path

from app.agent.orchestrator import engineer_issue
from app.db import SessionLocal, init_db
from app.llm.base import LLMProvider, LLMResponse
from app.models import Repository, Task, TaskEvent


class PlannerThenDelegateProvider(LLMProvider):
    """Turn 1: write a plan. Turn 2: delegate exploration. Then stop."""

    def __init__(self):
        self.n = 0
        self.saw_readonly_call = False

    def generate(self, messages, **kwargs):
        return LLMResponse(text="hi")

    def tool_call(self, messages, tools, **kwargs):
        self.n += 1
        names = {t.name for t in tools}
        if names == {"list_files", "read_file", "search_code"}:
            # inside the subagent: structural guarantee, new tools withheld
            self.saw_readonly_call = True
            assert "task" not in names and "write_todos" not in names
            return LLMResponse(text="auth is in token.py", tool_calls=[])
        assert {"task", "write_todos"} <= names  # main loop offers new tools
        if self.n == 1:
            return LLMResponse(
                text="planning",
                tool_calls=[
                    {
                        "name": "write_todos",
                        "arguments": '{"todos": [{"content": "Find auth", "activeForm": "Finding auth", "status": "in_progress"}]}',
                    }
                ],
            )
        if self.n == 2:
            return LLMResponse(
                text="delegating",
                tool_calls=[
                    {"name": "task", "arguments": '{"description": "where is auth?"}'}
                ],
            )
        return LLMResponse(text="done", tool_calls=[])


def test_plan_and_subagent_flow(tmp_path: Path):
    init_db()
    db = SessionLocal()
    repo = db.query(Repository).filter_by(full_name="demo/delegate").first()
    if repo is None:
        repo = Repository(full_name="demo/delegate")
        db.add(repo)
        db.commit()
        db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=1, title="delegate t", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    import app.agent.orchestrator as orch

    orig = orch.run_in_sandbox
    orch.run_in_sandbox = lambda *a, **k: {"ok": False, "output": "repro fail"}
    try:
        prov = PlannerThenDelegateProvider()
        engineer_issue(db, task, tmp_path, prov)
    finally:
        orch.run_in_sandbox = orig
    assert prov.saw_readonly_call, "subagent never ran with its read-only toolset"
    stages = [e.stage for e in db.query(TaskEvent).filter_by(task_id=task.id).all()]
    assert "PLAN" in stages
    assert "SUBAGENT" in stages
    tool_msgs = [
        e.message
        for e in db.query(TaskEvent).filter_by(task_id=task.id, stage="TOOL").all()
    ]
    assert any(m.startswith("write_todos ok=True") for m in tool_msgs)
    assert any(m.startswith("task ok=True") for m in tool_msgs)
    assert any("token.py" in m for m in tool_msgs)  # subagent report flows back
    db.close()
