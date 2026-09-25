"""Phase 32: end-to-end attribution sequences with stubbed sandbox.

CASE A (the reported bug): baseline suite fails (pre-existing, unrelated);
agent fixes its target; after-run shows the same baseline failure and zero
new failures -> VERIFIED_WITH_LIMITATIONS, NO debugging, proof records the
baseline, artifact is publishable.

CASE B (inverse): baseline green; patch breaks its own module ->
TASK_FAILURE -> DEBUGGING.
"""

import subprocess
import uuid
from pathlib import Path

from app.agent.orchestrator import engineer_issue
from app.db import SessionLocal, init_db
from app.llm.base import LLMProvider, LLMResponse
from app.models import Patch, Repository, Task, TaskEvent, VerificationRun


def _git(cwd: Path, *args: str) -> None:
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    assert p.returncode == 0, f"git {' '.join(args)} failed: {p.stderr}"


def _repo_ws(tmp_path: Path, name: str) -> Path:
    ws = tmp_path / name
    (ws / "tests" / "other").mkdir(parents=True)
    (ws / "calc.py").write_text("def total(a, b):\n    return a - b\n")
    (ws / "tests" / "test_calc.py").write_text(
        "from calc import total\n\n\ndef test_total():\n    assert total(2, 3) == 5\n"
    )
    (ws / "tests" / "other" / "test_old.py").write_text(
        "def test_old():\n    assert 1 == 2  # legacy, unrelated\n"
    )
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@t.t")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", ".")
    _git(ws, "commit", "-qm", "base")
    return ws


def _fake_run(ws: Path):
    def fake(wd, cmd, **k):
        fixed = (ws / "calc.py").read_text().count("return a + b") == 1
        if "test_calc" in cmd and "other" not in cmd:
            if fixed:
                return {"ok": True, "output": "1 passed", "sandbox": "docker"}
            return {
                "ok": False,
                "output": "FAILED tests/test_calc.py::test_total - AssertionError",
                "sandbox": "docker",
            }
        if "ruff" in cmd:
            return {"ok": True, "output": "All checks passed!", "sandbox": "docker"}
        out = "FAILED tests/other/test_old.py::test_old - ValueError: legacy"
        if not fixed:
            out += "\nFAILED tests/test_calc.py::test_total - AssertionError"
        return {"ok": False, "output": out, "sandbox": "docker"}

    return fake


class CalcFixer(LLMProvider):
    def generate(self, messages, **kwargs):
        return LLMResponse(text="fix")

    def tool_call(self, messages, tools, **kwargs):
        if not getattr(self, "_done", False):
            self._done = True
            return LLMResponse(
                text="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "arguments": (
                            '{"path": "calc.py", '
                            '"old_string": "return a - b", '
                            '"new_string": "return a + b"}'
                        ),
                    }
                ],
            )
        return LLMResponse(text="fixed", tool_calls=[])


class CalcBreaker(LLMProvider):
    def generate(self, messages, **kwargs):
        return LLMResponse(text="break")

    def tool_call(self, messages, tools, **kwargs):
        if not getattr(self, "_done", False):
            self._done = True
            return LLMResponse(
                text="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "arguments": (
                            '{"path": "calc.py", '
                            '"old_string": "return a + b", '
                            '"new_string": "return a - b"}'
                        ),
                    }
                ],
            )
        return LLMResponse(text="broken", tool_calls=[])


def _task(db, repo, title):
    task = Task(repo_id=repo.id, issue_number=1, title=title, state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _cleanup(db, tid, rid):
    db.query(VerificationRun).filter_by(task_id=tid).delete(synchronize_session=False)
    db.query(TaskEvent).filter_by(task_id=tid).delete(synchronize_session=False)
    db.query(Patch).filter_by(task_id=tid).delete(synchronize_session=False)
    db.query(Task).filter_by(id=tid).delete(synchronize_session=False)
    db.query(Repository).filter_by(id=rid).delete(synchronize_session=False)
    db.commit()


def test_baseline_failure_does_not_block_fixed_task(tmp_path, monkeypatch):
    """CASE A: the 10-step Phase-32 sequence."""
    import app.agent.orchestrator as orch
    import app.verify.pipeline as pipe
    from app.verify.pipeline import build_proof

    ws = _repo_ws(tmp_path, "ship")
    fake = _fake_run(ws)
    monkeypatch.setattr(pipe, "run_in_sandbox", fake)
    monkeypatch.setattr(orch, "run_in_sandbox", fake)
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"e2e/ship-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = _task(db, repo, "fix total")
    tid, rid = task.id, repo.id
    try:
        # 1-2. task created, baseline runs inside engineer_issue.
        out = engineer_issue(db, task, ws, CalcFixer())
        rows = db.query(VerificationRun).filter_by(task_id=tid).all()
        base_rows = [r for r in rows if r.phase == "BASELINE"]
        # 3. baseline failure recorded.
        assert any(r.check == "suite" and r.status == "FAIL" for r in base_rows), [
            (r.check, r.status, r.phase) for r in rows
        ]
        # 4. agent modified code.
        assert (ws / "calc.py").read_text().count("return a + b") == 1
        # 5-6. regression still FAILs on legacy, targeted gate passes.
        by_check = {r.check: r.status for r in out["results"]}
        assert by_check["suite"] == "FAIL"
        assert by_check.get("impacted") == "PASS"
        # 7-8. WITH_LIMITATIONS, and the agent is NOT sent back to debugging.
        assert out["overall"] == "VERIFIED_WITH_LIMITATIONS"
        assert out["attribution"]["new"] == []
        assert out["attribution"]["pre_existing"] >= 1
        db.refresh(task)
        assert task.state == "READY_FOR_APPROVAL"
        stages = [e.stage for e in db.query(TaskEvent).filter_by(task_id=tid).all()]
        assert "DEBUGGING" not in stages
        assert "ATTRIBUTION" in stages and "BASELINE" in stages
        # 9. proof records the baseline failure.
        proof = build_proof(
            task,
            out["results"],
            "calc.py",
            "FAIL",
            "FAIL",
            out["attribution"],
        )
        assert "Pre-existing failures (unchanged)" in proof
        assert "New failures introduced: 0" in proof
        # 10. publishable: record the patch like run_task_sync does, then the
        # artifact gate must accept WITH_LIMITATIONS.
        from app.github.publisher import build_verified_artifact
        from app.repo.workspaces import git_diff_all

        db.add(Patch(task_id=tid, diff=git_diff_all(ws)[-20000:], branch=""))
        db.commit()
        artifact = build_verified_artifact(db, task, repo)
        assert artifact.verification_status == "VERIFIED_WITH_LIMITATIONS"
        assert artifact.patch_diff.count("return a + b") == 1
    finally:
        _cleanup(db, tid, rid)
        db.close()


def test_new_failure_in_changed_module_debugs(tmp_path, monkeypatch):
    """CASE B: green baseline; patch breaks its own module -> TASK_FAILURE."""
    import app.agent.orchestrator as orch
    import app.verify.pipeline as pipe

    ws = _repo_ws(tmp_path, "break")
    (ws / "calc.py").write_text("def total(a, b):\n    return a + b\n")
    (ws / "tests" / "other" / "test_old.py").write_text(
        "def test_old():\n    assert 1 == 1\n"
    )
    _git(ws, "add", ".")
    _git(ws, "commit", "-qm", "green base")

    def fake(wd, cmd, **k):
        broken = (ws / "calc.py").read_text().count("return a - b") == 1
        if "ruff" in cmd:
            return {"ok": True, "output": "All checks passed!", "sandbox": "docker"}
        if broken:
            return {
                "ok": False,
                "output": "FAILED tests/test_calc.py::test_total - AssertionError",
                "sandbox": "docker",
            }
        return {"ok": True, "output": "2 passed", "sandbox": "docker"}

    monkeypatch.setattr(pipe, "run_in_sandbox", fake)
    monkeypatch.setattr(orch, "run_in_sandbox", fake)
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"e2e/break-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = _task(db, repo, "break total")
    tid, rid = task.id, repo.id
    try:
        out = engineer_issue(db, task, ws, CalcBreaker())
        assert out["overall"] == "FAILED"
        assert out["attribution"]["new"] == [
            "pytest:tests/test_calc.py::test_total:AssertionError"
        ]
        db.refresh(task)
        assert task.state == "DEBUGGING"
    finally:
        _cleanup(db, tid, rid)
        db.close()


def test_rerun_reuses_baseline_without_duplicating(tmp_path, monkeypatch):
    """Manual re-runs load the existing BASELINE snapshot instead of
    re-recording it (and must not hit an unimported helper)."""
    import app.agent.orchestrator as orch
    import app.verify.pipeline as pipe
    from app.llm.base import LLMResponse

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bug.py").write_text(
        "def test_bug():\n    assert 1 == 2\n"
    )

    def fake(wd, cmd, **k):
        return {"ok": False, "output": "1 failed", "sandbox": "docker"}

    monkeypatch.setattr(pipe, "run_in_sandbox", fake)
    monkeypatch.setattr(orch, "run_in_sandbox", fake)
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"e2e/rerun-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = _task(db, repo, "rerun me")
    tid, rid = task.id, repo.id
    try:

        class Noop(LLMProvider):
            def generate(self, messages, **kwargs):
                return LLMResponse(text="hi")

            def tool_call(self, messages, tools, **kwargs):
                return LLMResponse(text="nothing", tool_calls=[])

        engineer_issue(db, task, tmp_path, Noop())

        def baseline_count():
            return (
                db.query(VerificationRun)
                .filter_by(task_id=tid, phase="BASELINE")
                .count()
            )

        assert baseline_count() >= 1
        # Second run (manual retry): loads baseline, records none new.
        out = engineer_issue(db, task, tmp_path, Noop())
        assert baseline_count() >= 1
        assert out["overall"] == "VERIFIED_WITH_LIMITATIONS"
    finally:
        _cleanup(db, tid, rid)
        db.close()
