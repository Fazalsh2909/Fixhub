"""Regression tests for the loop guards that stop the 2026-10 CI trace.

The observed failure: one model turn per tool call, alternating
`read_file(".github/workflows/ci.yml")`, `run_command("cat ...")` and
`git_status` for 38 calls until the 100k input-token budget fired. Nothing
was edited. Each guard below closes one way that loop could happen.
"""
import pytest

from app.agent import context as _ctx
from app.agent import loop as _loop
from app.llm.client import AssistantMessage, ToolCall


def _script(monkeypatch, script):
    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)
    return calls


def _run(monkeypatch, tmp_path, script, executor, **kwargs):
    calls = _script(monkeypatch, script)
    monkeypatch.setattr(_loop, "_execute", executor)
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type=kwargs.pop("trigger_type", "issue"),
        repository="acme/r",
        issue_title="t",
        issue_body="b",
        **kwargs,
    )
    return out, calls


def test_read_cache_survives_intervening_call(monkeypatch, tmp_path):
    """A successful `git_status` must not invalidate the read cache.

    Regression: `state_version` used to bump on every successful call, so any
    intervening action made the next identical read re-execute.
    """
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        return "x = 1\n" if name == "read_file" else "clean"

    out, _ = _run(
        monkeypatch,
        tmp_path,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "a.py"})]),
            AssistantMessage("", [ToolCall("2", "git_status", {})]),
            AssistantMessage("", [ToolCall("3", "read_file", {"path": "a.py"})]),
            AssistantMessage("done", []),
        ],
        executor,
    )
    assert executed == ["read_file", "git_status"]
    assert len([e for e in out.events if e.get("cached")]) == 1


def test_cat_counts_as_a_read_of_the_same_path(monkeypatch, tmp_path):
    """`cat <path>` is a read; a second one is not new evidence."""
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        return "x = 1\n" if name == "read_file" else "exit_code: 0\nstdout: x = 1"

    out, _ = _run(
        monkeypatch,
        tmp_path,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "ci.yml"})]),
            AssistantMessage(
                "", [ToolCall("2", "run_command", {"command": "cat ci.yml"})]
            ),
            AssistantMessage(
                "", [ToolCall("3", "run_command", {"command": "cat ci.yml"})]
            ),
            AssistantMessage("done", []),
        ],
        executor,
    )
    assert executed == ["read_file", "run_command"]
    assert any(e.get("read_budget") for e in out.events)


def test_read_budget_blocks_third_read(monkeypatch, tmp_path):
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        return "line\n" * 20

    out, _ = _run(
        monkeypatch,
        tmp_path,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "a.py"})]),
            AssistantMessage(
                "", [ToolCall("2", "read_file", {"path": "a.py", "offset": 201})]
            ),
            AssistantMessage(
                "", [ToolCall("3", "read_file", {"path": "a.py", "offset": 401})]
            ),
            AssistantMessage("done", []),
        ],
        executor,
    )
    assert executed == ["read_file", "read_file"]
    assert any(e.get("read_budget") for e in out.events)


@pytest.mark.parametrize(
    "blocked",
    [
        ToolCall("3", "edit_file", {"path": "b.py", "old": "x", "new": "y"}),
        ToolCall(
            "3",
            "write_file",
            {
                "path": ".github/workflows/ci.yml",
                "content": "continue-on-error: true\n",
            },
        ),
    ],
    ids=["gated", "prohibited"],
)
def test_blocked_write_grants_the_read_it_asked_for(monkeypatch, tmp_path, blocked):
    """A blocked write must not be followed by a read-budget refusal.

    Production 40371361810: the gate demanded evidence, then the per-path
    budget skipped the read that would have supplied it. Each blocked write
    now grants one read that bypasses the cache and both read budgets.
    """
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        return "x = 1\n" if name == "read_file" else "exit_code: 0\nstdout: x = 1"

    out, _ = _run(
        monkeypatch,
        tmp_path,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "a.py"})]),
            # Different offsets are distinct calls, so this one executes and
            # exhausts the per-path budget without adding depth.
            AssistantMessage(
                "", [ToolCall("2", "read_file", {"path": "a.py", "offset": 201})]
            ),
            AssistantMessage("", [blocked]),
            AssistantMessage("", [ToolCall("4", "read_file", {"path": "a.py"})]),
            AssistantMessage("done", []),
        ],
        executor,
    )

    block_ev = next(e for e in out.events if e.get("step") == 3)
    assert block_ev.get("blocked") is True
    assert block_ev.get("gated") or block_ev.get("prohibited")
    # a.py was read twice already; the repair read still runs.
    assert executed == ["read_file", "read_file", "read_file"]
    read_ev = next(e for e in out.events if e.get("step") == 4)
    assert read_ev.get("ok") is True and not read_ev.get("blocked")
    assert not read_ev.get("cached")


def test_batch_duplicate_executes_once(monkeypatch, tmp_path):
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        return "x = 1\n"

    out, _ = _run(
        monkeypatch,
        tmp_path,
        [
            AssistantMessage(
                "",
                [
                    ToolCall("1", "read_file", {"path": "a.py"}),
                    ToolCall("2", "read_file", {"path": "a.py"}),
                ],
            ),
            AssistantMessage("done", []),
        ],
        executor,
    )
    assert executed == ["read_file"]
    assert any(e.get("duplicate") for e in out.events)


def test_stall_breaker_stops_a_pure_read_loop(monkeypatch, tmp_path):
    """Three turns with no new evidence end the run with a legible reason."""
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        return "clean"

    out, _ = _run(
        monkeypatch,
        tmp_path,
        [AssistantMessage("", [ToolCall(str(i), "git_status", {})]) for i in range(6)],
        executor,
    )
    assert out.finished is False
    assert "no progress" in out.summary
    # Consecutive identical calls are already served from cache, so the tool
    # runs once; the point is that the run stops after 3 turns, not 6.
    assert len(executed) == 1
    assert out.iterations == 3
    assert any(e.get("tool") == "stall_nudge" for e in out.events)


def test_stall_breaker_stops_read_cat_status_rotation(monkeypatch, tmp_path):
    """The exact 2026-10 trace: read_file / cat / git_status, forever."""
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        return "name: CI\n" if name == "read_file" else "exit_code: 0\nstdout: name: CI"

    out, _ = _run(
        monkeypatch,
        tmp_path,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "ci.yml"})]),
            AssistantMessage(
                "", [ToolCall("2", "run_command", {"command": "cat ci.yml"})]
            ),
            AssistantMessage("", [ToolCall("3", "git_status", {})]),
            AssistantMessage("", [ToolCall("4", "read_file", {"path": "ci.yml"})]),
            AssistantMessage(
                "", [ToolCall("5", "run_command", {"command": "cat ci.yml"})]
            ),
            AssistantMessage("", [ToolCall("6", "git_status", {})]),
        ],
        executor,
    )
    assert out.finished is False
    assert "no progress" in out.summary
    assert out.iterations <= 5
    # Only genuinely new evidence was executed.
    assert executed == ["read_file", "run_command", "git_status"]


def test_stall_breaker_ignores_real_investigation(monkeypatch, tmp_path):
    """Distinct evidence keeps the loop alive; it only stops repetition."""
    executed = []

    def executor(ws, name, args):
        executed.append(name)
        if name == "read_file":
            return "x = 1\n"
        if name == "write_file":
            return "wrote a.py"
        return "clean"

    out, _ = _run(
        monkeypatch,
        tmp_path,
        [
            AssistantMessage("", [ToolCall("1", "read_file", {"path": "a.py"})]),
            AssistantMessage("", [ToolCall("2", "read_file", {"path": "b.py"})]),
            AssistantMessage("", [ToolCall("3", "read_file", {"path": "c.py"})]),
            AssistantMessage(
                "",
                [ToolCall("4", "write_file", {"path": "a.py", "content": "x = 2\n"})],
            ),
            AssistantMessage("fixed", []),
        ],
        executor,
    )
    assert out.finished is True
    assert executed == ["read_file", "read_file", "read_file", "write_file"]


def test_budget_guard_injects_converge(monkeypatch, tmp_path):
    def executor(ws, name, args):
        return "wrote"

    out, _ = _run(
        monkeypatch,
        tmp_path,
        [
            AssistantMessage(
                "", [ToolCall("1", "write_file", {"path": "a.py", "content": "x\n"})]
            ),
            AssistantMessage("fixed", []),
        ],
        executor,
        budget_guard=lambda: 0.7,
    )
    assert any(e.get("tool") == "budget_converge" for e in out.events)


def test_clip_logs_keeps_the_failing_tail():
    """CI failures live at the end; a head-only slice drops the signal."""
    logs = ("setup step output line\n" * 600) + (
        "FAILED tests/test_pagination.py::test_page - assert 3 == 4\n"
    )
    clipped = _ctx.clip_logs(logs, 6000)
    assert len(clipped) <= 6000
    assert "FAILED tests/test_pagination.py::test_page" in clipped
    assert "FAILED tests/test_pagination.py::test_page" in _ctx.failure_signal(clipped)


def test_file_changed_event_requires_a_successful_write():
    """A rejected/gated edit must not be reported as a file change."""
    from app.tasks.service import _is_file_change_event

    assert _is_file_change_event({"tool": "edit_file", "ok": True}) is True
    assert _is_file_change_event({"tool": "write_file", "ok": True}) is True
    assert _is_file_change_event({"tool": "edit_file", "ok": False}) is False
    assert (
        _is_file_change_event({"tool": "edit_file", "ok": True, "blocked": True})
        is False
    )
    assert _is_file_change_event({"tool": "read_file", "ok": True}) is False


def test_failure_signal_extracts_pytest_line():
    logs = (
        "some noise\n"
        "FAILED tests/test_pagination.py::test_page - assert 3 == 4\n"
        "E   AssertionError: assert 3 == 4\n"
    )
    sig = _ctx.failure_signal(logs)
    assert "FAILED tests/test_pagination.py::test_page" in sig
    assert "AssertionError" in sig


def test_task_message_keeps_full_workflow_and_signal():
    workflow = "name: CI\n" + ("jobs:\n" + "  backend:\n    steps:\n" * 400)
    ctx = _ctx.TaskContext(
        repository="acme/r",
        trigger_type="ci",
        ci=_ctx.CIContext(
            workflow_name="CI",
            workflow_file=".github/workflows/ci.yml",
            workflow_content=workflow,
            job="backend",
            failure_logs="FAILED tests/test_x.py::test_y - assert 1 == 2\n",
        ),
    )
    msg = _ctx.build_task_message(ctx)
    assert "Failure signal:" in msg
    assert "FAILED tests/test_x.py::test_y" in msg
    # The tail of the workflow must survive (it used to be cut at 1000 chars).
    assert "backend:" in msg
    assert len(msg) > 4000


@pytest.mark.parametrize("bad", ["", "   "])
def test_failure_signal_empty(bad):
    assert _ctx.failure_signal(bad) == ""


def test_edit_failure_names_an_absent_target_after_repeats():
    """Repeated not-found results must say the text is absent, not invite
    another guess at the same oldString."""
    first = _loop._edit_failure_directive(
        "ERROR: oldString not found", {"path": "ci.yml"}, not_found=1
    )
    repeated = _loop._edit_failure_directive(
        "ERROR: oldString not found", {"path": "ci.yml"}, not_found=2
    )
    assert "Re-read the exact section" in first
    assert "not present" in repeated
    assert "workspace revision" in repeated
    assert "Stop retrying" in repeated
