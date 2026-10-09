"""End-to-end: issue -> stubbed LLM agent -> real code change -> commit -> push.

Proves Milestone 1 locally without GitHub/LLM keys:
- fixture repo (bare remote) with a real bug (calc.add subtracts)
- Task row created as the webhook would
- LLM stubbed to behave like a real agent: read -> edit -> run tests -> finish
- run_task_inline clones, runs the loop, commits, pushes branch fixhub-fixes/*
- assert branch exists on remote, commit recorded, memory written from real changes
"""
import json
import subprocess

from app.agent import loop as _loop
from app.db.models import Memory, Task, TaskEvent
from app.llm.client import AssistantMessage, ToolCall
from app.tasks import service as _svc


def _git(path, *args):
    r = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (args, r.stderr[-500:])
    return r


def _fixture_repo(tmp_path):
    remote = tmp_path / "calc.git"
    _git(str(tmp_path), "init", "--bare", str(remote))
    work = tmp_path / "src"
    _git(str(tmp_path), "clone", str(remote), str(work))
    _git(str(work), "config", "user.email", "t@t.t")
    _git(str(work), "config", "user.name", "t")
    (work / "calc.py").write_text("def add(a, b):\n    return a - b  # BUG: should add\n")
    (work / "test_calc.py").write_text("from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n")
    _git(str(work), "add", "-A")
    _git(str(work), "commit", "-m", "init with bug")
    _git(str(work), "push", "-u", "origin", "HEAD:main")
    # Point the bare remote's HEAD at main so clones check out the branch.
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=str(remote), capture_output=True, timeout=30,
    )
    return str(remote)


def test_e2e_issue_to_push(db, tmp_path, monkeypatch):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    remote = _fixture_repo(tmp_path)

    db.add(Task(repository="acme/calc", trigger_type="issue", issue_number=1,
                issue_title="add() subtracts", issue_body="add(2,3) returns -1", status="RUNNING"))
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()

    # Scripted LLM: investigate then fix then verify. Reads microscopically like a real agent.
    script = [
        AssistantMessage("", [ToolCall("1", "read_file", {"path": "test_calc.py"})]),
        AssistantMessage("", [ToolCall("2", "read_file", {"path": "calc.py"})]),
        AssistantMessage("", [ToolCall("3", "edit_file", {"path": "calc.py", "old": "return a - b  # BUG: should add", "new": "return a + b"})]),
        AssistantMessage("", [ToolCall("4", "run_command", {"command": "python -c \"from calc import add; assert add(2,3)==5\""})]),
        AssistantMessage("Fixed add() to use + instead of -. Tests pass.", []),
    ]
    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)

    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "COMPLETED", out
    assert out["branch"] == f"fixhub-fixes/issue-1-task-{task.id}"

    db.expire_all()
    t = db.query(Task).filter(Task.id == task.id).first()
    assert t.commit_sha and len(t.commit_sha) == 40

    # Branch really pushed to remote?
    r = subprocess.run(["git", "ls-remote", remote, t.branch], capture_output=True, text=True, timeout=30)
    assert t.branch in r.stdout

    # Events recorded (tool metadata only, no chain-of-thought)?
    types = [e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == task.id).all()]
    for need in ("TASK_CREATED" if False else "AGENT_STARTED", "TOOL_CALL", "FILE_CHANGED", "COMMAND_RUN", "AGENT_FINISHED", "COMMIT_CREATED"):
        assert need in types, types

    # Memory came from the REAL change, not faked?
    mems = db.query(Memory).filter(Memory.repository == "acme/calc").all()
    assert mems, "memory must be written after a real task"
    blob = json.dumps([m.summary for m in mems])
    assert "calc" in blob.lower() or "add" in blob.lower()
