"""Skill routing tests: registry resolution and service-level skill selection.

Deterministic: scripted chat_completion FIFO, real filesystem + real _execute
(no external LLM). Follows backend/tests/test_agent_loop.py conventions.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from app.agent import loop as _loop
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


def test_registry_ci_selects_ci_repair():
    s = _skills.resolve_skill("ci")
    assert s.name == "ci-repair"
    assert s.doc_path == "CI_REPAIR.md"
    prompt = _skills.skill_prompt(s)
    assert "not to make the run report success" in prompt
    assert "continue-on-error" in prompt
    # The honest-finish contract is part of the instruction doc itself.
    assert "finish without editing" in prompt


def test_registry_unknown_raises():
    for bad in ("", "pr", "manual", "ISSUES ", "unknown"):
        with pytest.raises(_skills.UnknownSkill):
            _skills.resolve_skill(bad)


def test_registry_case_insensitive_trims():
    assert _skills.resolve_skill("  Issue ").name == "fix-issues"
    assert _skills.resolve_skill("CI").name == "ci-repair"


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
    head_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    db.add(
        Task(
            repository="acme/ci-skill",
            trigger_type="ci",
            ci_job="t",
            ci_workflow="ci",
            ci_sha=head_sha,
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
    assert sel is not None and json.loads(sel.data_json)["skill"] == "ci-repair"


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


_CANARY_WORKFLOW = (
    "name: ci\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - name: tests\n"
    "        run: python -m pytest tests/ -q\n"
    "      - name: FixHub canary (deliberate failure)\n"
    "        run: exit 1\n"
)


def _ci_task_row(db, remote, repository):
    head_sha = _git(remote, "rev-parse", "HEAD").stdout.strip()
    db.add(
        Task(
            repository=repository,
            trigger_type="ci",
            ci_job="t",
            ci_workflow="ci",
            ci_sha=head_sha,
            ci_url="http://ci/1",
            ci_excerpt="process completed with exit code 1",
            status="RUNNING",
        )
    )
    db.commit()
    return db.query(Task).order_by(Task.id.desc()).first()


def test_service_ci_no_change_ci_red_is_needs_review(db, tmp_path, monkeypatch):
    """Regression: an intentional CI failure (deliberate canary step) ends
    with zero workspace edits, and the honest loop finish routes through
    _no_changes_outcome to NEEDS_REVIEW (no_changes_ci_red) — never a fake
    COMPLETED and never FAILED."""
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "ws5"))
    remote = _fixture_repo(
        tmp_path,
        files={
            "app.py": "def f():\n    return 1\n",
            "tests/test_f.py": "from app import f\ndef test_f():\n    assert f() == 1\n",
            ".github/workflows/ci.yml": _CANARY_WORKFLOW,
        },
    )
    task = _ci_task_row(db, remote, "acme/ci-no-change")
    _script(
        monkeypatch,
        [
            AssistantMessage(
                "", [ToolCall("1", "read_file", {"path": ".github/workflows/ci.yml"})]
            ),
            AssistantMessage(
                "The failing step is an intentional canary; no safe fix "
                "exists — left unchanged for human review.",
                [],
            ),
        ],
    )
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "NEEDS_REVIEW", out
    assert out.get("no_changes") is True
    review = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task.id, TaskEvent.type == "NEEDS_REVIEW")
        .first()
    )
    assert json.loads(review.data_json)["reason"] == "no_changes_ci_red"
    db.expire_all()
    row = db.query(Task).filter(Task.id == task.id).first()
    assert row.status == "NEEDS_REVIEW"


def test_service_ci_workflow_step_removal_blocked_at_publish(db, tmp_path, monkeypatch):
    """Regression: 'fixing' CI by deleting a workflow run step dies at the
    publishing gate (ci_weakening_detected); nothing is ever pushed."""
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "ws6"))
    monkeypatch.setattr(_settings, "CI_WEAKENING_GATE", "block")
    remote = _fixture_repo(
        tmp_path,
        files={
            "app.py": "def f():\n    return 1\n",
            ".github/workflows/ci.yml": _CANARY_WORKFLOW,
        },
    )
    task = _ci_task_row(db, remote, "acme/ci-gate")
    _script(
        monkeypatch,
        [
            AssistantMessage(
                "", [ToolCall("1", "read_file", {"path": ".github/workflows/ci.yml"})]
            ),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "2",
                        "edit_file",
                        {
                            "path": ".github/workflows/ci.yml",
                            "old": (
                                "      - name: tests\n"
                                "        run: python -m pytest tests/ -q\n"
                                "      - name: FixHub canary (deliberate failure)\n"
                                "        run: exit 1\n"
                            ),
                            "new": (
                                "      - name: tests\n"
                                "        run: python -m pytest tests/ -q\n"
                            ),
                        },
                    )
                ],
            ),
            AssistantMessage("Removed the failing step.", []),
        ],
    )
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "BLOCKED", out
    assert "ci_weakening_detected" in (out.get("error") or "")
    db.expire_all()
    row = db.query(Task).filter(Task.id == task.id).first()
    assert row.status == "BLOCKED"
    types = [
        e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == task.id).all()
    ]
    assert "CI_WEAKENING_WARNING" in types
    assert "BLOCKED" in types
