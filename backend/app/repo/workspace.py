"""Task workspace: isolated per-task clone. Agent tools operate ONLY here.

Phase 1 supports local path / git URL clones. GitHub App token clones are
added in the GitHub phase; token is embedded in the clone URL and never logged.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
import uuid

from app.config import settings


class WorkspaceError(RuntimeError):
    pass


def _on_rm_error(func, path: str, _exc_info) -> None:
    """Windows-robust removal: git packfiles are often read-only/locked."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    try:
        func(path)
    except OSError:
        pass


def _rmtree(path: str, *, retries: int = 3) -> bool:
    """Remove a tree, retrying transient Windows locks. Returns True if gone."""
    for attempt in range(retries):
        if not os.path.exists(path):
            return True
        shutil.rmtree(path, onerror=_on_rm_error)
        if not os.path.exists(path):
            return True
        time.sleep(0.5 * (attempt + 1))
    return not os.path.exists(path)


def workspace_path(task_id: int | str) -> str:
    root = os.path.abspath(settings.WORKSPACE_ROOT)
    return os.path.join(root, f"task-{task_id}")


def create_workspace(task_id: int | str) -> str:
    path = workspace_path(task_id)
    os.makedirs(path, exist_ok=True)
    return path


def destroy_workspace(task_id: int | str) -> None:
    path = workspace_path(task_id)
    if os.path.isdir(path):
        try:
            _rmtree(path)
        except Exception:
            pass


def clone_repo(source: str, task_id: int | str, branch: str = "") -> str:
    """Clone source (local path or URL) into a fresh task workspace. Returns path."""
    path = workspace_path(task_id)
    if os.path.isdir(path) or os.path.exists(path):
        if not _rmtree(path):
            raise WorkspaceError(f"workspace path already exists and could not be removed: {path}")
    os.makedirs(os.path.abspath(settings.WORKSPACE_ROOT), exist_ok=True)
    cmd = ["git", "clone", "--quiet", source, path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise WorkspaceError(f"clone failed: {proc.stderr[-500:]}")
    if branch:
        proc = subprocess.run(
            ["git", "checkout", branch], cwd=path, capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            raise WorkspaceError(f"checkout {branch} failed: {proc.stderr[-500:]}")
    return path


def checkout_ref(path: str, ref: str) -> bool:
    """Checkout a commit SHA (detached HEAD) or branch in an existing workspace.

    Used for CI tasks so the agent sees the exact failing commit instead of
    the default branch. Returns success; callers fall back on False.
    """
    if not ref or not os.path.isdir(path):
        return False
    proc = subprocess.run(
        ["git", "checkout", ref], cwd=path, capture_output=True, text=True, timeout=60
    )
    return proc.returncode == 0


def head_sha(path: str) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, timeout=30
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def run_id() -> str:
    return uuid.uuid4().hex[:8]
