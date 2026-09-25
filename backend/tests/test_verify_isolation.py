"""P0-4 fail-closed verification: repo-defined commands NEVER execute on the
API host. Without an isolated executor every gate fails loudly with the
isolation-unavailable message."""

import os
import subprocess
from pathlib import Path

from app.db import SessionLocal, init_db
from app.models import Repository, Task, TaskEvent, VerificationRun
from app.sandbox import docker_runner
from app.tools.registry import run_command
from app.verify.pipeline import (
    BLOCKED,
    ERROR,
    ISOLATION_UNAVAILABLE,
    _run_gate,
    overall_status,
    run_verification,
)


def _no_docker(monkeypatch):
    monkeypatch.setattr(docker_runner, "docker_available", lambda: False)


def _host_guard(monkeypatch):
    """Explode if anything tries host subprocess execution."""

    def _boom(*a, **k):
        raise AssertionError("host execution attempted — must fail closed")

    monkeypatch.setattr(subprocess, "run", _boom)


def _repo_with_suite(tmp_path: Path) -> Path:
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return ws


def test_gate_fails_closed_without_isolation(monkeypatch, tmp_path: Path):
    _no_docker(monkeypatch)
    _host_guard(monkeypatch)
    gate = _run_gate(tmp_path, "python -m pytest -q", "suite", True)
    assert gate.passed is False
    assert gate.status == ERROR
    assert gate.output == ISOLATION_UNAVAILABLE


def test_run_verification_fails_all_gates_without_isolation(
    monkeypatch, tmp_path: Path
):
    _no_docker(monkeypatch)
    _host_guard(monkeypatch)
    ws = _repo_with_suite(tmp_path)
    init_db()
    db = SessionLocal()
    unique = f"p04/iso-{os.getpid()}-{tmp_path.name[-6:]}"
    repo = Repository(full_name=unique, local_path=str(ws))
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=0, title="p04 probe", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id
    try:
        results = run_verification(db, task, ws)
        assert len(results) == 3
        for gate in results:
            assert gate.passed is False, f"{gate.check} must fail closed"
            assert gate.status == ERROR
        # Nothing could execute at all: BLOCKED, not FAILED.
        assert overall_status(results) == BLOCKED
        rows = db.query(VerificationRun).filter_by(task_id=task_id).all()
        assert len(rows) == 3
        assert all(r.output == ISOLATION_UNAVAILABLE for r in rows)
        assert all(r.passed is False for r in rows)
        assert all(r.status == ERROR for r in rows)
    finally:
        db.query(VerificationRun).filter_by(task_id=task_id).delete()
        db.query(TaskEvent).filter_by(task_id=task_id).delete()
        db.query(Task).filter_by(id=task_id).delete()
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()


def test_agent_command_requires_isolation(monkeypatch, tmp_path: Path):
    _no_docker(monkeypatch)
    _host_guard(monkeypatch)
    out = run_command(tmp_path, "pytest -q")
    assert out["ok"] is False
    assert out.get("sandbox") == "unavailable"


def test_isolation_message_contract():
    assert ISOLATION_UNAVAILABLE == (
        "Verification unavailable: isolated execution environment required. "
        "Start Docker Desktop and press Run again — zero tokens were burned."
    )
