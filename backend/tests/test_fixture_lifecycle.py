"""Deterministic lifecycle on tests/fixtures/mini_repo (req 19).

Known bug (app.add subtracts), failing test, CI workflow, expected fix.
Scripted LLM (no network) exercises the full loop the way the target trace
should look: read workflow -> read failure/test -> read source -> edit ->
verify -> gates -> commit -> push -> PR-allowed. No dependency on Nexus.
"""
import json
import os
import shutil
import subprocess

from app.agent import loop as _loop
from app.db.models import Task, TaskEvent
from app.llm.client import AssistantMessage, ToolCall
from app.tasks import service as _svc

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "mini_repo")


def _git(path, *args):
    r = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (args, r.stderr[-500:])
    return r


def _fixture_remote(tmp_path):
    remote = tmp_path / "mini.git"
    _git(str(tmp_path), "init", "--bare", str(remote))
    work = tmp_path / "src"
    _git(str(tmp_path), "clone", str(remote), str(work))
    _git(str(work), "config", "user.email", "t@t.t")
    _git(str(work), "config", "user.name", "t")
    for root, _, files in os.walk(FIXTURE):
        for f in files:
            src = os.path.join(root, f)
            rel = os.path.relpath(src, FIXTURE)
            dst = os.path.join(str(work), rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
    _git(str(work), "add", "-A")
    _git(str(work), "commit", "-m", "mini repo with bug")
    _git(str(work), "push", "-u", "origin", "HEAD:main")
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=str(remote), capture_output=True, timeout=30)
    return str(remote)


def test_fixture_full_lifecycle(db, tmp_path, monkeypatch):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    remote = _fixture_remote(tmp_path)

    db.add(Task(repository="acme/mini", trigger_type="ci", ci_job="backend",
                ci_workflow="ci", ci_sha="abc", ci_url="http://ci/1",
                ci_excerpt="backend failed", status="RUNNING"))
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()

    script = [
        AssistantMessage("", [ToolCall("1", "read_file", {"path": ".github/workflows/ci.yml"})]),
        AssistantMessage("", [ToolCall("2", "read_file", {"path": "tests/test_app.py"})]),
        AssistantMessage("", [ToolCall("3", "read_file", {"path": "app.py"})]),
        AssistantMessage("", [ToolCall("4", "edit_file", {"path": "app.py",
            "old": "return a - b  # BUG: should add", "new": "return a + b"})]),
        AssistantMessage("", [ToolCall("5", "run_command",
                                       {"command": "python -m pytest tests/ -q", "cwd": "."})]),
        AssistantMessage("Fixed add() to use +. Tests and gates pass.", []),
    ]
    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)

    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "COMPLETED", out
    assert out["branch"] == f"fixhub-fixes/ci-abc-task-{task.id}"

    db.expire_all()
    t = db.query(Task).filter(Task.id == task.id).first()
    assert t.commit_sha and len(t.commit_sha) == 40
    r = subprocess.run(["git", "ls-remote", remote, t.branch],
                       capture_output=True, text=True, timeout=30)
    assert t.branch in r.stdout

    types = [e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == task.id).all()]
    for need in ("AGENT_STARTED", "TOOL_CALL", "FILE_CHANGED", "COMMAND_RUN",
                 "AGENT_FINISHED", "VALIDATION_STARTED", "VALIDATION_PASSED",
                 "COMMIT_CREATED"):
        assert need in types, types
    # The agent read the workflow first (CI-first), not blind browsing.
    reads = [json.loads(e.data_json)["args"].get("path", "")
             for e in db.query(TaskEvent).filter(TaskEvent.task_id == task.id,
                                                 TaskEvent.type == "TOOL_CALL").all()]
    assert reads[0] == ".github/workflows/ci.yml"
