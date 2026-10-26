"""Regression tests for the CI evidence + forced-action pipeline.

Observed failure (run 40338550350): the `check_run` context had no workflow
fields, `failing_logs_excerpt()` returned step metadata instead of logs, and
two `pip install` failures (exit 1) were recorded as successful actions. The
agent read for 15 calls and never edited.

These tests pin the four fixes:
1. metadata-only excerpts are not failure evidence;
2. check_run task messages carry the workflow and the failure signal;
3. a non-zero command is a failure and cannot be repeated;
4. after diagnosis a CI run gets two reads, then must edit.
"""
import inspect
import subprocess

import pytest

from app.agent import context as _ctx
from app.agent import loop as _loop
from app.agent import phase
from app.llm.client import AssistantMessage, ToolCall


def test_metadata_only_excerpt_is_not_failure_evidence():
    metadata = (
        "JOB backend conclusion=failure\n"
        "  step Set up Python conclusion=success\n"
        "  step pip install -r requirements.txt conclusion=failure"
    )
    assert _ctx.has_failure_signal(metadata) is False
    assert _ctx.has_failure_signal("check backend failed") is False
    assert _ctx.has_failure_signal("workflow CI failed on main") is False
    assert _ctx.has_failure_signal("") is False

    assert _ctx.has_failure_signal(
        "JOB backend failure signal:\n"
        "ERROR: Could not find a version that satisfies the requirement ruff\n"
    )
    assert _ctx.has_failure_signal(
        "FAILED tests/test_x.py::test_y - assert 1 == 2\n"
    )
    assert _ctx.has_failure_signal("Would reformat: apps/api/app/main.py")


def test_ci_task_message_supplies_evidence_without_nagging():
    ctx = _ctx.TaskContext(
        repository="acme/r",
        trigger_type="ci",
        ci=_ctx.CIContext(
            workflow_name="CI",
            workflow_file=".github/workflows/ci.yml",
            workflow_content="name: CI\n",
            job="backend",
            failure_logs=(
                "ERROR: Could not find a version that satisfies the requirement "
                "ruff==0.1.0\n"
            ),
        ),
    )
    msg = _ctx.build_task_message(ctx)
    assert "Failure signal:" in msg
    assert "Could not find a version" in msg
    assert "Workflow content:" in msg
    # Evidence is supplied; the prompt does not forbid inspecting it.
    assert "Do NOT read the workflow file" not in msg


def test_ci_task_message_without_logs_asks_for_reproduction():
    ctx = _ctx.TaskContext(
        repository="acme/r",
        trigger_type="ci",
        ci=_ctx.CIContext(job="backend", workflow_content="name: CI\n"),
    )
    msg = _ctx.build_task_message(ctx)
    assert "reproduce the failing step" in msg
    assert "not captured" in msg


def test_failing_logs_excerpt_returns_real_log_text(monkeypatch):
    from app.github import client as _gh

    calls = {}

    def fake_tail(**kwargs):
        calls.update(kwargs)
        return "JOB backend failure signal:\nFAILED tests/test_x.py::test_y"

    monkeypatch.setattr(_gh, "failed_log_tail", fake_tail)
    out = _gh.failing_logs_excerpt(token="t", full_name="a/b", run_id="42")
    assert "FAILED tests/test_x.py::test_y" in out
    assert calls["run_id"] == "42"


def test_enrich_ci_logs_returns_real_failure_tail(monkeypatch):
    from app.github import app_auth, client as _gh
    from app.tasks import service as _svc

    monkeypatch.setattr(app_auth, "installation_token", lambda iid: "t")
    monkeypatch.setattr(
        _gh,
        "failed_log_tail",
        lambda **kw: "JOB backend failure signal:\nFAILED tests/test_x.py::test_y",
    )
    out = _svc._enrich_ci_logs(
        repository="a/b",
        installation_id="1",
        run_id="42",
        job_name="backend",
        check_url="",
    )
    assert "FAILED tests/test_x.py::test_y" in out
    assert _ctx.has_failure_signal(out)


def test_phase_requires_a_code_read_or_reproduction_when_signal_supplied():
    tracker = phase.InvestigationTracker(skill="fix-ci-cd")
    tracker.seed_ci_workflow_context(".github/workflows/ci.yml")
    tracker.seed_ci_failure("FAILED tests/test_x.py::test_y")

    # Workflow content + a successful `ls` is not enough: the failure signal
    # names the code, so the agent must read code (or reproduce).
    assert tracker.evidence_met() is False
    tracker.record("run_command", True, {"command": "ls -la"})
    assert tracker.evidence_met() is False
    tracker.record("read_file", True, {"path": "apps/api/requirements.txt"})
    assert tracker.evidence_met() is True
    assert tracker.phase == phase.DIAGNOSED


def test_phase_reproduction_alone_is_enough():
    tracker = phase.InvestigationTracker(skill="fix-ci-cd")
    tracker.mark_reproduced()
    assert tracker.evidence_met() is True
    assert tracker.phase == phase.DIAGNOSED


def _script(monkeypatch, script):
    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)
    return calls


def _ci_ctx(tmp_path):
    return _ctx.TaskContext(
        repository="acme/r",
        workspace_root=str(tmp_path),
        trigger_type="ci",
        ci=_ctx.CIContext(
            workflow_name="CI",
            workflow_file=".github/workflows/ci.yml",
            workflow_content="name: CI\n",
            job="backend",
            failure_logs="FAILED tests/test_x.py::test_y - assert 1 == 2\n",
        ),
        skill="fix-ci-cd",
    )


def test_ci_post_diagnosis_read_budget_forces_an_edit(monkeypatch, tmp_path):
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        if name == "write_file":
            return "wrote app/main.py"
        return "x = 1\n"

    _script(
        monkeypatch,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "app/main.py"})]),
            AssistantMessage("", [ToolCall("2", "read_file", {"path": "app/one.py"})]),
            AssistantMessage("", [ToolCall("3", "read_file", {"path": "app/two.py"})]),
            AssistantMessage(
                "", [ToolCall("4", "read_file", {"path": "app/three.py"})]
            ),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "5",
                        "write_file",
                        {"path": "app/main.py", "content": "x = 2\n"},
                    )
                ],
            ),
            AssistantMessage("fixed", []),
        ],
    )
    monkeypatch.setattr(_loop, "_execute", executor)
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="acme/r",
        ctx=_ci_ctx(tmp_path),
    )

    # First read diagnoses; the next two reads are allowed; the fourth is
    # rejected and the edit still lands.
    assert executed == ["read_file", "read_file", "read_file", "write_file"]
    assert any(e.get("read_budget") for e in out.events)
    assert any(e.get("tool") == "write_file" for e in out.events)
    assert out.finished is True


def test_ci_failed_command_is_not_progress_and_cannot_repeat(monkeypatch, tmp_path):
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        if name == "run_command":
            return (
                "exit_code: 1\n"
                "stdout:\n"
                "ERROR: Could not find a version that satisfies the requirement ruff\n"
                "cwd: apps/api\n"
                "duration_ms: 12\n"
                "timed_out: false"
            )
        return "wrote apps/api/requirements.txt"

    _script(
        monkeypatch,
        [
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "1",
                        "run_command",
                        {"command": "pip install -r requirements.txt", "cwd": "apps/api"},
                    )
                ],
            ),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "2",
                        "run_command",
                        {"command": "pip install -r requirements.txt", "cwd": "apps/api"},
                    )
                ],
            ),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "3",
                        "write_file",
                        {
                            "path": "apps/api/requirements.txt",
                            "content": "ruff==0.1.0\n",
                        },
                    )
                ],
            ),
            AssistantMessage("fixed", []),
        ],
    )
    monkeypatch.setattr(_loop, "_execute", executor)
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="acme/r",
        ctx=_ci_ctx(tmp_path),
    )

    # The failed command runs once; the identical retry is blocked; the edit
    # proceeds. Previously exit 1 was recorded as ok=True and retried.
    assert executed == ["run_command", "write_file"]
    assert any(e.get("tool") == "command_failed" for e in out.events)
    assert any(e.get("blocked") and not e.get("read_budget") for e in out.events)
    assert out.finished is True


@pytest.mark.parametrize("bad", ["", "   "])
def test_has_failure_signal_empty(bad):
    assert _ctx.has_failure_signal(bad) is False


def test_ci_failed_edit_grants_a_repair_read_and_retries(monkeypatch, tmp_path):
    """A rejected edit must not end the run on the same turn.

    Production trace: two blocked reads (stall 2), then a correct `edit_file`
    whose oldString lost its YAML indentation. The edit failed, the turn was
    counted as a stall, and the run blocked at 3 with the fix never applied.
    """
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        if name == "edit_file":
            if args.get("old") == "flat":
                return "ERROR: oldString not found"
            return "EDITED app/main.py"
        return "x = 1\n"

    _script(
        monkeypatch,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "app/main.py"})]),
            AssistantMessage("", [ToolCall("2", "read_file", {"path": "app/one.py"})]),
            AssistantMessage("", [ToolCall("3", "read_file", {"path": "app/two.py"})]),
            # Two blocked reads: stalled_turns reaches 2 (the nudge threshold).
            AssistantMessage("", [ToolCall("4", "read_file", {"path": "app/three.py"})]),
            AssistantMessage("", [ToolCall("5", "read_file", {"path": "app/four.py"})]),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "6",
                        "edit_file",
                        {"path": "app/main.py", "old": "flat", "new": "x = 2\n"},
                    )
                ],
            ),
            AssistantMessage("", [ToolCall("7", "read_file", {"path": "app/main.py"})]),
            AssistantMessage(
                "",
                [
                    ToolCall(
                        "8",
                        "edit_file",
                        {
                            "path": "app/main.py",
                            "old": "x = 1",
                            "new": "x = 2",
                        },
                    )
                ],
            ),
            AssistantMessage("fixed", []),
        ],
    )
    monkeypatch.setattr(_loop, "_execute", executor)
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="acme/r",
        ctx=_ci_ctx(tmp_path),
    )

    assert out.finished is True
    assert "no progress" not in out.summary
    assert any(e.get("tool") == "edit_failed" for e in out.events)
    # The repair read ran even though the post-diagnosis budget was exhausted.
    assert executed.count("read_file") == 4
    assert executed[-2:] == ["read_file", "edit_file"]
    edits = [e for e in out.events if e.get("tool") == "edit_file"]
    assert edits and edits[-1].get("ok") is True
    assert edits[-1].get("step") == 8

def test_ci_failed_edit_cap_stops_thrashing(monkeypatch, tmp_path):
    """Six rejected edits end the run with a legible reason.

    Production trace 40361749612 spent 8 attempts on a target that did not
    exist in the workspace tree; the stall breaker never fired because every
    rejected edit counted as a strategy change.
    """
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        if name == "read_file":
            return "x = 1\n"
        return "ERROR: oldString not found. Closest lines in the file:\n1: name: CI"

    script = [
        AssistantMessage("", [ToolCall("0", "read_file", {"path": "app/main.py"})])
    ]
    script += [
        AssistantMessage(
            "",
            [
                ToolCall(
                    str(i + 1),
                    "edit_file",
                    {
                        "path": ".github/workflows/ci.yml",
                        # Vary the lengths so each rejected edit is a distinct
                        # call: the identical-failure guard (which hashes
                        # content by length) must not mask the thrash cap.
                        "old": f"target-{i}" + "x" * i,
                        "new": f"replacement-{i}" + "y" * i,
                    },
                )
            ],
        )
        for i in range(10)
    ]
    _script(monkeypatch, script)
    monkeypatch.setattr(_loop, "_execute", executor)
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="ci",
        repository="acme/r",
        ctx=_ci_ctx(tmp_path),
    )

    assert out.finished is False
    assert "edits were rejected" in out.summary
    assert any(e.get("tool") == "edit_cap" for e in out.events)
    assert executed.count("edit_file") == _loop.MAX_FAILED_EDIT_ATTEMPTS


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _repo_with_two_revisions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "ci.yml").write_text("name: CI\n# default branch\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "default")
    default_branch = _git(repo, "branch", "--show-current").stdout.strip()
    default_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "failing-branch")
    (repo / "ci.yml").write_text("name: CI\n# failing revision\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "failing")
    failing_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", default_branch)
    return repo, default_sha, failing_sha


def test_ci_workspace_pins_the_failing_revision(tmp_path):
    """The workspace must sit on the failing commit, not the default branch.

    Production trace 40361749612: the guest was provisioned from the default
    branch, so `.github/workflows/ci.yml` did not contain the step named in
    the CI evidence and every edit was rejected.
    """
    from app.tasks import service as _svc

    repo, default_sha, failing_sha = _repo_with_two_revisions(tmp_path)
    assert _svc._pin_ci_workspace(str(repo), failing_sha)["ok"] is True
    assert _svc._pin_ci_workspace(str(repo), failing_sha)["actual"] == failing_sha
    body = (repo / "ci.yml").read_text(encoding="utf-8")
    assert "failing revision" in body
    assert "default branch" not in body


def test_ci_workspace_blocks_an_unreachable_sha(tmp_path):
    """An unreachable failing SHA is a blocked task, never a default-branch
    fallback that silently changes the evidence."""
    from app.tasks import service as _svc

    repo, default_sha, _ = _repo_with_two_revisions(tmp_path)
    pin = _svc._pin_ci_workspace(str(repo), "0" * 40)
    assert pin["ok"] is False
    assert pin["reason"] == "ci_sha_unreachable"
    assert _svc._ws.head_sha(str(repo)) == default_sha


def test_ci_checkout_precedes_guest_provision():
    """Ordering guard: provision() streams the host tree into the guest, so
    the CI checkout must happen first (or the guest gets the wrong tree)."""
    from app.tasks import service as _svc

    src = inspect.getsource(_svc.run_task_inline)
    assert src.index("_pin_ci_workspace(") < src.index("_fc.provision(")
