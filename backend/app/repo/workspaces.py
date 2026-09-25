"""Per-task / per-session isolated workspaces (P0-1).

Every autonomous task and every file-modifying interactive session gets its
own working directory. Preferred mechanism is ``git worktree`` (cheap,
shares objects, pinned to an exact base commit)::

    workspaces/
        <owner>__<repo>/        shared base clone (never written by agents)
        tasks/task-<id>/        detached worktree at base_sha (or snapshot copy)
        sessions/session-<id>/  same, provisioned lazily on first tool turn

Rules:
- Agents NEVER write to the shared base. All tool/sandbox/verify paths that
  execute agent work must receive a task/session workspace.
- A retry starts from a clean, known baseline: the caller removes and
  recreates the workspace (recorded as a task event), never reuses unknown
  dirty state.
- Detached worktrees carry no branch; the publisher (P1-7) creates the real
  branch at publish time. Retries therefore cannot leak branch state.
- Removal asserts the path lives under WORKSPACE_ROOT before deleting.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = BACKEND_DIR / "workspaces"
TASKS_DIR = WORKSPACE_ROOT / "tasks"
SESSIONS_DIR = WORKSPACE_ROOT / "sessions"

_GIT_TIMEOUT = 120
_MAX_DIFF_CHARS = 60000
_MAX_UNTRACKED_FILES = 200
_MAX_UNTRACKED_BYTES = 1_000_000


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=_GIT_TIMEOUT
    )


def task_workspace_dir(task_id: int) -> Path:
    return TASKS_DIR / f"task-{int(task_id)}"


def session_workspace_dir(session_id: int) -> Path:
    return SESSIONS_DIR / f"session-{int(session_id)}"


def is_git_repo(path: Path) -> bool:
    return (path / ".git").is_dir()


def base_head_sha(base: Path) -> str:
    """HEAD sha of the base repo, or '' when unavailable."""
    try:
        p = _run(["git", "rev-parse", "HEAD"], cwd=base)
    except Exception:
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def create_task_workspace(
    base: Path, task_id: int, base_sha: str = ""
) -> tuple[Path, str]:
    """Create a clean isolated workspace for a task. Deterministic: an
    existing directory at the target is removed first (clean baseline).

    Returns (workspace_path, recorded_base_sha). Uses a detached worktree
    when the base is a git repo, else a filesystem snapshot copy.
    """
    target = task_workspace_dir(task_id)
    return _create(target, base, base_sha)


def create_session_workspace(base: Path, session_id: int) -> tuple[Path, str]:
    target = session_workspace_dir(session_id)
    return _create(target, base, "")


def _create(target: Path, base: Path, base_sha: str) -> tuple[Path, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _remove(target, base if is_git_repo(base) else None)
    base = base.resolve()
    if is_git_repo(base):
        rev = base_sha.strip() or "HEAD"
        p = _run(["git", "worktree", "add", "--detach", str(target), rev], cwd=base)
        if p.returncode == 0 and target.is_dir():
            return target, base_head_sha(target)
        # Worktree failed (e.g. bare/odd repo): fall through to snapshot.
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(base, target, ignore=shutil.ignore_patterns(".git"))
    return target, ""


def remove_workspace(path: Path) -> bool:
    """Remove a task/session workspace. Returns True when gone. Refuses to
    touch anything outside WORKSPACE_ROOT."""
    try:
        resolved = path.resolve()
        resolved.relative_to(WORKSPACE_ROOT.resolve())
    except (ValueError, OSError):
        return False
    base = _worktree_base(resolved)
    _remove(resolved, base)
    return not resolved.exists()


def _worktree_base(path: Path) -> Path | None:
    """Find the base repo of a worktree via `git worktree list --porcelain`."""
    try:
        p = _run(["git", "worktree", "list", "--porcelain"], cwd=path)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    first = ""
    for line in p.stdout.splitlines():
        if line.startswith("worktree "):
            first = line[len("worktree ") :].strip()
            break
    base = Path(first) if first else None
    if base and base.is_dir() and base.resolve() != path.resolve():
        return base
    return None


def _remove(target: Path, base: Path | None) -> None:
    if base is not None and is_git_repo(base):
        try:
            _run(["git", "worktree", "remove", "--force", str(target)], cwd=base)
            _run(["git", "worktree", "prune"], cwd=base)
        except Exception:
            pass
    if target.is_symlink() or target.is_file():
        try:
            target.unlink()
        except OSError:
            pass
    elif target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def workspace_is_clean(workdir: Path) -> bool:
    """True when a git workspace has no staged, unstaged, or untracked changes."""
    if not is_git_repo(workdir) and not (workdir / ".git").is_file():
        return True
    try:
        p = _run(["git", "status", "--porcelain"], cwd=workdir)
    except Exception:
        return False
    return p.returncode == 0 and not p.stdout.strip()


def git_diff_all(workdir: Path) -> str:
    """Unified diff of tracked changes PLUS untracked files (rendered as
    /dev/null additions so new files appear in the evidence diff).

    Capped to _MAX_DIFF_CHARS; callers slice further as needed.
    """
    parts: list[str] = []
    try:
        p = _run(["git", "diff", "--", "."], cwd=workdir)
        if p.returncode == 0 and p.stdout:
            parts.append(p.stdout)
        st = _run(["git", "status", "--porcelain"], cwd=workdir)
        if st.returncode != 0:
            return "\n".join(parts)[:_MAX_DIFF_CHARS]
        shown = 0
        for line in st.stdout.splitlines():
            if not line.startswith("??"):
                continue
            rel = line[3:].strip().strip('"')
            if shown >= _MAX_UNTRACKED_FILES:
                parts.append(
                    f"... +{len(st.stdout.splitlines()) - shown} more untracked files"
                )
                break
            f = workdir / rel
            try:
                if not f.is_file() or f.stat().st_size > _MAX_UNTRACKED_BYTES:
                    parts.append(
                        f"--- /dev/null\n+++ b/{rel}\n(binary or oversized file, not shown)"
                    )
                    shown += 1
                    continue
            except OSError:
                continue
            d = subprocess.run(
                ["git", "diff", "--no-index", "--", "/dev/null", str(f)],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
            # --no-index exits 1 when files differ; output still usable.
            if d.stdout:
                parts.append(d.stdout)
            shown += 1
    except Exception:
        pass
    return "\n".join(parts)[:_MAX_DIFF_CHARS]
