"""End-to-end on the deterministic demo repo (Phase 22/29).

Fake-agent path (always runs): proves Task -> workspace -> agent edit ->
regression FAIL->PASS -> proof -> READY_FOR_APPROVAL with live Docker
verification. No LLM involved.

Live-engine path (opt-in): proves mini-SWE-agent itself fixes the demo.
Needs FIXHUB_LIVE_AGENT=1, the package, Docker, and a model API key.
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.agent.miniswe_adapter import AgentStep, MiniSweResult
from app.db import SessionLocal, init_db
from app.models import Patch, Repository, Task, TaskEvent, VerificationRun

DEMO = Path(__file__).resolve().parents[2] / "demo" / "fixhub-demo-python"


def _init_demo_repo(dst: Path) -> None:
    shutil.copytree(DEMO, dst, dirs_exist_ok=True)
    env = dict(os.environ)
    subprocess.run(["git", "init"], cwd=dst, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        cwd=dst,
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "bug"],
        cwd=dst,
        check=True,
        capture_output=True,
        env=env,
    )


def _rows(db, repo_name):
    repo = Repository(full_name=repo_name, local_path="")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(
        repo_id=repo.id, issue_number=1, title="total() off by one", state="CREATED"
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return repo, task


def _cleanup(db, task_id, repo_id, volume=None):
    db.query(VerificationRun).filter_by(task_id=task_id).delete(
        synchronize_session=False
    )
    db.query(TaskEvent).filter_by(task_id=task_id).delete(synchronize_session=False)
    db.query(Patch).filter_by(task_id=task_id).delete(synchronize_session=False)
    db.query(Task).filter_by(id=task_id).delete(synchronize_session=False)
    db.query(Repository).filter_by(id=repo_id).delete(synchronize_session=False)
    db.commit()
    if volume:
        try:
            from app.sandbox.docker_runner import remove_deps_volume

            remove_deps_volume(volume)
        except Exception:
            pass


def _fake_run_fix(workdir: Path, prompt: str, **kwargs) -> MiniSweResult:
    """Scripted stand-in for the coding agent: applies the real one-line fix."""
    assert "total" in prompt
    calc = workdir / "calc.py"
    text = calc.read_text(encoding="utf-8")
    assert "a + b + 1" in text, "demo must start buggy"
    calc.write_text(text.replace("return a + b + 1", "return a + b"), encoding="utf-8")
    from app.repo.workspaces import git_diff_all

    diff = git_diff_all(workdir)
    return MiniSweResult(
        exit_status="Submitted",
        submission="fixed off-by-one in total()",
        n_calls=4,
        steps=[
            AgentStep(step=1, command="cat calc.py", returncode=0),
            AgentStep(
                step=2,
                command="sed -i s/a+b+1/a+b/ calc.py",
                returncode=0,
            ),
        ],
        changed_files=["calc.py"],
        diff=diff,
    )


def test_e2e_demo_fail_to_pass_with_proof(tmp_path, monkeypatch):
    """Issue -> agent fix -> regression FAIL->PASS -> proof -> READY_FOR_APPROVAL."""
    import app.agent.runner as runner

    ws = tmp_path / "demo"
    _init_demo_repo(ws)
    monkeypatch.setattr(
        runner, "model_spec_from_settings", lambda: ("openai/test", {}, True)
    )
    monkeypatch.setattr(runner, "run_fix", _fake_run_fix)
    init_db()
    db = SessionLocal()
    name = f"e2e/demo-{uuid.uuid4().hex[:8]}"
    repo, task = _rows(db, name)
    tid, rid = task.id, repo.id
    try:
        out = runner.run_miniswe_task(db, task, ws, repo)
        assert out["overall"] == "VERIFIED", out
        assert out["verified"] is True and out["publishable"] is True
        db.refresh(task)
        assert task.state == "READY_FOR_APPROVAL"
        assert "FAIL → PASS" in out["proof"], out["proof"]
        assert "Result: VERIFIED" in out["proof"]
        assert out["changed_files"] == ["calc.py"]
        # No chain-of-thought persisted in events.
        blob = " ".join(
            e.message for e in db.query(TaskEvent).filter_by(task_id=tid).all()
        )
        assert "PRIVATE" not in blob and "reasoning" not in blob.lower()
        rows = {
            (r.check, r.phase): r.status
            for r in db.query(VerificationRun).filter_by(task_id=tid).all()
        }
        assert rows[("regression", "BASELINE")] == "FAIL"
        assert rows[("regression", "AFTER")] == "PASS"
        patch = db.query(Patch).filter_by(task_id=tid).first()
        assert patch is not None and "a + b" in patch.diff
    finally:
        from app.sandbox.docker_runner import deps_volume_for_task

        _cleanup(db, tid, rid, deps_volume_for_task(tid))
        db.close()


def test_runner_fails_loudly_when_agent_changes_nothing(tmp_path, monkeypatch):
    """No diff = nothing to verify. Must not run verification theater or
    land in REVIEWING with an empty patch (the publisher refuses those)."""
    import app.agent.runner as runner

    monkeypatch.setattr(
        runner, "model_spec_from_settings", lambda: ("openai/test", {}, True)
    )
    monkeypatch.setattr(runner, "run_four_checks", lambda *a, **k: [])
    monkeypatch.setattr(
        runner,
        "run_fix",
        lambda *a, **k: MiniSweResult(exit_status="Submitted", submission="done"),
    )
    init_db()
    db = SessionLocal()
    name = f"e2e/nochange-{uuid.uuid4().hex[:8]}"
    repo, task = _rows(db, name)
    tid, rid = task.id, repo.id
    try:
        out = runner.run_miniswe_task(db, task, tmp_path, repo)
        assert out["verified"] is False
        assert out["retryable"] is False
        assert "no changes" in out["error"]
        db.refresh(task)
        assert task.state == "FAILED"
    finally:
        _cleanup(db, tid, rid)
        db.close()


@pytest.mark.skipif(os.getenv("FIXHUB_LIVE_AGENT") != "1", reason="opt-in live LLM run")
def test_live_miniswe_engine_fixes_demo(tmp_path):
    """Phase 23: the real mini-SWE-agent fixes the demo repo (no mocks)."""
    from app.agent.miniswe_adapter import (
        build_task_prompt,
        miniswe_available,
        model_spec_from_settings,
        run_fix,
    )
    from app.config import settings

    if not miniswe_available():
        pytest.skip("mini-swe-agent not installed")
    _, _, has_key = model_spec_from_settings()
    if not has_key:
        pytest.skip("no model API key configured")
    ws = tmp_path / "livedemo"
    _init_demo_repo(ws)
    model_name, model_kwargs, _ = model_spec_from_settings()
    prompt = build_task_prompt(
        issue_title="#1 total() off by one",
        issue_body=(DEMO / "ISSUE.md").read_text(encoding="utf-8"),
        repo_name="demo/fixhub-demo-python",
    )
    res = run_fix(
        ws,
        prompt,
        model_name=model_name,
        model_kwargs=model_kwargs,
        image=settings.sandbox_image,
        step_limit=15,
        wall_time_s=600,
    )
    assert res.exit_status == "Submitted", res.error or res.exit_status
    fixed = (ws / "calc.py").read_text(encoding="utf-8")
    assert "return a + b\n" in fixed and "return a + b + 1" not in fixed
    # Independent check: the regression suite really passes now.
    p = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{ws}:/work",
            "-w",
            "/work",
            settings.sandbox_image,
            "python",
            "-m",
            "pytest",
            "tests/test_total.py",
            "-q",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert p.returncode == 0, (p.stdout + p.stderr)[-2000:]
