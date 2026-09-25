"""Simplified 4-check verification: verdicts + honest proof. Docker-free."""

import uuid

from app.verify.pipeline import GateResult as G
from app.verify.simple import (
    BLOCKED,
    FAILED,
    VERIFIED,
    WITH_LIMITATIONS,
    build_simple_proof,
    decide_verdict,
    persist_simple_attribution,
    verdict_summary,
)


def _g(check, status, output="", required=True):
    return G(check, status, required, output)


def test_all_pass_is_verified():
    before = [_g("regression", "PASS", "1 passed"), _g("lint", "PASS", "ok")]
    after = [_g("regression", "PASS", "1 passed"), _g("lint", "PASS", "ok")]
    v = decide_verdict(before, after, ["calc.py"])
    assert v.verdict == VERIFIED
    assert v.task_state == "READY_FOR_APPROVAL"
    assert v.debug is False
    assert v.publishable is True and v.verified is True


def test_task_failure_debugs_only_on_new_breakage():
    before = [_g("regression", "PASS", "2 passed")]
    after = [
        _g(
            "regression",
            "FAIL",
            "FAILED tests/test_total.py::test_total - assert 6 == 5",
        )
    ]
    v = decide_verdict(before, after, ["calc.py"])
    assert v.verdict == FAILED
    assert v.task_state == "DEBUGGING"
    assert v.debug is True
    assert len(v.new_failure_lines) == 1


def test_baseline_failure_is_limitations_not_debug():
    out = "FAILED tests/a.py::t - ValueError: old"
    before = [_g("regression", "FAIL", out)]
    after = [_g("regression", "FAIL", out)]
    v = decide_verdict(before, after, ["other.py"])
    assert v.verdict == WITH_LIMITATIONS
    assert v.task_state == "READY_FOR_APPROVAL"
    assert v.debug is False
    assert v.publishable is True and v.verified is False
    assert v.pre_existing == 1


def test_unknown_blocks_for_triage():
    before = [_g("regression", "FAIL", "weird !!!")]
    after = [_g("regression", "FAIL", "different weird ???")]
    v = decide_verdict(before, after, [])
    assert v.verdict == BLOCKED
    assert v.task_state == "BLOCKED"
    assert v.debug is False


def test_infrastructure_blocks():
    before = [_g("regression", "FAIL", "1 failed")]
    after = [
        _g(
            "regression",
            "ERROR",
            "Verification unavailable: isolated execution environment required.",
        )
    ]
    v = decide_verdict(before, after, ["calc.py"])
    assert v.verdict == BLOCKED
    assert v.debug is False


def test_environment_fails_without_debug():
    before = [_g("regression", "FAIL", "No module named 'asyncpg'")]
    after = [_g("regression", "FAIL", "No module named 'asyncpg'")]
    v = decide_verdict(before, after, ["calc.py"])
    assert v.verdict == FAILED
    assert v.task_state == "FAILED"
    assert v.debug is False


def test_unconfigured_required_suite_fails_honestly():
    before = [
        _g("regression", "SKIPPED", "skipped — no regression config in this repo")
    ]
    after = [_g("regression", "SKIPPED", "skipped — no regression config in this repo")]
    v = decide_verdict(before, after, [])
    assert v.verdict == FAILED
    assert v.debug is False


def test_optional_type_failure_only_limits():
    before = [
        _g("regression", "PASS", "1 passed"),
        _g("type", "PASS", "ok", required=False),
    ]
    after = [
        _g("regression", "PASS", "1 passed"),
        _g("type", "FAIL", "x.py:1: error: bad", required=False),
    ]
    v = decide_verdict(before, after, ["calc.py"])
    assert v.verdict == WITH_LIMITATIONS
    assert v.debug is False


def test_proof_is_honest_never_fake_green():
    before = [
        _g("regression", "FAIL", "assert 6 == 5"),
        _g("targeted", "SKIPPED", "skipped — no targeted config", required=False),
        _g("lint", "SKIPPED", "skipped — no lint config", required=False),
        _g("type", "SKIPPED", "skipped — no type config", required=False),
    ]
    after = [
        _g("regression", "PASS", "1 passed"),
        _g("targeted", "SKIPPED", "skipped — no targeted config", required=False),
        _g("lint", "SKIPPED", "skipped — no lint config", required=False),
        _g("type", "SKIPPED", "skipped — no type config", required=False),
    ]
    v = decide_verdict(before, after, ["calc.py"])
    proof = build_simple_proof(
        issue_ref="Issue: #1 total() off by one",
        base_sha="abc123",
        before=before,
        after=after,
        verdict=v,
        changed_files=["calc.py"],
        diff_head="--- a/calc.py\n+++ b/calc.py",
    )
    assert proof.splitlines()[0] == "PROOF OF FIX"
    assert "FAIL → PASS" in proof
    assert "New failures: 0" in proof
    assert "Files changed:" in proof and "calc.py" in proof
    assert f"Result: {v.verdict}" in proof
    assert "8/8" not in proof


def test_persist_attribution_writes_why_to_rows():
    """Live task 239: AFTER rows kept attribution=NONE so the UI could only
    say FAILED with no reason. Classifications must be persisted."""
    from app.db import SessionLocal, init_db
    from app.models import Repository, Task, TaskEvent, VerificationRun
    from app.verify.pipeline import PHASE_AFTER, record_gate

    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"attr/persist-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=0, title="persist", state="VERIFYING")
    db.add(task)
    db.commit()
    db.refresh(task)
    tid, rid = task.id, repo.id
    try:
        out = "No module named 'mcp'"
        record_gate(db, task, "regression", "FAIL", True, out, phase=PHASE_AFTER)
        before = [_g("regression", "FAIL", out)]
        after = [_g("regression", "FAIL", out)]
        v = decide_verdict(before, after, ["calc.py"])
        assert v.verdict == FAILED  # env marker wins: suite never ran
        persist_simple_attribution(db, tid, v)
        row = (
            db.query(VerificationRun)
            .filter_by(task_id=tid, check="regression", phase=PHASE_AFTER)
            .first()
        )
        assert row.attribution == "ENVIRONMENT_FAILURE", row.attribution
        summary = verdict_summary(v)
        assert "regression=ENVIRONMENT_FAILURE" in summary
    finally:
        db.query(VerificationRun).filter_by(task_id=tid).delete(
            synchronize_session=False
        )
        db.query(TaskEvent).filter_by(task_id=tid).delete(synchronize_session=False)
        db.query(Task).filter_by(id=tid).delete(synchronize_session=False)
        db.query(Repository).filter_by(id=rid).delete(synchronize_session=False)
        db.commit()
        db.close()
