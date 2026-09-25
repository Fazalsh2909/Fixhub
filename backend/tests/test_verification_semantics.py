"""P0-5 verification semantics + P0-6 real before/after regression.

SKIPPED is never PASS. Only VERIFIED counts as verified. The live test runs
a genuine FAIL→PASS regression inside Docker: a scripted agent fixes a
failing test, and the SAME suite command fails before and passes after.
"""

import os
import subprocess
from pathlib import Path

import pytest

from app.agent.orchestrator import engineer_issue
from app.db import SessionLocal, init_db
from app.llm.base import LLMProvider, LLMResponse
from app.models import Repository, Task, TaskEvent
from app.verify.pipeline import (
    BLOCKED,
    FAILED,
    GateResult,
    VERIFIED,
    WITH_LIMITATIONS,
    build_proof,
    is_verified,
    overall_status,
)


def G(check, status, required=True, output=""):
    return GateResult(check, status, required, output)


def test_overall_status_matrix():
    assert (
        overall_status(
            [G("suite", "PASS"), G("lint", "PASS"), G("type", "SKIPPED", False)]
        )
        == VERIFIED
    )
    assert (
        overall_status(
            [G("suite", "PASS"), G("lint", "PASS"), G("type", "PASS", False)]
        )
        == VERIFIED
    )
    # Optional gate actually failing only limits the verdict.
    assert (
        overall_status(
            [G("suite", "PASS"), G("lint", "PASS"), G("type", "FAIL", False)]
        )
        == WITH_LIMITATIONS
    )
    assert (
        overall_status(
            [G("suite", "PASS"), G("lint", "PASS"), G("type", "ERROR", False)]
        )
        == WITH_LIMITATIONS
    )
    # Any required gate not PASS fails the run.
    assert (
        overall_status(
            [G("suite", "FAIL"), G("lint", "PASS"), G("type", "SKIPPED", False)]
        )
        == FAILED
    )
    assert (
        overall_status(
            [G("suite", "SKIPPED"), G("lint", "PASS"), G("type", "SKIPPED", False)]
        )
        == FAILED
    )
    assert (
        overall_status(
            [G("suite", "ERROR"), G("lint", "PASS"), G("type", "SKIPPED", False)]
        )
        == FAILED
    )
    # Nothing could execute at all.
    assert (
        overall_status(
            [G("suite", "ERROR"), G("lint", "ERROR"), G("type", "NOT_RUN", False)]
        )
        == BLOCKED
    )
    assert overall_status([]) == BLOCKED
    # Only VERIFIED counts.
    assert (
        is_verified(
            [G("suite", "PASS"), G("lint", "PASS"), G("type", "SKIPPED", False)]
        )
        is True
    )
    assert (
        is_verified([G("suite", "PASS"), G("lint", "PASS"), G("type", "FAIL", False)])
        is False
    )


def test_empty_repo_is_not_verified(monkeypatch, tmp_path: Path):
    """No tests configured anywhere: suite SKIPPED (required) → FAILED."""
    import app.verify.pipeline as pipe

    (tmp_path / "notes.md").write_text("# nothing to run\n")
    monkeypatch.setattr(
        pipe, "run_in_sandbox", lambda wd, cmd, **k: {"ok": True, "output": "ok"}
    )
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"p05/empty-{os.getpid()}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=0, title="p05 empty", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id, repo_id = task.id, repo.id
    try:
        from app.verify.pipeline import run_verification

        results = run_verification(db, task, tmp_path)
        by_check = {r.check: r for r in results}
        assert by_check["suite"].status == "SKIPPED"
        assert by_check["suite"].required is True
        assert overall_status(results) == FAILED
        assert is_verified(results) is False
    finally:
        from app.models import VerificationRun

        db.query(VerificationRun).filter_by(task_id=task_id).delete()
        db.query(TaskEvent).filter_by(task_id=task_id).delete()
        db.query(Task).filter_by(id=task_id).delete()
        db.query(Repository).filter_by(id=repo_id).delete()
        db.commit()
        db.close()


def test_build_proof_renders_real_statuses():
    init_db()
    db = SessionLocal()
    task = Task(repo_id=0, issue_number=7, title="proof probe", state="VERIFYING")
    results = [
        G("suite", "PASS", True, "42 passed"),
        G("lint", "PASS", True, "ok"),
        G("type", "SKIPPED", False, "skipped — no type config in this repo"),
    ]
    proof = build_proof(
        task, results, "a.py\nb.py", "regression FAIL", "regression PASS"
    )
    assert "suite: PASS" in proof
    assert "type: SKIPPED (optional)" in proof
    assert "Status: VERIFIED — READY FOR PR" in proof
    assert "Before fix: regression FAIL" in proof
    assert "After fix: regression PASS" in proof
    assert proof.splitlines()[0] == "PROOF OF FIX"
    assert "\\n" not in proof  # real newlines, not escaped literals
    limited = build_proof(
        task,
        [
            G("suite", "PASS", True),
            G("lint", "PASS", True),
            G("type", "FAIL", False, "mypy err"),
        ],
        "a.py",
        "b",
        "a",
    )
    assert "Status: VERIFIED_WITH_LIMITATIONS" in limited
    assert "READY FOR PR" not in limited
    db.close()


def test_regression_gate_blocks_verified_on_failed_fix(monkeypatch, tmp_path: Path):
    """P0-6: regression is a REQUIRED gate row. Repro FAIL + suite still FAIL
    after the loop → regression FAIL → never VERIFIED.
    Attribution architecture: the identical before/after failure is a
    BASELINE_FAILURE (not a new task failure), so the aggregate becomes
    VERIFIED_WITH_LIMITATIONS instead of FAILED — still not verified, still
    not auto-debugged as if the agent broke something new."""
    import app.verify.pipeline as pipe
    from app.models import VerificationRun

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bug.py").write_text(
        "def test_bug():\n    assert 1 == 2\n"
    )

    def fake(wd, cmd, **k):
        return {"ok": False, "output": "1 failed", "sandbox": "docker"}

    monkeypatch.setattr(pipe, "run_in_sandbox", fake)
    import app.agent.orchestrator as orch

    monkeypatch.setattr(orch, "run_in_sandbox", fake)
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"p06/regfail-{os.getpid()}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=0, title="p06 reg fail", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id, repo_id = task.id, repo.id
    try:

        class NoopProvider(LLMProvider):
            def generate(self, messages, **kwargs):
                return LLMResponse(text="hi")

            def tool_call(self, messages, tools, **kwargs):
                return LLMResponse(text="nothing to do", tool_calls=[])

        out = engineer_issue(db, task, tmp_path, NoopProvider())
        assert out["verified"] is False
        assert out["overall"] == "VERIFIED_WITH_LIMITATIONS"
        assert out["attribution"]["pre_existing"] >= 1
        assert out["attribution"]["new"] == []
        db.refresh(task)
        assert task.state == "READY_FOR_APPROVAL"
        rows = {
            r.check: r
            for r in db.query(VerificationRun).filter_by(task_id=task_id).all()
        }
        assert "regression" in rows, sorted(rows)
        assert rows["regression"].status == "FAIL"
        assert rows["regression"].required is True
        assert (
            "BEFORE" in rows["regression"].output
            and "AFTER" in rows["regression"].output
        )
        by_check = {r.check: r.status for r in out["results"]}
        assert by_check["regression"] == "FAIL"
    finally:
        db.query(VerificationRun).filter_by(task_id=task_id).delete()
        db.query(TaskEvent).filter_by(task_id=task_id).delete()
        db.query(Task).filter_by(id=task_id).delete()
        db.query(Repository).filter_by(id=repo_id).delete()
        db.commit()
        db.close()


def test_regression_gate_skipped_when_not_reproduced(monkeypatch, tmp_path: Path):
    """P0-6 honesty: suite passes before any change → REPRODUCTION:
    NOT_REPRODUCED event + regression SKIPPED (required → overall FAILED,
    never a fake VERIFIED)."""
    import app.verify.pipeline as pipe
    from app.models import VerificationRun

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert 1 == 1\n"
    )

    def fake(wd, cmd, **k):
        return {"ok": True, "output": "1 passed", "sandbox": "docker"}

    monkeypatch.setattr(pipe, "run_in_sandbox", fake)
    import app.agent.orchestrator as orch

    monkeypatch.setattr(orch, "run_in_sandbox", fake)
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"p06/notrepro-{os.getpid()}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=0, title="p06 not repro", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id, repo_id = task.id, repo.id
    try:

        class NoopProvider(LLMProvider):
            def generate(self, messages, **kwargs):
                return LLMResponse(text="hi")

            def tool_call(self, messages, tools, **kwargs):
                return LLMResponse(text="nothing to do", tool_calls=[])

        out = engineer_issue(db, task, tmp_path, NoopProvider())
        rows = {
            r.check: r
            for r in db.query(VerificationRun).filter_by(task_id=task_id).all()
        }
        assert rows["regression"].status == "SKIPPED"
        assert "NOT_REPRODUCED" in rows["regression"].output
        stages = [e.stage for e in db.query(TaskEvent).filter_by(task_id=task_id).all()]
        assert "REPRODUCTION" in stages
        assert out["verified"] is False
    finally:
        db.query(VerificationRun).filter_by(task_id=task_id).delete()
        db.query(TaskEvent).filter_by(task_id=task_id).delete()
        db.query(Task).filter_by(id=task_id).delete()
        db.query(Repository).filter_by(id=repo_id).delete()
        db.commit()
        db.close()


class FixerProvider(LLMProvider):
    """Turn 1: fix the failing assert. Turn 2: stop. No other tools."""

    def __init__(self):
        self.n = 0

    def generate(self, messages, **kwargs):
        return LLMResponse(text="hi")

    def tool_call(self, messages, tools, **kwargs):
        self.n += 1
        if self.n == 1:
            return LLMResponse(
                text="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "arguments": (
                            '{"path": "tests/test_regression.py", '
                            '"old_string": "assert total(2, 3) == 6", '
                            '"new_string": "assert total(2, 3) == 5"}'
                        ),
                    }
                ],
            )
        return LLMResponse(text="fixed", tool_calls=[])


def _ensure_toolchain_image():
    image = "fixhub-sandbox-test"
    insp = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, timeout=60
    )
    if insp.returncode == 0:
        return image
    build = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            image,
            "-f",
            "infra/sandbox.Dockerfile",
            "infra/",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=1200,
    )
    if build.returncode != 0:
        pytest.skip(f"cannot build sandbox toolchain image: {build.stderr[-500:]}")
    return image


def test_live_regression_fail_to_pass_in_docker(tmp_path: Path, monkeypatch):
    """P0-6 crown jewel: SAME suite command FAILs before the edit (repro)
    and PASSes after (verify), all inside Docker. No mocks on execution."""
    image = _ensure_toolchain_image()
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "sandbox_image", image)
    ws = tmp_path / "buggy"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_regression.py").write_text(
        "from calc import total\n\n\ndef test_total():\n    assert total(2, 3) == 6\n"
    )
    (ws / "calc.py").write_text("def total(a, b):\n    return a + b\n")
    (ws / "fixhub.verify.json").write_text(
        '{"suite": "python -m pytest -q", "lint": null, "type": null}'
    )
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"p05/live-{os.getpid()}", local_path=str(ws))
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(
        repo_id=repo.id, issue_number=0, title="p05 live regression", state="CREATED"
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id, repo_id = task.id, repo.id
    try:
        out = engineer_issue(db, task, ws, FixerProvider())
        assert out["verified"] is True, out.get("results")
        assert out["overall"] == "VERIFIED"
        reg = out["regression"]
        assert "FAIL" in reg["before"], reg
        assert "PASS" in reg["after"], reg
        db.refresh(task)
        assert task.state == "READY_FOR_APPROVAL"
        events = {
            e.stage: e.message
            for e in db.query(TaskEvent).filter_by(task_id=task_id).all()
        }
        assert "REGRESSION" in events
        assert "FAIL" in events["REGRESSION"] and "PASS" in events["REGRESSION"]
        assert (ws / "tests" / "test_regression.py").read_text().count("== 5") == 1
    finally:
        from app.models import VerificationRun

        db.query(VerificationRun).filter_by(task_id=task_id).delete()
        db.query(TaskEvent).filter_by(task_id=task_id).delete()
        db.query(Task).filter_by(id=task_id).delete()
        db.query(Repository).filter_by(id=repo_id).delete()
        db.commit()
        db.close()
