"""Ephemeral workspace lifecycle + git ops (run inside worker, on sandbox copy)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def _run(cmd: list[str], cwd: Path, timeout: int = 120) -> str:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    p.check_returncode()
    return p.stdout


def clone_to_temp(repo_url: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="fixhub-ws-"))
    _run(["git", "clone", "--depth", "1", repo_url, str(tmp / "repo")], cwd=tmp)
    return tmp / "repo"


def git_status(workdir: Path) -> str:
    return _run(["git", "status", "--short"], cwd=workdir)


def git_diff(workdir: Path) -> str:
    return _run(["git", "diff"], cwd=workdir)


def git_log(workdir: Path, n: int = 20) -> str:
    return _run(["git", "log", f"-{n}", "--oneline"], cwd=workdir)


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
