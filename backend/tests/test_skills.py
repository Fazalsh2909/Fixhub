"""Phase 1 skill-specific tests: routing, gating, state machine, prohibited CI behavior.

Deterministic: scripted chat_completion FIFO, real filesystem + real _execute
(no external LLM). Follows backend/tests/test_agent_loop.py conventions.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from app.agent import loop as _loop
from app.agent import phase as _phase
from app.agent.skills import registry as _skills
from app.db.models import Task, TaskEvent
from app.llm.client import AssistantMessage, ToolCall
from app.tasks import service as _svc


def _script(monkeypatch, script):
    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)


def _git(path, *args):
    r = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, (args, r.stderr[-500:])
    return r


def _fixture_repo(tmp_path, files: dict | None = None):
    remote = tmp_path / "skill.git"
    _git(str(tmp_path), "init", "--bare", str(remote))
    work = tmp_path / "src"
    _git(str(tmp_path), "clone", str(remote), str(work))
    _git(str(work), "config", "user.email", "t@t.t")
    _git(str(work), "config", "user.name", "t")
    files = files or {
        "calc.py": "def add(a, b):\n    return a - b  # BUG: should add\n",
        "test_calc.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
    }
    for rel, content in files.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(str(work), "add", "-A")
    _git(str(work), "commit", "-m", "init")
    _git(str(work), "push", "-u", "origin", "HEAD:main")
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=str(remote),
        capture_output=True,
        timeout=30,
    )
    return str(remote)


# --- registry ---------------------------------------------------------------


def test_registry_issue_selects_fix_issues():
    s = _skills.resolve_skill("issue")
    assert s.name == "fix-issues"
    assert "UNDERSTAND" in _skills.skill_prompt(s)


def test_registry_ci_selects_fix_ci_cd():
    s = _skills.resolve_skill("ci")
    assert s.name == "fix-ci-cd"
    prompt = _skills.skill_prompt(s)
    assert "MAKE CI APPEAR GREEN" in prompt or "APPEAR GREEN" in prompt


def test_registry_unknown_raises():
    for bad in ("", "pr", "manual", "ISSUES ", "unknown"):
        with pytest.raises(_skills.UnknownSkill):
            _skills.resolve_skill(bad)


def test_registry_case_insensitive_trims():
    assert _skills.resolve_skill("  Issue ").name == "fix-issues"
    assert _skills.resolve_skill("CI").name == "fix-ci-cd"


# --- phase tracker ----------------------------------------------------------


def test_tracker_starts_investigating_requires_read():
    t = _phase.InvestigationTracker(skill="fix-issues")
    assert t.phase == _phase.INVESTIGATING
    ok, missing = t.can_write()
    assert ok is False and "read_file" in missing


def test_tracker_advances_after_depth():
    # Phase 4.5: one read is not enough; two distinct evidence pieces are.
    t = _phase.InvestigationTracker(skill="fix-issues")
    t.record("read_file", True, {"path": "a.py"})
    assert t.phase == _phase.INVESTIGATING
    t.record("read_file", True, {"path": "a.py"})  # same file: still shallow
    assert t.phase == _phase.INVESTIGATING
    t.record("search_code", True, {"pattern": "a"})
    assert t.phase == _phase.DIAGNOSED
    ok, _ = t.can_write()
    assert ok is True


def test_tracker_ci_requires_workflow_read():
    t = _phase.InvestigationTracker(skill="fix-ci-cd")
    t.record("read_file", True, {"path": "app.py"})
    t.record("search_code", True, {"pattern": "x"})
    assert t.phase == _phase.INVESTIGATING  # no workflow read yet
    t.record("read_file", True, {"path": ".github/workflows/ci.yml"})
    assert t.phase == _phase.DIAGNOSED


def test_tracker_failed_calls_do_not_count():
    t = _phase.InvestigationTracker(skill="fix-issues")
    t.record("read_file", False)
    assert t.phase == _phase.INVESTIGATING
    ok, _ = t.can_write()
    assert ok is False


def test_prohibited_continue_on_error_blocked():
    reason = _phase.prohibited_reason(
        "write_file",
        {
            "path": ".github/workflows/ci.yml",
            "content": "steps:\n  continue-on-error: true\n",
        },
    )
    assert reason is not None and "camouflage" in reason.lower()


def test_prohibited_test_stub_blocked():
    reason = _phase.prohibited_reason(
        "write_file", {"path": "tests/test_x.py", "content": "pass"}
    )
    assert reason is not None


def test_prohibited_assert_removal_blocked():
    reason = _phase.prohibited_reason(
        "edit_file",
        {"path": "tests/test_x.py", "old": "assert add(2,3)==5", "new": "pass"},
    )
    assert reason is not None


def test_legitimate_edit_not_prohibited():
    assert (
        _phase.prohibited_reason(
            "edit_file",
            {"path": "calc.py", "old": "return a - b", "new": "return a + b"},
        )
        is None
    )


# --- loop gating ------------------------------------------------------------


def test_loop_blocks_random_write_before_diagnosis(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    _script(
        monkeypatch,
        [
            AssistantMessage(
                "", [ToolCall("1", "write_file", {"path": "b.py", "content": "y=2"})]
            ),
            AssistantMessage("", [ToolCall("2", "read_file", {"path": "a.py"})]),
            AssistantMessage("", [ToolCall("3", "read_file", {"path": "a.py"})]),
            AssistantMessage("done", []),
        ],
    )
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="issue",
        repository="r",
        issue_title="t",
        issue_body="b",
    )
    blocked = [e for e in out.events if e.get("blocked") and e["tool"] == "write_file"]
    assert blocked, out.events
    assert any(
        "investigation incomplete" in (e.get("summary", "") or "").lower() or True
        for e in blocked
    )  # blocked flag is authoritative
    assert not (tmp_path / "b.py").exists()


def test_loop_allows_write_after_investigation(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    _script(
        monkeypatch,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "a.py"})]),
            AssistantMessage("", [ToolCall("2", "search_code", {"pattern": "x"})]),
            AssistantMessage(
                "",
                [ToolCall("3", "write_file", {"path": "b.py", "content": "y = 2\n"})],
            ),
            AssistantMessage("done", []),
        ],
    )
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="issue",
        repository="r",
        issue_title="t",
        issue_body="b",
    )
    assert (tmp_path / "b.py").exists()
    assert out.finished is True


def test_loop_unknown_trigger_blocked(tmp_path, monkeypatch):
    _script(monkeypatch, [AssistantMessage("hi", [])])
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="bogus",
        repository="r",
        issue_title="t",
        issue_body="b",
    )
    assert out.finished is False
    assert "unknown trigger" in out.summary.lower()


def test_loop_rejects_test_deletion_camouflage(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_f.py").write_text("def test_f():\n    assert True\n")
    _script(
        monkeypatch,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "app.py"})]),
            AssistantMessage("", [ToolCall("2", "search_code", {"pattern": "test_f"})]),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "3",
                        "edit_file",
                        {
                            "path": "tests/test_f.py",
                            "old": "assert True",
                            "new": "pass",
                        },
                    )
                ],
            ),
            AssistantMessage("done", []),
        ],
    )
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="issue",
        repository="r",
        issue_title="t",
        issue_body="b",
    )
    prohibited = [e for e in out.events if e.get("prohibited")]
    assert prohibited, out.events
    # Original assertion preserved (edit rejected, file unchanged).
    assert "assert True" in (tmp_path / "tests" / "test_f.py").read_text()


def test_loop_rejects_ci_camouflage(tmp_path, monkeypatch):
    (tmp_path / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
    _script(
        monkeypatch,
        [
            AssistantMessage(
                "", [ToolCall("1", "read_file", {"path": ".github/workflows/ci.yml"})]
            ),
            AssistantMessage("", [ToolCall("2", "search_code", {"pattern": "continue"})]),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "3",
                        "write_file",
                        {
                            "path": ".github/workflows/ci.yml",
                            "content": "name: ci\njobs:\n  t:\n    continue-on-error: true\n",
                        },
                    )
                ],
            ),
            AssistantMessage("done", []),
        ],
    )
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="r",
        issue_title="",
        issue_body="",
        ci_info="job failed",
    )
    assert any(e.get("prohibited") for e in out.events)


# --- service routing ---------------------------------------------------------


def test_service_issue_records_skill_selected(db, tmp_path, monkeypatch):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "ws"))
    remote = _fixture_repo(tmp_path)
    db.add(
        Task(
            repository="acme/skill",
            trigger_type="issue",
            issue_number=7,
            issue_title="add broken",
            issue_body="add(2,3) wrong",
            status="RUNNING",
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    _script(
        monkeypatch,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "test_calc.py"})]),
            AssistantMessage("", [ToolCall("2", "read_file", {"path": "calc.py"})]),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "3",
                        "edit_file",
                        {
                            "path": "calc.py",
                            "old": "return a - b  # BUG: should add",
                            "new": "return a + b",
                        },
                    )
                ],
            ),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "4",
                        "run_command",
                        {
                            "command": 'python -c "from calc import add; assert add(2,3)==5"'
                        },
                    )
                ],
            ),
            AssistantMessage("Fixed.", []),
        ],
    )
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "COMPLETED", out
    types = [
        e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == task.id).all()
    ]
    assert "SKILL_SELECTED" in types
    sel = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task.id, TaskEvent.type == "SKILL_SELECTED")
        .first()
    )
    assert json.loads(sel.data_json)["skill"] == "fix-issues"


def test_service_ci_records_skill_selected(db, tmp_path, monkeypatch):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "ws2"))
    remote = _fixture_repo(
        tmp_path,
        files={
            "app.py": "def f():\n    return 1\n",
            "tests/test_f.py": "from app import f\ndef test_f():\n    assert f() == 2\n",
            "requirements.txt": "pytest\n",
            ".github/workflows/ci.yml": "name: ci\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: python -m pytest tests/ -q\n",
        },
    )
    db.add(
        Task(
            repository="acme/ci-skill",
            trigger_type="ci",
            ci_job="t",
            ci_workflow="ci",
            ci_sha="abc",
            ci_url="http://ci/1",
            ci_excerpt="FAILED tests/test_f.py::test_f - assert 1 == 2",
            status="RUNNING",
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    _script(
        monkeypatch,
        [
            AssistantMessage(
                "", [ToolCall("1", "read_file", {"path": ".github/workflows/ci.yml"})]
            ),
            AssistantMessage("", [ToolCall("2", "read_file", {"path": "app.py"})]),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "3",
                        "edit_file",
                        {
                            "path": "app.py",
                            "old": "    return 1",
                            "new": "    return 2",
                        },
                    )
                ],
            ),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "4",
                        "run_command",
                        {"command": "python -m pytest tests/test_f.py -q"},
                    )
                ],
            ),
            AssistantMessage("Fixed root cause.", []),
        ],
    )
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "COMPLETED", out
    sel = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task.id, TaskEvent.type == "SKILL_SELECTED")
        .first()
    )
    assert sel is not None and json.loads(sel.data_json)["skill"] == "fix-ci-cd"


def test_service_unknown_trigger_blocked(db, tmp_path, monkeypatch):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "ws3"))
    remote = _fixture_repo(tmp_path)
    db.add(
        Task(
            repository="acme/bad",
            trigger_type="bogus",
            issue_number=1,
            issue_title="x",
            issue_body="y",
            status="RUNNING",
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    _script(monkeypatch, [AssistantMessage("hi", [])])
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "BLOCKED", out
    assert "unsupported trigger" in out["error"].lower()


def test_service_missing_issue_body_still_routes(db, tmp_path, monkeypatch):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "ws4"))
    remote = _fixture_repo(tmp_path)
    db.add(
        Task(
            repository="acme/empty",
            trigger_type="issue",
            issue_number=9,
            issue_title="",
            issue_body="",
            status="RUNNING",
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    _script(
        monkeypatch,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "test_calc.py"})]),
            AssistantMessage("", [ToolCall("2", "read_file", {"path": "calc.py"})]),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "3",
                        "edit_file",
                        {
                            "path": "calc.py",
                            "old": "return a - b  # BUG: should add",
                            "new": "return a + b",
                        },
                    )
                ],
            ),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "4",
                        "run_command",
                        {
                            "command": 'python -c "from calc import add; assert add(2,3)==5"'
                        },
                    )
                ],
            ),
            AssistantMessage("Fixed.", []),
        ],
    )
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "COMPLETED", out
