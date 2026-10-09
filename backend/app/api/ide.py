"""VS Code-like IDE APIs for the FixHub frontend.

All routes are scoped to a task workspace (the isolated clone the agent edits):

- GET    /api/tasks/{id}/files?path=.        list directory
- GET    /api/tasks/{id}/file?path=...       read file (bounded)
- PUT    /api/tasks/{id}/file                save file {path, content}
- GET    /api/tasks/{id}/diff                git status + unified diff
- GET    /api/tasks/{id}/events?after=0      poll agent trace (TraceView/chat)
- POST   /api/tasks/{id}/terminal {command}  sandboxed command (terminal panel)
- POST   /api/tasks/{id}/chat {message}      append a user note (chat panel)
- GET    /api/tasks/{id}/chat/stream         SSE stream of new events
- POST   /api/tasks/{id}/approve {title?, body?}  ReviewPanel Approve & Commit

Path safety mirrors agent/tools.py: repo-relative, no absolute, no `..`
escape, sensitive files blocked.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_csrf
from app.db.models import Task, TaskEvent, User
from app.repo import workspace as _ws

router = APIRouter()

_SENSITIVE = re.compile(
    r"(^|/)(\.env(\..*)?|\.git/.*|.*\.pem$|.*\.key$|.*secret.*|.*token.*|.*credentials.*)$",
    re.IGNORECASE,
)

_MAX_READ_BYTES = 200_000


def _db():
    from app.db.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _task_or_404(db: Session, task_id: int, user: User | None = None) -> Task:
    """Task lookup scoped to the authenticated user (Phase 2).

    Unknown IDs and other users' tasks both 404 (no existence leak).
    Callers without a user (none remain in production) get the legacy lookup.
    """
    q = db.query(Task).filter(Task.id == task_id)
    if user is not None:
        q = q.filter(Task.owner_id == user.id)
    t = q.first()
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    return t


def _workspace_or_410(t: Task) -> str:
    path = t.workspace or _ws.workspace_path(t.id)
    if not path or not os.path.isdir(path):
        raise HTTPException(status_code=410, detail="workspace expired (re-run the task)")
    return path


def _resolve(workspace: str, rel: str) -> str:
    """Single choke point shared with the agent tool layer (no drift)."""
    from app.agent import tools as _tools

    try:
        return _tools._resolve(workspace, (rel or ".").strip() or ".")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300])


def _is_sensitive(rel: str) -> bool:
    return bool(_SENSITIVE.search((rel or "").replace(os.sep, "/")))


def _event(db: Session, task_id: int, type_: str, data: dict) -> None:
    db.add(TaskEvent(task_id=task_id, type=type_, data_json=json.dumps(data)[:4000]))


@router.get("/api/tasks/{task_id}/files")
def list_files(task_id: int, path: str = Query(default="."),
               user: User = Depends(get_current_user),
               db: Session = Depends(_db)) -> dict:
    t = _task_or_404(db, task_id, user)
    ws = _workspace_or_410(t)
    full = _resolve(ws, path)
    if not os.path.isdir(full):
        raise HTTPException(status_code=404, detail=f"not a directory: {path}")
    try:
        entries = sorted(os.listdir(full))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:300])
    out = []
    for name in entries[:1000]:
        if name == ".git":
            continue
        p = os.path.join(full, name)
        try:
            is_dir = os.path.isdir(p)
            size = 0 if is_dir else os.path.getsize(p)
        except OSError:
            continue
        out.append({"name": name, "is_dir": is_dir, "size": size})
    return {"path": path, "entries": out}


@router.get("/api/tasks/{task_id}/file")
def read_file(
    task_id: int,
    path: str = Query(default=""),
    offset: int = Query(default=1, ge=1),
    limit: int = Query(default=500, ge=1, le=2000),
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> dict:
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    if _is_sensitive(path):
        raise HTTPException(status_code=403, detail="access to sensitive file is blocked")
    t = _task_or_404(db, task_id, user)
    ws = _workspace_or_410(t)
    full = _resolve(ws, path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail=f"file not found: {path}")
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:300])
    total = len(lines)
    chunk = lines[offset - 1 : offset - 1 + limit]
    content = "".join(chunk)
    truncated = len(content) >= _MAX_READ_BYTES or (offset - 1 + limit) < total
    return {
        "path": path,
        "content": content[:_MAX_READ_BYTES],
        "offset": offset,
        "limit": limit,
        "total_lines": total,
        "truncated": truncated,
    }


@router.put("/api/tasks/{task_id}/file")
def save_file(task_id: int, payload: dict, request: Request,
              user: User = Depends(get_current_user),
              db: Session = Depends(_db)) -> dict:
    require_csrf(request)
    path = str(payload.get("path", ""))
    content = payload.get("content", "")
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")
    if _is_sensitive(path):
        raise HTTPException(status_code=403, detail="writing to sensitive file is blocked")
    if len(content) > 500_000:
        raise HTTPException(status_code=413, detail="file too large (500KB max via UI)")
    t = _task_or_404(db, task_id, user)
    ws = _workspace_or_410(t)
    full = _resolve(ws, path)
    try:
        os.makedirs(os.path.dirname(full) or full, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:300])
    _event(db, t.id, "FILE_CHANGED", {"tool": "ide_save", "args": {"path": path[:200]}, "ok": True})
    db.commit()
    return {"ok": True, "path": path, "bytes": len(content)}


@router.get("/api/tasks/{task_id}/diff")
def task_diff(task_id: int, user: User = Depends(get_current_user),
              db: Session = Depends(_db)) -> dict:
    import subprocess as _sp

    from app.github import publisher as _pub

    t = _task_or_404(db, task_id, user)
    ws = _workspace_or_410(t)
    try:
        files = _pub.changed_files(ws)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:300])

    def _git(*args: str, cap: int = 16000) -> str:
        try:
            proc = _sp.run(["git", *args], cwd=ws, capture_output=True, text=True, timeout=30)
        except Exception as exc:
            return f"ERROR: {exc}"
        out = (proc.stdout or "") + (proc.stderr or "")
        return out[:cap] + (f"\n...[truncated {len(out) - cap} bytes]..." if len(out) > cap else "")

    return {
        "branch": t.branch or "",
        "status": t.status,
        "files": files,
        "stat": _git("diff", "--stat", cap=4000),
        "diff": _git("diff", cap=60000),
    }


@router.get("/api/tasks/{task_id}/events")
def task_events(
    task_id: int,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> dict:
    _task_or_404(db, task_id, user)
    rows = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id, TaskEvent.id > after)
        .order_by(TaskEvent.id.asc())
        .limit(limit)
        .all()
    )
    return {
        "events": [
            {"id": e.id, "type": e.type, "data": e.data_json, "at": str(e.created_at)} for e in rows
        ]
    }


@router.post("/api/tasks/{task_id}/terminal")
def run_terminal(task_id: int, payload: dict, request: Request,
                 user: User = Depends(get_current_user),
                 db: Session = Depends(_db)) -> dict:
    from app.sandbox import sandbox as _sandbox
    from app.sandbox.backend import run_command as _dispatch

    require_csrf(request)
    command = str(payload.get("command", ""))
    if not command.strip():
        raise HTTPException(status_code=400, detail="command is required")
    t = _task_or_404(db, task_id, user)
    ws = _workspace_or_410(t)
    try:
        res = _dispatch(ws, command)
    except _sandbox.SandboxBlockedError as exc:
        raise HTTPException(status_code=403, detail=f"blocked: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=403, detail=f"blocked: {exc}")
    _event(db, t.id, "COMMAND_RUN", {"tool": "ide_terminal", "command": command[:200],
                                     "exit": res.exit_code})
    db.commit()
    return {"exit": res.exit_code, "stdout": res.stdout, "stderr": res.stderr,
            "truncated": res.truncated, "duration_ms": res.duration_ms,
            "timed_out": res.timed_out, "cwd": res.cwd}


@router.post("/api/tasks/{task_id}/chat")
def post_chat(task_id: int, payload: dict, request: Request,
              user: User = Depends(get_current_user),
              db: Session = Depends(_db)) -> dict:
    require_csrf(request)
    message = str(payload.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    t = _task_or_404(db, task_id, user)
    _event(db, t.id, "CHAT_MSG", {"message": message[:2000]})
    db.commit()
    return {"ok": True}


@router.get("/api/tasks/{task_id}/chat/stream")
def chat_stream(task_id: int, after: int = Query(default=0, ge=0),
                user: User = Depends(get_current_user),
                db: Session = Depends(_db)):
    """SSE stream of task events (chat + trace). Polls DB for ~60s.

    Frontend uses this for live TraceView updates while the worker runs.
    Event format: `data: {json}\n\n`, with `: ping` keepalives.
    """
    _task_or_404(db, task_id, user)

    def _gen():
        from app.db.database import SessionLocal as _SessionLocal

        cursor = after
        for _ in range(60):
            s = _SessionLocal()
            try:
                rows = (
                    s.query(TaskEvent)
                    .filter(TaskEvent.task_id == task_id, TaskEvent.id > cursor)
                    .order_by(TaskEvent.id.asc())
                    .limit(100)
                    .all()
                )
                for e in rows:
                    cursor = max(cursor, e.id)
                    yield f"data: {json.dumps({'id': e.id, 'type': e.type, 'data': e.data_json, 'at': str(e.created_at)})}\n\n"
                if rows:
                    pass
                else:
                    yield ": ping\n\n"
            finally:
                s.close()
            import time as _time

            _time.sleep(1)
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/api/tasks/{task_id}/approve")
def approve(task_id: int, request: Request, payload: dict | None = None,
            user: User = Depends(get_current_user),
            db: Session = Depends(_db)) -> dict:
    """ReviewPanel Approve & Commit: publish pending NEEDS_REVIEW changes."""
    from app.tasks.service import approve_task as _approve

    require_csrf(request)
    _task_or_404(db, task_id, user)
    title = str((payload or {}).get("title", ""))
    body = str((payload or {}).get("body", ""))
    # approve_task manages its own session (needs fresh state after agent run).
    result = _approve(task_id, title=title, body=body)
    if result.get("status") == "FAILED":
        raise HTTPException(status_code=500, detail=result.get("error", "approve failed")[:500])
    return result


@router.get("/api/tasks/{task_id}/verification")
def verification(task_id: int, user: User = Depends(get_current_user),
                 db: Session = Depends(_db)) -> dict:
    """VerificationView: test/verification signals from COMMAND_RUN events + status."""
    import json as _json

    t = _task_or_404(db, task_id, user)
    rows = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.id.asc())
        .limit(1000)
        .all()
    )
    commands = []
    files_changed: list[str] = []
    for e in rows:
        try:
            data = _json.loads(e.data_json or "{}")
        except Exception:
            data = {}
        if e.type == "COMMAND_RUN":
            commands.append({"command": str(data.get("command", data.get("args", "")))[:300],
                             "at": str(e.created_at)})
        if e.type == "FILE_CHANGED":
            args = data.get("args", {})
            p = args.get("path", "") if isinstance(args, dict) else ""
            if p and p not in files_changed:
                files_changed.append(p)
    return {
        "status": t.status,
        "branch": t.branch or "",
        "pr_url": t.pr_url or "",
        "commit_sha": t.commit_sha or "",
        "error": t.error or "",
        "files_changed": files_changed[:50],
        "commands_run": commands[-20:],
        "checks": [
            {"name": "agent finished", "passed": any(e.type == "AGENT_FINISHED" for e in rows)},
            {"name": "files changed", "passed": bool(files_changed)},
            {"name": "verification command run", "passed": bool(commands)},
            {"name": "published", "passed": bool(t.pr_url or t.commit_sha)},
        ],
    }
