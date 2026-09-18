"""Per-repo verification: no hardcoded `mypy backend`, skips are explicit."""

from pathlib import Path

from app.db import SessionLocal, init_db
from app.models import Repository, Task
from app.verify.pipeline import detect_verification_config, run_verification


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
    assert cfg == {"suite": None, "lint": None, "type": None, "install": None}


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

    orig = pipe.run_in_sandbox
    pipe.run_in_sandbox = lambda wd, cmd, **k: {"ok": True, "output": "ok"}
    try:
        results = run_verification(db, task, tmp_path)
    finally:
        pipe.run_in_sandbox = orig
    as_dict = dict(results)
    # suite may be None (no tests/ dir) -> skipped PASS; type must be skipped PASS
    assert as_dict["type"] is True
    db.close()
