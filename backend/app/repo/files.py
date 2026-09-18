"""Repo file browser: transparent VS Code-like read/edit of the agent workdir.

Same workdir the agent uses (cloned `local_path`, demo fallback) so what you
see is what the agent sees. Reads are open; writes go through the standard
`require_api_token` gate (open in dev, bearer in prod).

Security: every path is jailed with `tools.registry._resolve` — `..` escapes
and absolute paths are rejected. `.git` internals and `.env` files are never
served or written. Sizes are capped so a huge bundle can't OOM the UI.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..logging import get_logger, log_event
from ..models import Repository
from ..security import require_api_token

router = APIRouter(prefix="/api/repos", tags=["repo-files"])
logger = get_logger("fixhub.repo-files")

SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
    }
)
MAX_FILES = 5000
MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 500_000


class SaveFileBody(BaseModel):
    full_name: str = ""
    path: str = ""
    content: str = ""


class ExecBody(BaseModel):
    full_name: str = ""
    cmd: str = ""


def resolve_workdir(repo: Repository) -> tuple[Path, str]:
    """Return (workdir, root_kind) using the same rule as the chat runner."""
    if repo.local_path:
        p = Path(repo.local_path)
        if p.is_dir():
            return p, "workspace"
    demo = Path(__file__).resolve().parents[3] / "demo" / "fastapi-jwt"
    return demo, "demo"


def _get_repo(db: Session, full_name: str) -> Repository:
    name = (full_name or "").strip()
    if not name or "/" not in name:
        raise HTTPException(status_code=400, detail="full_name must be owner/repo")
    repo = db.query(Repository).filter_by(full_name=name).first()
    if repo is None:
        raise HTTPException(
            status_code=404, detail="repo not known — connect or clone it first"
        )
    return repo


@router.get("/files")
def list_files(
    full_name: str = Query(..., description="owner/repo"),
    db: Session = Depends(get_db),
) -> dict:
    repo = _get_repo(db, full_name)
    workdir, root = resolve_workdir(repo)
    if not workdir.is_dir():
        raise HTTPException(
            status_code=400, detail="repo workspace not found — clone it first"
        )
    out: list[dict] = []
    truncated = False
    for p in sorted(workdir.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(workdir)
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        out.append({"path": rel.as_posix(), "size": size})
        if len(out) >= MAX_FILES:
            truncated = True
            break
    return {"repo": repo.full_name, "root": root, "files": out, "truncated": truncated}


@router.get("/file")
def read_file(
    full_name: str = Query(..., description="owner/repo"),
    path: str = Query(..., description="workdir-relative file path"),
    db: Session = Depends(get_db),
) -> dict:
    from ..tools.registry import _resolve

    repo = _get_repo(db, full_name)
    workdir, _ = resolve_workdir(repo)
    rel = (path or "").strip().lstrip("/")
    if not rel or rel.startswith(".git") or rel == ".env" or rel.endswith("/.env"):
        raise HTTPException(status_code=403, detail="path not servable")
    target = _resolve(workdir, rel)
    if target is None:
        raise HTTPException(status_code=403, detail="path escapes workdir")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        size = target.stat().st_size
        raw = target.read_bytes()[: MAX_READ_BYTES + 1]
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}")
    if b"\x00" in raw[:8192]:
        raise HTTPException(status_code=400, detail="binary file — not shown")
    truncated = len(raw) > MAX_READ_BYTES
    text = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    return {
        "repo": repo.full_name,
        "path": rel,
        "content": text,
        "size": size,
        "truncated": truncated,
    }


@router.post("/file")
def save_file(
    body: SaveFileBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    from ..tools.registry import _resolve

    repo = _get_repo(db, body.full_name)
    workdir, _ = resolve_workdir(repo)
    rel = (body.path or "").strip().lstrip("/")
    if not rel or rel.startswith(".git") or rel == ".env" or rel.endswith("/.env"):
        raise HTTPException(status_code=403, detail="path not writable")
    if len(body.content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise HTTPException(status_code=413, detail="file too large (500KB cap)")
    target = _resolve(workdir, rel)
    if target is None:
        raise HTTPException(status_code=403, detail="path escapes workdir")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content, encoding="utf-8")
        size = target.stat().st_size
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"write failed: {e}")
    log_event(logger, "repo_file_saved", repo=repo.full_name, path=rel, size=size)
    return {"status": "ok", "repo": repo.full_name, "path": rel, "size": size}


@router.post("/exec")
def exec_command(
    body: ExecBody,
    db: Session = Depends(get_db),
    _auth: None = Depends(require_api_token),
) -> dict:
    """VS Code-like terminal: allow-listed commands only, same sandbox as the agent.

    Same policy as agent tools (`tools.registry.run_command`): allowed prefixes
    only, denied substrings rejected, Docker isolation required. Output capped.
    """
    from ..tools.registry import run_command

    repo = _get_repo(db, body.full_name)
    workdir, _ = resolve_workdir(repo)
    if not workdir.is_dir():
        raise HTTPException(
            status_code=400, detail="repo workspace not found — clone it first"
        )
    cmd = (body.cmd or "").strip()[:500]
    if not cmd:
        raise HTTPException(status_code=400, detail="cmd required")
    out = run_command(workdir, cmd, timeout=120)
    log_event(logger, "repo_exec", repo=repo.full_name, ok=out.get("ok"))
    return {
        "repo": repo.full_name,
        "cmd": cmd[:200],
        "ok": bool(out.get("ok")),
        "output": str(out.get("output", ""))[-4000:],
    }
