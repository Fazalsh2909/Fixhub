"""Per-repo verification: no hardcoded `mypy backend`, skips are explicit."""

from pathlib import Path

from app.db import SessionLocal, init_db
from app.models import Repository, Task
from app.verify.pipeline import (
    detect_verification_config,
    overall_status,
    run_verification,
)


def test_demo_repo_skips_type_gate(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1==1\n")
    cfg = detect_verification_config(tmp_path)
    assert cfg["suite"] == "python -m pytest -q"
    assert cfg["type"] is None, "demo without mypy config must skip type, not fail"


def test_override_file_respected(tmp_path: Path):
    (tmp_path / "fixhub.verify.json").write_text(
        '{"suite": null, "lint": null, "type": null}'
    )
    cfg = detect_verification_config(tmp_path)
    assert cfg == {
        "suite": None,
        "lint": None,
        "type": None,
        "install": None,
        "regression": None,
        "targeted": None,
        "project_root": "",
    }


def test_monorepo_finds_nested_suite(tmp_path: Path):
    """Root with no markers + apps/api with requirements+tests → the suite
    resolves to the nested project root (task-71 class of miss)."""
    (tmp_path / "README.md").write_text("# monorepo\n")
    api = tmp_path / "apps" / "api"
    (api / "tests").mkdir(parents=True)
    (api / "requirements.txt").write_text("pytest\n")
    (api / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n")
    cfg = detect_verification_config(tmp_path)
    assert cfg["suite"] == "python -m pytest -q"
    assert cfg["project_root"] == "apps/api"


def test_monorepo_gates_run_in_nested_root(tmp_path: Path):
    """run_verification executes install/suite/lint with cwd set to the
    nested project dir — captured, not assumed."""
    import app.verify.pipeline as pipe

    api = tmp_path / "apps" / "api"
    (api / "tests").mkdir(parents=True)
    (api / "requirements.txt").write_text("pytest\n")
    (api / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n")
    seen: list = []

    def fake(wd, cmd, **k):
        seen.append((str(wd), cmd))
        return {"ok": True, "output": "ok", "sandbox": "docker"}

    orig = pipe.run_in_sandbox
    pipe.run_in_sandbox = fake
    try:
        init_db()
        db = SessionLocal()
        repo = Repository(full_name="demo/monorepo-probe")
        db.add(repo)
        db.commit()
        db.refresh(repo)
        task = Task(repo_id=repo.id, issue_number=0, title="mono", state="CREATED")
        db.add(task)
        db.commit()
        db.refresh(task)
        try:
            results = run_verification(db, task, tmp_path)
        finally:
            from app.models import VerificationRun

            db.query(VerificationRun).filter_by(task_id=task.id).delete()
            db.query(Task).filter_by(id=task.id).delete()
            db.query(Repository).filter_by(id=repo.id).delete()
            db.commit()
            db.close()
    finally:
        pipe.run_in_sandbox = orig
    assert seen, "gates must execute"
    cwds = {wd for wd, _ in seen}
    assert len(cwds) == 1
    (only,) = cwds
    assert Path(only).name == "api"
    assert (Path(only) / "requirements.txt").is_file()
    assert overall_status(results) == "VERIFIED"


def test_gates_share_per_task_deps_volume(tmp_path: Path):
    """Ephemeral containers lose site-packages installs: install targets
    /deps and every gate mounts the same per-task volume."""
    import app.verify.pipeline as pipe
    from app.sandbox.docker_runner import _volume_for_workdir, deps_volume_for_task

    assert deps_volume_for_task(71) == "fixhub-deps-task-71"
    fake_root = tmp_path / "ws"
    assert _volume_for_workdir(fake_root / "tasks" / "task-71") == "fixhub-deps-task-71"
    assert (
        _volume_for_workdir(fake_root / "sessions" / "session-9")
        == "fixhub-deps-session-9"
    )
    assert _volume_for_workdir(fake_root / "Fazalsh2909__nexus") is None
    api = tmp_path / "apps" / "api"
    (api / "tests").mkdir(parents=True)
    (api / "requirements.txt").write_text("pytest\n")
    (api / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n")
    seen: list = []

    def fake(wd, cmd, **k):
        seen.append((str(wd), cmd, k.get("deps_volume")))
        return {"ok": True, "output": "ok", "sandbox": "docker"}

    orig = pipe.run_in_sandbox
    pipe.run_in_sandbox = fake
    try:
        init_db()
        db = SessionLocal()
        repo = Repository(full_name="demo/depsvol-probe")
        db.add(repo)
        db.commit()
        db.refresh(repo)
        task = Task(repo_id=repo.id, issue_number=0, title="deps", state="CREATED")
        db.add(task)
        db.commit()
        db.refresh(task)
        try:
            run_verification(db, task, tmp_path)
        finally:
            from app.models import VerificationRun

            db.query(VerificationRun).filter_by(task_id=task.id).delete()
            db.query(Task).filter_by(id=task.id).delete()
            db.query(Repository).filter_by(id=repo.id).delete()
            db.commit()
            db.close()
    finally:
        pipe.run_in_sandbox = orig
    install_calls = [s for s in seen if s[1].startswith("pip install")]
    assert install_calls, "install gate must run"
    assert "--target /deps" in install_calls[0][1]
    vols = {s[2] for s in seen}
    assert vols == {f"fixhub-deps-task-{task.id}"}, vols


def test_ensure_deps_never_raises(tmp_path: Path):
    import app.verify.pipeline as pipe
    from app.verify.pipeline import ensure_deps

    orig = pipe.run_in_sandbox

    def boom(*a, **k):
        raise RuntimeError("docker down")

    pipe.run_in_sandbox = boom
    try:
        out = ensure_deps(tmp_path, "", "pip install -q -r requirements.txt", 1)
        assert out["ok"] is False
        out = ensure_deps(tmp_path, "", None, 1)
        assert out["ok"] is True
    finally:
        pipe.run_in_sandbox = orig


def test_run_verification_records_skips(tmp_path: Path):
    init_db()
    db = SessionLocal()
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1==1\n")
    repo = db.query(Repository).filter_by(full_name="demo/verify-skip").first()
    if repo is None:
        repo = Repository(full_name="demo/verify-skip")
        db.add(repo)
        db.commit()
        db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=1, title="v", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    import app.verify.pipeline as pipe
    from app.verify.pipeline import SKIPPED, is_verified

    orig = pipe.run_in_sandbox
    pipe.run_in_sandbox = lambda wd, cmd, **k: {"ok": True, "output": "ok"}
    try:
        results = run_verification(db, task, tmp_path)
    finally:
        pipe.run_in_sandbox = orig
    by_check = {r.check: r for r in results}
    # suite may be None (no tests/ dir) -> SKIPPED (never PASS-by-skip);
    # type must be SKIPPED (optional) — and the run must NOT be verified,
    # because no meaningful verification occurred.
    assert by_check["type"].status == SKIPPED
    assert by_check["type"].required is False
    assert by_check["type"].passed is False
    assert overall_status(results) == "FAILED"
    assert is_verified(results) is False
    db.close()
