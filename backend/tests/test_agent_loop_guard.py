"""Regression tests for CI evidence handling and file-change accounting."""
import pytest

from app.agent import context as _ctx


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
    """A rejected edit must not be reported as a file change."""
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
