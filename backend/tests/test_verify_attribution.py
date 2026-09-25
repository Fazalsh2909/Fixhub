"""Attribution architecture: normalized failure signatures.

Signatures (not raw stdout) are what baseline-vs-after comparison runs on,
so the same failure at a shifted line number still matches.
"""

import pytest

from app.verify.pipeline import (
    ATTR_BASELINE,
    ATTR_DEP,
    ATTR_ENV,
    ATTR_TASK,
    ATTR_UNKNOWN,
    ATTR_UNRELATED,
    FAILED,
    INCONCLUSIVE,
    WITH_LIMITATIONS,
    attribute_gate,
    attribute_verdict,
    build_verify_feedback,
)
from app.verify.pipeline import (
    GateResult as G,
)
from app.verify.signatures import (
    compare_signatures,
    parse_mypy,
    parse_process_markers,
    parse_pytest,
)


def test_pytest_failure_signature():
    out = (
        "FAILED tests/test_llm.py::TestGetLLMProvider::test_x - ExceptionGroup: boom\n"
        "1 failed in 0.12s"
    )
    assert parse_pytest(out) == [
        "pytest:tests/test_llm.py::TestGetLLMProvider::test_x:ExceptionGroup"
    ]


def test_pytest_collection_error_signature():
    out = (
        "ERROR collecting tests/test_llm.py\nModuleNotFoundError: No module named 'mcp'"
    )
    sigs = parse_pytest(out)
    assert sigs == ["pytest-collect:tests/test_llm.py"]


def test_pytest_empty_output_no_signatures():
    assert parse_pytest("") == []
    assert parse_pytest("3 passed in 0.5s") == []


def test_mypy_signature_ignores_line_numbers():
    a = "app/orchestration/tools_postgres.py:3: error: Skipping analyzing [import-untyped]"
    b = "app/orchestration/tools_postgres.py:47: error: Skipping analyzing [import-untyped]"
    assert (
        parse_mypy(a)
        == parse_mypy(b)
        == ["mypy:app/orchestration/tools_postgres.py:import-untyped"]
    )


def test_mypy_without_code():
    out = "app/x.py:10: error: Something broke"
    assert parse_mypy(out) == ["mypy:app/x.py:error"]


def test_process_markers():
    assert parse_process_markers('exec: "git": executable file not found in $PATH') == (
        "ENVIRONMENT",
        "missing-executable:git",
    )
    assert parse_process_markers("timeout") == ("TIMEOUT", "timeout")
    assert parse_process_markers("/usr/local/bin/python: No module named pytest") == (
        "DEPENDENCY",
        "missing-module:pytest",
    )
    assert parse_process_markers("Connection refused by db:5432") == (
        "ENVIRONMENT",
        "connection-refused",
    )
    assert parse_process_markers("denied by tool policy: chained") == (
        "CONFIGURATION",
        "policy-denial",
    )
    assert parse_process_markers("All checks passed!") == (None, "")


def test_compare_signatures():
    base = ["pytest:a.py::t1:ValueError", "mypy:b.py:import-untyped"]
    after = ["pytest:a.py::t1:ValueError", "pytest:c.py::t2:KeyError"]
    diff = compare_signatures(base, after)
    assert diff["unchanged"] == ["pytest:a.py::t1:ValueError"]
    assert diff["new"] == ["pytest:c.py::t2:KeyError"]
    assert diff["resolved"] == ["mypy:b.py:import-untyped"]


def test_compare_empty_baseline_marks_all_new():
    diff = compare_signatures([], ["pytest:a.py::t1:ValueError"])
    assert diff["new"] == ["pytest:a.py::t1:ValueError"]
    assert diff["unchanged"] == [] and diff["resolved"] == []


@pytest.mark.parametrize(
    "addr_a,addr_b",
    [("0x7f1a2b3c", "0x9e8d7c6b"), ("in 12.34s", "in 0.01s")],
)
def test_normalization_ignores_addresses_and_timings(addr_a, addr_b):
    a = f"FAILED tests/x.py::t - ValueError: bad object at {addr_a} after {addr_b}"
    b = "FAILED tests/x.py::t - ValueError: bad object at 0x000000 in 9.99s"
    assert parse_pytest(a) == parse_pytest(b)


def _gate(check, status, output="", required=True, sigs=(), attribution="NONE"):
    return G(check, status, required, output, attribution, tuple(sigs), 0)


def test_attribute_new_failure_in_changed_file_is_task_failure():
    base = _gate("suite", "PASS", "3 passed")
    after = _gate(
        "suite",
        "FAIL",
        "FAILED tests/auth/test_x.py::t - ValueError: bad",
        sigs=("pytest:tests/auth/test_x.py::t:ValueError",),
    )
    assert attribute_gate(after, base, ["app/auth/x.py"]) == ATTR_TASK


def test_attribute_identical_failure_is_baseline():
    out = "FAILED tests/a.py::t1 - ValueError: x"
    sig = ("pytest:tests/a.py::t1:ValueError",)
    base = _gate("suite", "FAIL", out, sigs=sig)
    after = _gate("suite", "FAIL", out, sigs=sig)
    assert attribute_gate(after, base, ["app/other.py"]) == ATTR_BASELINE


def test_attribute_new_failure_elsewhere_is_unrelated():
    base = _gate(
        "suite",
        "FAIL",
        "FAILED tests/a.py::t1 - ValueError",
        sigs=("pytest:tests/a.py::t1:ValueError",),
    )
    after = _gate(
        "suite",
        "FAIL",
        "FAILED tests/a.py::t1 - ValueError\nFAILED tests/z.py::t9 - KeyError",
        sigs=(
            "pytest:tests/a.py::t1:ValueError",
            "pytest:tests/z.py::t9:KeyError",
        ),
    )
    assert attribute_gate(after, base, ["app/mine.py"]) == ATTR_UNRELATED


def test_attribute_env_markers_win():
    assert (
        attribute_gate(
            _gate("suite", "FAIL", "No module named 'asyncpg'"),
            _gate("suite", "FAIL", "No module named 'asyncpg'"),
            [],
        )
        == ATTR_DEP
    )
    assert (
        attribute_gate(
            _gate("suite", "FAIL", 'exec: "git": executable file not found'),
            None,
            [],
        )
        == ATTR_ENV
    )


def test_verdict_with_limitations_no_debug():
    sig = ("pytest:tests/a.py::t1:ValueError",)
    base = [_gate("suite", "FAIL", "x", sigs=sig)]
    after = [_gate("suite", "FAIL", "x", sigs=sig)]
    verdict, summary, debug, state = attribute_verdict(after, base, ["app/o.py"])
    assert verdict == WITH_LIMITATIONS
    assert debug is False
    assert state == "READY_FOR_APPROVAL"
    assert summary["pre_existing"] == 1 and summary["new"] == []


def test_verdict_failed_debug_on_new_task_failure():
    base = [_gate("suite", "PASS", "3 passed")]
    after = [
        _gate(
            "suite",
            "FAIL",
            "FAILED tests/auth/test_x.py::t - ValueError",
            sigs=("pytest:tests/auth/test_x.py::t:ValueError",),
        )
    ]
    verdict, summary, debug, state = attribute_verdict(after, base, ["app/auth/x.py"])
    assert verdict == FAILED
    assert debug is True
    assert state == "DEBUGGING"
    assert summary["new"] == ["pytest:tests/auth/test_x.py::t:ValueError"]


def test_verdict_legacy_no_baseline_matches_overall():
    after = [_gate("suite", "FAIL", "boom")]
    verdict, _summary, debug, state = attribute_verdict(after, [], [])
    assert verdict == FAILED
    assert debug is False
    assert state == "DEBUGGING"


def test_verdict_inconclusive_when_baseline_unassessed():
    base = [_gate("suite", "SKIPPED", "no config", required=True)]
    after = [_gate("suite", "FAIL", "weird unparseable crash !!!")]
    verdict, _summary, debug, state = attribute_verdict(after, base, [])
    assert verdict == INCONCLUSIVE
    assert debug is False
    assert state == "DEBUGGING"


def test_verdict_unknown_attribution_is_inconclusive():
    assert (
        attribute_gate(_gate("suite", "FAIL", "???"), None, ["app/x.py"])
        == ATTR_UNKNOWN
    )


def test_build_verify_feedback_scoped_and_bounded():
    summary = {
        "verdict": FAILED,
        "new": [f"pytest:tests/x.py::t{i}:ValueError" for i in range(10)],
        "resolved": [],
        "unchanged": ["pytest:tests/old.py::t:KeyError"],
        "pre_existing": 1,
        "debug_task": True,
    }
    msg = build_verify_feedback([], summary, ["app/x.py"])
    assert "NEW failure" in msg
    assert "NOT your task" in msg
    assert len(msg) <= 1500
    # Only first 5 new failures listed.
    assert "t5" not in msg and "t4" in msg


def test_proof_with_attribution_sections():
    from app.verify.pipeline import build_proof

    class _T:
        issue_number = 3
        title = "mcp stdio"

    results = [
        _gate("suite", "FAIL", "boom", sigs=("pytest:tests/m.py::t:ExceptionGroup",)),
        _gate("lint", "PASS", "ok"),
    ]
    attribution = {
        "verdict": WITH_LIMITATIONS,
        "new": [],
        "resolved": [],
        "unchanged": ["pytest:tests/m.py::t:ExceptionGroup"],
        "pre_existing": 1,
    }
    proof = build_proof(_T(), results, "diff", "FAIL", "FAIL", attribution)
    assert proof.splitlines()[0] == "PROOF OF FIX"
    assert "New failures introduced: 0" in proof
    assert "Pre-existing failures (unchanged): 1" in proof
    assert "Status: VERIFIED_WITH_LIMITATIONS" in proof
    assert "READY FOR PR" not in proof


def test_select_targeted_tests_maps_stems(tmp_path):
    from app.verify.pipeline import select_targeted_tests

    (tmp_path / "tests" / "auth").mkdir(parents=True)
    (tmp_path / "tests" / "auth" / "test_middleware.py").write_text("x=1\n")
    (tmp_path / "tests" / "test_other.py").write_text("x=1\n")
    nodes = select_targeted_tests(tmp_path, ["app/auth/middleware.py"])
    assert nodes == ["tests/auth/test_middleware.py"]
    assert select_targeted_tests(tmp_path, ["README.md"]) == []
    assert select_targeted_tests(tmp_path, []) == []
    # A changed test file maps to itself.
    assert select_targeted_tests(tmp_path, ["tests/test_other.py"]) == [
        "tests/test_other.py"
    ]


def test_verdict_for_task_uses_baseline(tmp_path=None):
    """DB-level: BASELINE FAIL + AFTER FAIL identical → WITH_LIMITATIONS
    (not FAILED); gate lines carry the attribution."""
    import uuid

    from app.db import SessionLocal, init_db
    from app.models import Repository, Task, VerificationRun
    from app.verify.pipeline import record_gate, verdict_for_task

    _ = tmp_path
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"attr/verdict-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=0, title="attr", state="VERIFYING")
    db.add(task)
    db.commit()
    db.refresh(task)
    tid, rid = task.id, repo.id
    try:
        record_gate(
            db,
            task,
            "suite",
            "FAIL",
            True,
            "FAILED tests/a.py::t - ValueError",
            phase="BASELINE",
        )
        record_gate(
            db,
            task,
            "suite",
            "FAIL",
            True,
            "FAILED tests/a.py::t - ValueError",
            phase="AFTER",
        )
        verdict, run_ids, gate_lines = verdict_for_task(db, tid)
        assert verdict == WITH_LIMITATIONS, gate_lines
        assert any("BASELINE_FAILURE" in line for line in gate_lines)
        assert any("pre-existing unchanged" in line for line in gate_lines)
        assert len(run_ids) == 1
        # Stored rows got their attribution persisted for API readers.
        row = (
            db.query(VerificationRun)
            .filter_by(task_id=tid, check="suite", phase="AFTER")
            .first()
        )
        assert row.attribution == "BASELINE_FAILURE"
    finally:
        db.query(VerificationRun).filter_by(task_id=tid).delete(
            synchronize_session=False
        )
        from app.models import TaskEvent

        db.query(TaskEvent).filter_by(task_id=tid).delete(synchronize_session=False)
        db.query(Task).filter_by(id=tid).delete(synchronize_session=False)
        db.query(Repository).filter_by(id=rid).delete(synchronize_session=False)
        db.commit()
        db.close()
