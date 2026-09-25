"""Deterministic failure classification (Phase 24). No Docker, no LLM.

CASE A: before FAIL / after PASS            -> no failure (None)
CASE B: before FAIL / after FAIL same sig   -> BASELINE_FAILURE
CASE C: before PASS / after FAIL            -> TASK_FAILURE
CASE D: no command configured               -> SKIPPED
CASE E: Docker unavailable                  -> INFRASTRUCTURE_FAILURE
CASE F: broken repo env (missing module)    -> ENVIRONMENT_FAILURE
"""

from app.verify.classify import (
    BASELINE_FAILURE,
    ENVIRONMENT_FAILURE,
    INFRASTRUCTURE_FAILURE,
    SKIPPED,
    TASK_FAILURE,
    TIMEOUT,
    UNKNOWN,
    classify_gate,
    should_debug,
)


def test_case_a_fail_to_pass_is_no_failure():
    out = "FAILED tests/test_total.py::test_total - assert 6 == 5"
    assert (
        classify_gate(
            check="regression",
            before_status="FAIL",
            before_output=out,
            after_status="PASS",
            after_output="1 passed",
            changed_files=["calc.py"],
        )
        is None
    )


def test_case_b_identical_failure_is_baseline():
    out = "FAILED tests/test_total.py::test_total - assert 6 == 5"
    c = classify_gate(
        check="regression",
        before_status="FAIL",
        before_output=out,
        after_status="FAIL",
        after_output=out,
        changed_files=["other.py"],
    )
    assert c is not None and c.category == BASELINE_FAILURE
    assert should_debug(c.category) is False


def test_case_c_new_failure_after_pass_is_task_failure():
    c = classify_gate(
        check="regression",
        before_status="PASS",
        before_output="2 passed",
        after_status="FAIL",
        after_output="FAILED tests/test_total.py::test_total - assert 6 == 5",
        changed_files=["calc.py"],
    )
    assert c is not None and c.category == TASK_FAILURE
    assert should_debug(c.category) is True


def test_case_d_no_command_configured_is_skipped():
    c = classify_gate(
        check="lint",
        before_status=None,
        before_output="",
        after_status="SKIPPED",
        after_output="skipped — no lint config in this repo",
        changed_files=[],
        command_configured=False,
    )
    assert c is not None and c.category == SKIPPED
    assert should_debug(c.category) is False


def test_case_e_no_docker_is_infrastructure():
    c = classify_gate(
        check="regression",
        before_status="FAIL",
        before_output="1 failed",
        after_status="ERROR",
        after_output=(
            "Verification unavailable: isolated execution environment required."
        ),
        changed_files=["calc.py"],
    )
    assert c is not None and c.category == INFRASTRUCTURE_FAILURE
    assert should_debug(c.category) is False


def test_case_f_broken_env_is_environment_not_task():
    c = classify_gate(
        check="regression",
        before_status="FAIL",
        before_output="No module named 'asyncpg'",
        after_status="FAIL",
        after_output="No module named 'asyncpg'",
        changed_files=["calc.py"],
    )
    assert c is not None and c.category == ENVIRONMENT_FAILURE
    assert should_debug(c.category) is False


def test_timeout_is_timeout():
    c = classify_gate(
        check="regression",
        before_status="PASS",
        before_output="1 passed",
        after_status="FAIL",
        after_output="timeout",
        changed_files=["calc.py"],
    )
    assert c is not None and c.category == TIMEOUT


def test_no_baseline_is_unknown_never_debug():
    c = classify_gate(
        check="regression",
        before_status=None,
        before_output="",
        after_status="FAIL",
        after_output="FAILED tests/x.py::t - ValueError: boom",
        changed_files=["calc.py"],
    )
    assert c is not None and c.category == UNKNOWN
    assert should_debug(c.category) is False


def test_new_failure_touching_patch_is_task_failure():
    c = classify_gate(
        check="targeted",
        before_status="FAIL",
        before_output="FAILED tests/a.py::t1 - ValueError: old",
        after_status="FAIL",
        after_output=(
            "FAILED tests/a.py::t1 - ValueError: old\n"
            "FAILED tests/auth/test_x.py::t - ValueError: new"
        ),
        changed_files=["app/auth/x.py"],
    )
    assert c is not None and c.category == TASK_FAILURE


def test_new_failure_far_from_patch_is_unknown_triage():
    c = classify_gate(
        check="targeted",
        before_status="FAIL",
        before_output="FAILED tests/a.py::t1 - ValueError: old",
        after_status="FAIL",
        after_output=(
            "FAILED tests/a.py::t1 - ValueError: old\n"
            "FAILED tests/z.py::t9 - KeyError: unrelated"
        ),
        changed_files=["app/mine.py"],
    )
    assert c is not None and c.category == UNKNOWN
    assert should_debug(c.category) is False


def test_only_task_failure_debugs():
    for cat in (
        BASELINE_FAILURE,
        ENVIRONMENT_FAILURE,
        INFRASTRUCTURE_FAILURE,
        TIMEOUT,
        SKIPPED,
        UNKNOWN,
    ):
        assert should_debug(cat) is False
    assert should_debug(TASK_FAILURE) is True
    assert should_debug(None) is False
