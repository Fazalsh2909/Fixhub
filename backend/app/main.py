"""FixHub FastAPI entrypoint. Routers are mounted here; heavy logic lives in subpackages."""
from __future__ import annotations

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.api.ide import router as ide_router
from app.config import settings
from app.db.database import Base, engine
from app.db.models import PullRequest, Repository, Task  # noqa: F401  (register tables)
from app.github.webhook import router as github_router

app = FastAPI(title=settings.APP_NAME)
app.include_router(github_router)
app.include_router(ide_router)


@app.on_event("startup")
def _create_tables() -> None:
    from app.db.database import ensure_columns

    Base.metadata.create_all(bind=engine)
    ensure_columns()
    _sweep_stale_running_tasks()
    _start_ci_watcher()


_watcher_started = False


def _start_ci_watcher() -> None:
    """In-process CI watcher tick (single backend instance).

    Every 60s it checks AWAITING_CI tasks and enqueues repair jobs for fresh
    failures. Daemon thread; never raises; safe to call twice.
    """
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True

    import threading
    import time as _time

    def _tick() -> None:
        while True:
            _time.sleep(60)
            try:
                from app.tasks import ciwatch as _ciwatch

                _ciwatch.check_awaiting_ci()
            except Exception:
                pass

    threading.Thread(target=_tick, daemon=True, name="ci-watcher").start()


def _sweep_stale_running_tasks() -> None:
    """Crash recovery: tasks stuck RUNNING longer than a job could ever take
    (worker killed/restarted mid-run) are marked FAILED so they never wedge
    the queue forever. Re-run to retry. Never raises."""
    try:
        # NOTE: SQLite returns naive datetimes, so compare naive-to-naive.
        from datetime import datetime, timedelta

        from app.db.database import SessionLocal
        from app.db.models import TaskEvent

        db = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(
                seconds=settings.JOB_TIMEOUT_S + 600
            )
            stale = (
                db.query(Task)
                .filter(Task.status == "RUNNING", Task.updated_at < cutoff)
                .all()
            )
            for t in stale:
                t.status = "FAILED"
                t.error = "stale: worker never finished (restart/crash); re-run to retry"[:1000]
                t.updated_at = datetime.utcnow()
                db.add(TaskEvent(task_id=t.id, type="FAILED",
                                 data_json='{"reason": "stale_sweep"}'))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "env": settings.ENV}


def _db():
    from app.db.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/api/repositories")
def list_repositories(db: Session = Depends(_db)) -> list[dict]:
    rows = db.query(Repository).order_by(Repository.id.desc()).limit(200).all()
    return [
        {
            "id": r.id,
            "github_full_name": r.github_full_name,
            "default_branch": r.default_branch,
            "connected": bool(r.installation_id),
        }
        for r in rows
    ]


@app.post("/api/repositories/connect")
def connect_repository(payload: dict, db: Session = Depends(_db)) -> dict:
    """Store the GitHub App installation mapping for a repo (enables issues + clone)."""
    from fastapi import HTTPException

    full_name = str(payload.get("github_full_name", "")).strip()
    installation_id = str(payload.get("installation_id", "")).strip()
    if not full_name or "/" not in full_name:
        raise HTTPException(status_code=400, detail="github_full_name must be owner/repo")
    if not installation_id:
        raise HTTPException(status_code=400, detail="installation_id is required")
    repo = db.query(Repository).filter(Repository.github_full_name == full_name).first()
    if not repo:
        repo = Repository(github_full_name=full_name)
        db.add(repo)
    repo.installation_id = installation_id
    db.commit()
    db.refresh(repo)
    return {"id": repo.id, "github_full_name": repo.github_full_name, "connected": True}


@app.get("/api/github/installations")
def list_installations() -> list[dict] | dict:
    """Show where the GitHub App is installed (id + account). Needs App key configured."""
    from fastapi.responses import JSONResponse

    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    try:
        token = _app_auth.app_jwt()
        return _gh.list_installations(app_jwt=token)
    except RuntimeError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)[:300]})
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": f"github api failed: {exc}"[:300]})


@app.get("/api/github/repos")
def list_installed_repos(db: Session = Depends(_db)) -> list[dict] | dict:
    """Every repo the GitHub App can see, grouped by installation, with the
    FixHub `connected` flag (installation mapping stored = runs enabled).

    Unconnected repos show a one-click Connect in the UI; the manual
    owner/repo + installation form stays for forks/OSS repos outside these
    installations. Never raises: per-installation failures degrade to []."""
    from fastapi.responses import JSONResponse

    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    try:
        app_token = _app_auth.app_jwt()
        installations = _gh.list_installations(app_jwt=app_token)
    except RuntimeError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)[:300]})
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": f"github api failed: {exc}"[:300]})
    rows = {r.github_full_name: r for r in db.query(Repository).all()}
    out = []
    for inst in installations:
        entry: dict = {"installation_id": inst.get("id", ""),
                       "account": inst.get("account", ""),
                       "type": inst.get("type", ""), "repos": []}
        try:
            token = _app_auth.installation_token(inst.get("id", ""))
            repos = _gh.list_installation_repos(token=token)
        except Exception:
            repos = []
        for repo in repos:
            row = rows.get(repo["full_name"])
            entry["repos"].append({
                "github_full_name": repo["full_name"],
                "private": repo["private"],
                "default_branch": repo["default_branch"],
                "connected": bool(row and row.installation_id),
                "installation_id": (row.installation_id if row and row.installation_id
                                    else inst.get("id", "")),
            })
        out.append(entry)
    return out


@app.get("/api/github/issues")
def list_github_issues(repo: str, db: Session = Depends(_db)) -> list[dict] | dict:
    """Live open issues for a connected repo (installation token, PRs excluded)."""
    from fastapi.responses import JSONResponse

    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    row = db.query(Repository).filter(Repository.github_full_name == repo).first()
    if not row or not row.installation_id:
        return JSONResponse(
            status_code=400,
            content={"error": f"{repo} is not connected (set installation_id via POST /api/repositories/connect)"},
        )
    try:
        token = _app_auth.installation_token(row.installation_id)
        return _gh.list_issues(token=token, full_name=repo)
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": f"github api failed: {exc}"[:300]})


@app.get("/api/github/contents")
def read_repo_dir(repo: str, path: str = ".", ref: str = "", db: Session = Depends(_db)) -> list[dict] | dict:
    """Read-only directory listing of a connected repo at ref (branch/sha).

    Powers the in-IDE repo browser and the published-changes view for expired
    task workspaces. Never raises: unconnected repos 400, GitHub failures 502.
    """
    from fastapi.responses import JSONResponse

    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    row = db.query(Repository).filter(Repository.github_full_name == repo).first()
    if not row or not row.installation_id:
        return JSONResponse(
            status_code=400,
            content={"error": f"{repo} is not connected (set installation_id via POST /api/repositories/connect)"},
        )
    try:
        token = _app_auth.installation_token(row.installation_id)
        return _gh.repo_dir_contents(token=token, full_name=repo, path=path, ref=ref)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)[:300]})
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": f"github api failed: {exc}"[:300]})


@app.get("/api/github/file")
def read_repo_file(repo: str, path: str, ref: str = "", db: Session = Depends(_db)) -> dict:
    """Read-only file content of a connected repo at ref (branch/sha).

    Returns {content, truncated, binary, size}. Unconnected repos 400,
    GitHub failures 502.
    """
    from fastapi.responses import JSONResponse

    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    row = db.query(Repository).filter(Repository.github_full_name == repo).first()
    if not row or not row.installation_id:
        return JSONResponse(
            status_code=400,
            content={"error": f"{repo} is not connected (set installation_id via POST /api/repositories/connect)"},
        )
    try:
        token = _app_auth.installation_token(row.installation_id)
        return _gh.repo_file_content(token=token, full_name=repo, path=path, ref=ref)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)[:300]})
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": f"github api failed: {exc}"[:300]})


@app.get("/api/tasks/{task_id}/published-diff")
def task_published_diff(task_id: int, db: Session = Depends(_db)) -> dict:
    """Unified diff of the task's pull request, for expired workspaces.

    Returns the DiffInfo shape {branch, status, files, stat, diff}. 404 when
    the task has no PR yet, 400 when its repo is not connected, 502 on
    GitHub failures.
    """
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    if not t.pr_number:
        raise HTTPException(status_code=404, detail="task has no pull request yet")
    row = db.query(Repository).filter(Repository.github_full_name == t.repository).first()
    if not row or not row.installation_id:
        raise HTTPException(status_code=400, detail=f"{t.repository} is not connected")
    try:
        token = _app_auth.installation_token(row.installation_id)
        files = _gh.pull_files(token=token, full_name=t.repository, number=t.pr_number)
        dd = _gh.pull_diff(token=token, full_name=t.repository, number=t.pr_number)
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": f"github api failed: {exc}"[:300]})
    return {"branch": t.branch or "", "status": "published", "files": files, "stat": "",
            "diff": dd["diff"], "truncated": dd["truncated"],
            "pr_number": t.pr_number, "pr_url": t.pr_url or ""}


@app.post("/api/tasks/from-issue")
def create_task_from_issue(payload: dict, db: Session = Depends(_db)) -> dict:
    """Create a RUNNING task from a live GitHub issue (Fix button). Body: {repository, issue_number}."""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from app.db.models import TaskEvent
    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    full_name = str(payload.get("repository", "")).strip()
    try:
        number = int(payload.get("issue_number", 0))
    except (TypeError, ValueError):
        number = 0
    if not full_name or "/" not in full_name or number <= 0:
        raise HTTPException(status_code=400, detail="repository (owner/repo) and issue_number required")
    row = db.query(Repository).filter(Repository.github_full_name == full_name).first()
    if not row or not row.installation_id:
        raise HTTPException(status_code=400, detail=f"{full_name} is not connected")
    try:
        token = _app_auth.installation_token(row.installation_id)
        issue = _gh.get_issue(token=token, full_name=full_name, number=number)
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": f"github api failed: {exc}"[:300]})
    import json as _json

    task = Task(
        repository=full_name,
        repository_id=row.id,
        trigger_type="issue",
        issue_number=issue["number"],
        issue_title=issue["title"][:500],
        issue_body=issue["body"][:8000],
        issue_url=issue["url"],
        status="RUNNING",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    db.add(TaskEvent(task_id=task.id, type="TASK_CREATED", data_json=_json.dumps({"trigger": "ui-fix", "issue": number})[:4000]))
    db.commit()
    return {"task_id": task.id}


@app.get("/api/tasks")
def list_tasks(db: Session = Depends(_db)) -> list[dict]:
    rows = db.query(Task).order_by(Task.id.desc()).limit(200).all()
    return [_task_summary(t) for t in rows]


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: int, db: Session = Depends(_db)) -> dict:
    from fastapi import HTTPException

    from app.db.models import Memory, TaskEvent

    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    events = (
        db.query(TaskEvent).filter(TaskEvent.task_id == task_id).order_by(TaskEvent.id.asc()).all()
    )
    mems = (
        db.query(Memory).filter(Memory.repository == t.repository).order_by(Memory.id.asc()).all()
    )
    out = _task_summary(t)
    out["events"] = [{"type": e.type, "data": e.data_json, "at": str(e.created_at)} for e in events]
    out["memory"] = [{"path": m.path, "summary": m.summary} for m in mems]
    return out


@app.post("/api/tasks/{task_id}/run")
def rerun_task(task_id: int, sync: bool = False, db: Session = Depends(_db)) -> dict:
    """Run entrypoint. Default enqueues to Redis (worker executes).

    Pass ?sync=true to run inline (tests, local dev without Redis).
    Falls back to inline when Redis is unavailable so OSS dev never breaks.
    """
    from fastapi import HTTPException

    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    if sync:
        from app.tasks.service import run_task_inline

        result = run_task_inline(task_id)
        return {"task_id": task_id, "result": result, "sync": True}
    from app.tasks import queue as _queue

    out = _queue.enqueue_task(task_id)
    if out.get("enqueued"):
        return {"task_id": task_id, "queued": True, "job_id": out.get("job_id")}
    from app.tasks.service import run_task_inline as _inline

    result = _inline(task_id)
    return {"task_id": task_id, "result": result, "sync": True,
            "queue_fallback": out.get("error", "redis unavailable")}


@app.get("/api/queue/health")
def queue_health() -> dict:
    """Worker/Redis health for ops + frontend status bar."""
    from app.tasks import queue as _queue

    return _queue.queue_health()


@app.post("/api/tasks/{task_id}/cleanup")
def cleanup_task(task_id: int, db: Session = Depends(_db)) -> dict:
    """Manually wipe a per-task workspace (fresh per-task guarantee)."""
    from fastapi import HTTPException

    from app.tasks.service import cleanup_task_workspace

    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    removed = cleanup_task_workspace(task_id)
    return {"task_id": task_id, "removed": removed}


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: int, db: Session = Depends(_db)) -> dict:
    """Request cancellation of a running task.

    Sets a flag the agent loop polls every iteration and before every tool
    call; the run stops promptly with status CANCELLED. Never raises 5xx.
    """
    from fastapi import HTTPException

    from app.db.models import TaskEvent

    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    if t.status in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
        return {"task_id": task_id, "status": t.status, "already_terminal": True}
    t.cancel_requested = 1
    db.add(TaskEvent(task_id=t.id, type="CANCEL_REQUESTED", data_json="{}"))
    db.commit()
    return {"task_id": task_id, "status": t.status, "cancel_requested": True}


@app.post("/api/cron/ci-watch")
def cron_ci_watch() -> dict:
    """Manually trigger one CI-watch pass (the backend also ticks every 60s)."""
    from app.tasks import ciwatch as _ciwatch

    return _ciwatch.check_awaiting_ci()


def _task_summary(t: Task) -> dict:
    return {
        "id": t.id,
        "repository": t.repository,
        "trigger_type": t.trigger_type,
        "issue_number": t.issue_number,
        "issue_title": t.issue_title,
        "status": t.status,
        "branch": t.branch,
        "commit_sha": t.commit_sha,
        "pr_number": t.pr_number,
        "pr_url": t.pr_url,
        "error": t.error,
        "created_at": str(t.created_at),
        "updated_at": str(t.updated_at),
    }
