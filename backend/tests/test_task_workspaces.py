"""P0-1 isolated workspaces: per-task/per-session dirs, no cross-talk,
deterministic retry baselines, refusal to delete outside the root."""

import subprocess
from pathlib import Path

import pytest

from app.automation import run_task_sync
from app.config import settings
from app.db import SessionLocal, init_db
from app.models import Repository, Task, TaskEvent


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    assert p.returncode == 0, f"git {' '.join(args)} failed: {p.stderr}"
    return p.stdout.strip()


@pytest.fixture()
def base_repo(tmp_path: Path) -> Path:
    base = tmp_path / "base"
    base.mkdir()
    (base / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "t@t.t")
    _git(base, "config", "user.name", "t")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "init")
    return base


def test_two_tasks_get_different_workspaces(base_repo: Path):
    from app.repo.workspaces import (
        base_head_sha,
        create_task_workspace,
        workspace_is_clean,
    )

    ws_a, sha_a = create_task_workspace(base_repo, 900001)
    ws_b, sha_b = create_task_workspace(base_repo, 900002)
    try:
        assert ws_a != ws_b
        assert ws_a.is_dir() and ws_b.is_dir()
        assert sha_a == sha_b == base_head_sha(base_repo) != ""
        assert workspace_is_clean(ws_a) and workspace_is_clean(ws_b)
        # Edit in A is invisible in B and in the base.
        (ws_a / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        assert (ws_b / "app.py").read_text() == "VALUE = 1\n"
        assert (base_repo / "app.py").read_text() == "VALUE = 1\n"
        assert not workspace_is_clean(ws_a)
        assert workspace_is_clean(ws_b)
    finally:
        from app.repo.workspaces import remove_workspace

        remove_workspace(ws_a)
        remove_workspace(ws_b)
    assert not ws_a.exists() and not ws_b.exists()


def test_recreate_from_dirty_gives_clean_baseline(base_repo: Path):
    from app.repo.workspaces import create_task_workspace, workspace_is_clean

    ws, _ = create_task_workspace(base_repo, 900003)
    try:
        (ws / "dirty.py").write_text("x = 1\n", encoding="utf-8")
        assert not workspace_is_clean(ws)
        # Retry semantics: recreate, never reuse unknown dirty state.
        ws2, _ = create_task_workspace(base_repo, 900003)
        assert ws2 == ws
        assert workspace_is_clean(ws2)
        assert not (ws2 / "dirty.py").exists()
    finally:
        from app.repo.workspaces import remove_workspace

        remove_workspace(ws)


def test_session_workspace_isolated_from_task(base_repo: Path):
    from app.repo.workspaces import (
        create_session_workspace,
        create_task_workspace,
        remove_workspace,
    )

    ws_t, _ = create_task_workspace(base_repo, 900004)
    ws_s, _ = create_session_workspace(base_repo, 700001)
    try:
        assert ws_t != ws_s
        (ws_s / "notes.md").write_text("session scratch\n", encoding="utf-8")
        assert not (ws_t / "notes.md").exists()
        assert not (base_repo / "notes.md").exists()
    finally:
        remove_workspace(ws_t)
        remove_workspace(ws_s)


def test_remove_refuses_paths_outside_root(tmp_path: Path):
    from app.repo.workspaces import remove_workspace

    outside = tmp_path / "precious.txt"
    outside.write_text("keep me\n", encoding="utf-8")
    assert remove_workspace(outside) is False
    assert outside.exists()


def test_git_diff_all_includes_untracked_files(base_repo: Path):
    from app.repo.workspaces import (
        create_task_workspace,
        git_diff_all,
        remove_workspace,
    )

    ws, _ = create_task_workspace(base_repo, 900005)
    try:
        (ws / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        (ws / "brand_new.py").write_text("NEW = True\n", encoding="utf-8")
        diff = git_diff_all(ws)
        assert "VALUE" in diff
        assert "brand_new.py" in diff
        assert "NEW = True" in diff
    finally:
        remove_workspace(ws)


def test_demo_trigger_uses_isolated_workspace(monkeypatch):
    """Demo mode removed: POST /api/demo/trigger returns 410 and creates
    no rows (previously it created demo/fastapi-jwt pollution)."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    init_db()
    client = TestClient(create_app())
    r = client.post("/api/demo/trigger", json={"issue": "p01 demo isolation probe"})
    assert r.status_code == 410, r.text
    assert "demo mode removed" in r.text


def test_run_task_sync_provisions_workspace(base_repo: Path, monkeypatch):
    """Wiring: the simple run path records workspace_path/base_sha + event."""
    import os

    init_db()
    db = SessionLocal()
    unique = f"p01/ws-probe-{os.getpid()}-{base_repo.name}"
    for stale in db.query(Repository).filter_by(full_name=unique).all():
        for t in db.query(Task).filter_by(repo_id=stale.id).all():
            db.query(TaskEvent).filter_by(task_id=t.id).delete()
            db.query(Task).filter_by(id=t.id).delete()
        db.query(Repository).filter_by(id=stale.id).delete()
    db.commit()
    repo = Repository(full_name=unique, local_path=str(base_repo))
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=0, title="p01 probe", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id
    monkeypatch.setattr(type(settings), "resolved_llm", lambda self: ("", "", ""))
    try:
        out = run_task_sync(task_id, force=True)
        assert out.get("run_id") == task_id
        assert out.get("state") == "BLOCKED"  # no key → fail-closed, workspace kept
        db.refresh(task)
        assert task.workspace_path, "workspace_path must be recorded"
        assert Path(task.workspace_path).is_dir()
        assert task.base_sha, "base_sha must be recorded"
        stages = [e.stage for e in db.query(TaskEvent).filter_by(task_id=task_id).all()]
        assert "WORKSPACE" in stages
    finally:
        from app.repo.workspaces import remove_workspace

        if task.workspace_path:
            remove_workspace(Path(task.workspace_path))
        db.query(TaskEvent).filter_by(task_id=task_id).delete()
        db.query(Task).filter_by(id=task_id).delete()
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()
