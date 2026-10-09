"""FixHub FastAPI entrypoint. Routers are mounted here; heavy logic lives in subpackages."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from sqlalchemy.orm import Session

from app.api.auth import router as auth_router
from app.api.deps import (
    get_current_user,
    require_admin,
    require_csrf,
    require_owned_repository_by_name,
    require_owned_task,
)
from app.api.ide import router as ide_router
from app.api.llm_keys import router as llm_keys_router
from app.config import settings
from app.db.database import Base, engine
from app.db.models import GitHubConnection, Repository, Task, User  # noqa: F401  (register tables)
from app.github.webhook import router as github_router

app = FastAPI(title=settings.APP_NAME)
app.include_router(auth_router)
app.include_router(llm_keys_router)
app.include_router(github_router)
app.include_router(ide_router)


@app.on_event("startup")
def _create_tables() -> None:
    from app.db import migrate as _migrate
    from app.db.database import ensure_columns

    # Phase 4.5: production fail-fast. Never silently run prod on SQLite,
    # insecure cookies, or without credential encryption.
    _enforce_production_guards()
    # Final hardening: timeout/usage coherence validated on every boot.
    _validate_startup_timeouts()
    from app.config import validate_usage_limits as _validate_usage

    try:
        _validate_usage(settings)
    except ValueError as exc:
        if settings.ENV.strip().lower() == "prod":
            raise RuntimeError(f"refusing production startup: {exc}") from exc
        raise RuntimeError(f"refusing startup: {exc}") from exc
    # Gate 0: Alembic is the schema authority. In production a failed
    # migration stops startup (MigrationFailed propagates — NO create_all
    # fallback, NO partial schema). Production never calls create_all or
    # ensure_columns at all; dev/test keep create_all bootstrapping
    # (explicit local convenience only, loudly).
    _migrate.upgrade_head()
    if settings.ENV.strip().lower() != "prod":
        Base.metadata.create_all(bind=engine)
        ensure_columns()
    _promote_admins()
    _sweep_stale_running_tasks()
    _start_ci_watcher()
    _start_recovery_sweep()


def _enforce_production_guards() -> None:
    """Phase 4.5 + final hardening: refuse to boot production unsafe."""
    from app.config import validate_timeout_ladder, validate_usage_limits

    if settings.ENV.strip().lower() != "prod":
        return
    url = (settings.DATABASE_URL or "").strip().lower()
    if url.startswith("sqlite"):
        raise RuntimeError(
            "refusing production startup on SQLite (set a postgresql DATABASE_URL)"
        )
    if not url.startswith("postgres"):
        raise RuntimeError(
            f"refusing production startup on unsupported database: {url[:32]}"
        )
    if not bool(settings.AUTH_COOKIE_SECURE):
        raise RuntimeError(
            "refusing production startup with insecure auth cookie (set AUTH_COOKIE_SECURE=1)"
        )
    if not (settings.FIXHUB_CREDENTIAL_ENCRYPTION_KEY or "").strip():
        raise RuntimeError(
            "refusing production startup without FIXHUB_CREDENTIAL_ENCRYPTION_KEY"
        )
    # Final hardening: unbounded workers and incoherent ladders fail closed.
    try:
        validate_usage_limits(settings)
    except ValueError as exc:
        raise RuntimeError(f"refusing production startup: {exc}") from exc
    try:
        validate_timeout_ladder(settings)
    except ValueError as exc:
        raise RuntimeError(f"refusing production startup: {exc}") from exc
    # Phase 5: production requires the real microVM backend. The legacy host
    # subprocess sandbox must never serve production traffic for untrusted code.
    # Checked last so each earlier guard keeps its own error (and its tests).
    backend = str(getattr(settings, "SANDBOX_BACKEND", "host") or "host").strip().lower()
    if backend != "firecracker":
        raise RuntimeError(
            f"refusing production startup on SANDBOX_BACKEND={backend!r} "
            "(production requires SANDBOX_BACKEND=firecracker)"
        )
    try:
        from app.sandbox import images as _images

        prereq = _images.verify_artifacts()
        if not prereq["ok"]:
            raise RuntimeError(
                "refusing production startup: firecracker prerequisites missing: "
                + "; ".join(prereq["missing"])
            )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"refusing production startup: sandbox check failed: {exc}")


def _validate_startup_timeouts() -> None:
    """Final hardening: dev validates too; slow-model dev needs explicit flag.

    Production always enforces the strict ladder. Non-prod with coherent
    defaults passes silently. Non-prod with an incoherent ladder (e.g. legacy
    1500/1800 overrides) raises unless DEV_SLOW_MODEL=1, which documents the
    relaxation as intentional instead of silently accepting it.
    """
    from app.config import validate_timeout_ladder

    try:
        validate_timeout_ladder(settings)
        return
    except ValueError as exc:
        is_prod = settings.ENV.strip().lower() == "prod"
        if is_prod:
            raise RuntimeError(f"refusing startup: {exc}") from exc
        if int(getattr(settings, "DEV_SLOW_MODEL", 0) or 0) == 1:
            return
        raise RuntimeError(
            f"incoherent timeout/lease configuration (set DEV_SLOW_MODEL=1 to "
            f"explicitly allow slow-model dev values): {exc}"
        ) from exc


def _promote_admins() -> None:
    """Phase 4.5: idempotent ADMIN_EMAILS promotion (never demotes)."""
    try:
        from app.db.database import SessionLocal
        from app.db.models import User

        emails = {
            e.strip().lower()
            for e in (settings.ADMIN_EMAILS or "").split(",")
            if e.strip()
        }
        if not emails:
            return
        db = SessionLocal()
        try:
            for user in db.query(User).filter(User.email.in_(sorted(emails))).all():
                if not user.is_admin:
                    user.is_admin = 1
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


_recovery_started = False


_watcher_started = False


# Phase 4: set on shutdown so daemon ticks stop scheduling new passes.
_shutdown = None


@app.on_event("shutdown")
def _on_shutdown() -> None:
    global _shutdown
    try:
        import threading as _threading

        if _shutdown is None:
            _shutdown = _threading.Event()
        _shutdown.set()
    except Exception:
        pass


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
            if _shutdown is not None and _shutdown.is_set():
                return
            try:
                from app.tasks import ciwatch as _ciwatch

                _ciwatch.check_awaiting_ci()
            except Exception:
                pass

    threading.Thread(target=_tick, daemon=True, name="ci-watcher").start()


def _sweep_stale_running_tasks() -> None:
    """Phase 4 crash recovery (periodic, lease-based).

    - RUNNING with an expired lease -> QUEUED for safe re-claim (same branch
      reused by the next worker; never a second branch).
    - RUNNING with no lease older than JOB_TIMEOUT_S+600 (pre-Phase-4 rows) ->
      FAILED as before.
    Never raises. Safe to run from multiple API replicas (lease expiry is the
    single arbiter; re-queue is idempotent by status).
    """
    try:
        from datetime import timedelta

        from app.db.database import SessionLocal, utcnow
        from app.db.models import TaskEvent

        db = SessionLocal()
        try:
            now = utcnow()
            expired = (
                db.query(Task)
                .filter(
                    Task.status == "RUNNING",
                    Task.lease_expires_at.isnot(None),
                    Task.lease_expires_at < now,
                )
                .all()
            )
            for t in expired:
                t.status = "QUEUED"
                t.claimed_by = ""
                t.lease_expires_at = None
                t.error = "lease expired (worker lost); re-queued for reclaim"[:1000]
                db.add(
                    TaskEvent(
                        task_id=t.id,
                        type="QUEUED",
                        data_json='{"reason": "lease_expired"}',
                    )
                )
            cutoff = now - timedelta(seconds=settings.JOB_TIMEOUT_S + 600)
            legacy = (
                db.query(Task)
                .filter(
                    Task.status == "RUNNING",
                    Task.lease_expires_at.is_(None),
                    Task.updated_at < cutoff,
                )
                .all()
            )
            for t in legacy:
                t.status = "FAILED"
                t.error = (
                    "stale: worker never finished (restart/crash); re-run to retry"[
                        :1000
                    ]
                )
                db.add(
                    TaskEvent(
                        task_id=t.id,
                        type="FAILED",
                        data_json='{"reason": "stale_sweep"}',
                    )
                )
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
    # Gate 0 (P0-2): the API tier MUST NOT reap microVMs. destroy_orphans()
    # used to run here, but this process cannot see worker-owned registry
    # entries, so a live VM looked orphaned. Reaping runs only in the
    # sandbox host's worker/manager process (see tasks/worker.py), keyed
    # on durable owner records + DB leases. The DB lease sweep above stays
    # the source of truth for task state.


def _start_recovery_sweep() -> None:
    """Run the lease sweep every 5 minutes (not only at startup)."""
    global _recovery_started
    if _recovery_started:
        return
    _recovery_started = True

    import threading
    import time as _time

    def _tick() -> None:
        while True:
            _time.sleep(300)
            if _shutdown is not None and _shutdown.is_set():
                return
            try:
                _sweep_stale_running_tasks()
            except Exception:
                pass

    threading.Thread(target=_tick, daemon=True, name="recovery-sweep").start()


@app.get("/health")
def health() -> dict:
    """Process aliveness only (no dependency checks)."""
    return {"status": "ok", "env": settings.ENV}


@app.get("/readiness")
def readiness() -> dict:
    """Phase 4: dependency readiness for orchestrators.

    Open (probes cannot authenticate) but leak-free: booleans only, no URLs,
    counts, or secrets. /health stays pure aliveness.
    """
    from fastapi.responses import JSONResponse

    pg_ok, redis_ok = False, False
    try:
        from sqlalchemy import text as _text

        from app.db.database import engine

        with engine.connect() as conn:
            conn.execute(_text("SELECT 1"))
        pg_ok = True
    except Exception:
        pg_ok = False
    try:
        import redis as _redis

        r = _redis.Redis.from_url(settings.REDIS_URL, socket_timeout=3)
        r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    # Phase 5: sandbox readiness (booleans only — no paths, versions, or secrets).
    try:
        from app.sandbox import images as _images

        prereq = _images.verify_artifacts()
        sandbox_ok = bool(prereq["ok"])
    except Exception:
        sandbox_ok = False
    try:
        from app.sandbox.backend import active_backend_name as _active_backend

        sandbox_backend = _active_backend()
    except Exception:
        sandbox_backend = "unknown"
    ready = pg_ok and redis_ok
    body = {
        "ready": ready,
        "postgres": pg_ok,
        "redis": redis_ok,
        "sandbox_backend": sandbox_backend,
        "sandbox_ready": sandbox_ok,
    }
    if ready:
        return body
    return JSONResponse(status_code=503, content=body)


def _db():
    from app.db.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/api/repositories")
def list_repositories(
    user: User = Depends(get_current_user), db: Session = Depends(_db)
) -> list[dict]:
    rows = (
        db.query(Repository)
        .filter(Repository.owner_id == user.id)
        .order_by(Repository.id.desc())
        .limit(200)
        .all()
    )
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
def connect_repository(
    payload: dict,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> dict:
    """Store the GitHub App installation mapping for a repo (enables issues + clone).

    Phase 2: the repository and installation claim belong to the authenticated
    user. Claiming another user's repository or installation returns 404.
    """
    from fastapi import HTTPException

    require_csrf(request)
    full_name = str(payload.get("github_full_name", "")).strip()
    installation_id = str(payload.get("installation_id", "")).strip()
    if not full_name or "/" not in full_name:
        raise HTTPException(
            status_code=400, detail="github_full_name must be owner/repo"
        )
    if not installation_id:
        raise HTTPException(status_code=400, detail="installation_id is required")
    repo = db.query(Repository).filter(Repository.github_full_name == full_name).first()
    if repo and repo.owner_id is not None and repo.owner_id != user.id:
        raise HTTPException(status_code=404, detail="repository not found")
    # An installation claimed by another user cannot be re-claimed here.
    other = (
        db.query(GitHubConnection)
        .filter(
            GitHubConnection.installation_id == installation_id,
            GitHubConnection.user_id != user.id,
        )
        .first()
    )
    if other:
        raise HTTPException(status_code=404, detail="repository not found")
    if not repo:
        repo = Repository(github_full_name=full_name, owner_id=user.id)
        db.add(repo)
    if repo.owner_id is None:
        repo.owner_id = user.id
    repo.installation_id = installation_id
    if (
        not db.query(GitHubConnection)
        .filter(
            GitHubConnection.user_id == user.id,
            GitHubConnection.installation_id == installation_id,
        )
        .first()
    ):
        db.add(GitHubConnection(user_id=user.id, installation_id=installation_id))
    db.commit()
    db.refresh(repo)
    return {"id": repo.id, "github_full_name": repo.github_full_name, "connected": True}


@app.get("/api/github/installations")
def list_installations(user: User = Depends(get_current_user)) -> list[dict] | dict:
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
        return JSONResponse(
            status_code=502, content={"error": f"github api failed: {exc}"[:300]}
        )


@app.get("/api/github/repos")
def list_installed_repos(
    user: User = Depends(get_current_user), db: Session = Depends(_db)
) -> list[dict] | dict:
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
        return JSONResponse(
            status_code=502, content={"error": f"github api failed: {exc}"[:300]}
        )
    rows = {
        r.github_full_name: r
        for r in db.query(Repository).filter(Repository.owner_id == user.id).all()
    }
    out = []
    for inst in installations:
        entry: dict = {
            "installation_id": inst.get("id", ""),
            "account": inst.get("account", ""),
            "type": inst.get("type", ""),
            "repos": [],
        }
        try:
            token = _app_auth.installation_token(inst.get("id", ""))
            repos = _gh.list_installation_repos(token=token)
        except Exception:
            repos = []
        for repo in repos:
            row = rows.get(repo["full_name"])
            entry["repos"].append(
                {
                    "github_full_name": repo["full_name"],
                    "private": repo["private"],
                    "default_branch": repo["default_branch"],
                    "connected": bool(row and row.installation_id),
                    "installation_id": (
                        row.installation_id
                        if row and row.installation_id
                        else inst.get("id", "")
                    ),
                }
            )
        out.append(entry)
    return out


@app.get("/api/github/issues")
def list_github_issues(
    repo: str, user: User = Depends(get_current_user), db: Session = Depends(_db)
) -> list[dict] | dict:
    """Live open issues for a connected repo (installation token, PRs excluded)."""
    from fastapi.responses import JSONResponse

    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    row = require_owned_repository_by_name(repo, user, db)
    if not row.installation_id:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"{repo} is not connected (set installation_id via POST /api/repositories/connect)"
            },
        )
    try:
        token = _app_auth.installation_token(row.installation_id)
        return _gh.list_issues(token=token, full_name=repo)
    except Exception as exc:
        return JSONResponse(
            status_code=502, content={"error": f"github api failed: {exc}"[:300]}
        )


@app.get("/api/github/contents")
def read_repo_dir(
    repo: str,
    path: str = ".",
    ref: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> list[dict] | dict:
    """Read-only directory listing of a connected repo at ref (branch/sha).

    Powers the in-IDE repo browser and the published-changes view for expired
    task workspaces. Never raises: unconnected repos 400, GitHub failures 502.
    """
    from fastapi.responses import JSONResponse

    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    row = require_owned_repository_by_name(repo, user, db)
    if not row.installation_id:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"{repo} is not connected (set installation_id via POST /api/repositories/connect)"
            },
        )
    try:
        token = _app_auth.installation_token(row.installation_id)
        return _gh.repo_dir_contents(token=token, full_name=repo, path=path, ref=ref)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)[:300]})
    except Exception as exc:
        return JSONResponse(
            status_code=502, content={"error": f"github api failed: {exc}"[:300]}
        )


@app.get("/api/github/file")
def read_repo_file(
    repo: str,
    path: str,
    ref: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> dict:
    """Read-only file content of a connected repo at ref (branch/sha).

    Returns {content, truncated, binary, size}. Unconnected repos 400,
    GitHub failures 502.
    """
    from fastapi.responses import JSONResponse

    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    row = require_owned_repository_by_name(repo, user, db)
    if not row.installation_id:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"{repo} is not connected (set installation_id via POST /api/repositories/connect)"
            },
        )
    try:
        token = _app_auth.installation_token(row.installation_id)
        return _gh.repo_file_content(token=token, full_name=repo, path=path, ref=ref)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)[:300]})
    except Exception as exc:
        return JSONResponse(
            status_code=502, content={"error": f"github api failed: {exc}"[:300]}
        )


@app.get("/api/tasks/{task_id}/published-diff")
def task_published_diff(
    task_id: int, user: User = Depends(get_current_user), db: Session = Depends(_db)
) -> dict:
    """Unified diff of the task's pull request, for expired workspaces.

    Returns the DiffInfo shape {branch, status, files, stat, diff}. 404 when
    the task has no PR yet, 400 when its repo is not connected, 502 on
    GitHub failures.
    """
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    t = require_owned_task(task_id, user, db)
    if not t.pr_number:
        raise HTTPException(status_code=404, detail="task has no pull request yet")
    row = (
        db.query(Repository).filter(Repository.github_full_name == t.repository).first()
    )
    if not row or not row.installation_id:
        raise HTTPException(status_code=400, detail=f"{t.repository} is not connected")
    try:
        token = _app_auth.installation_token(row.installation_id)
        files = _gh.pull_files(token=token, full_name=t.repository, number=t.pr_number)
        dd = _gh.pull_diff(token=token, full_name=t.repository, number=t.pr_number)
    except Exception as exc:
        return JSONResponse(
            status_code=502, content={"error": f"github api failed: {exc}"[:300]}
        )
    return {
        "branch": t.branch or "",
        "status": "published",
        "files": files,
        "stat": "",
        "diff": dd["diff"],
        "truncated": dd["truncated"],
        "pr_number": t.pr_number,
        "pr_url": t.pr_url or "",
    }


@app.post("/api/tasks/from-issue")
def create_task_from_issue(
    payload: dict,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> dict:
    """Create a RUNNING task from a live GitHub issue (Fix button). Body: {repository, issue_number}."""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from app.db.models import TaskEvent
    from app.github import app_auth as _app_auth
    from app.github import client as _gh

    require_csrf(request)
    full_name = str(payload.get("repository", "")).strip()
    try:
        number = int(payload.get("issue_number", 0))
    except (TypeError, ValueError):
        number = 0
    if not full_name or "/" not in full_name or number <= 0:
        raise HTTPException(
            status_code=400, detail="repository (owner/repo) and issue_number required"
        )
    row = require_owned_repository_by_name(full_name, user, db)
    if not row.installation_id:
        raise HTTPException(status_code=400, detail=f"{full_name} is not connected")
    try:
        token = _app_auth.installation_token(row.installation_id)
        issue = _gh.get_issue(token=token, full_name=full_name, number=number)
    except Exception as exc:
        return JSONResponse(
            status_code=502, content={"error": f"github api failed: {exc}"[:300]}
        )
    import json as _json

    task = Task(
        repository=full_name,
        repository_id=row.id,
        owner_id=user.id,
        trigger_type="issue",
        issue_number=issue["number"],
        issue_title=issue["title"][:500],
        issue_body=issue["body"][:8000],
        issue_url=issue["url"],
        status="QUEUED",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    db.add(
        TaskEvent(
            task_id=task.id,
            type="TASK_CREATED",
            data_json=_json.dumps({"trigger": "ui-fix", "issue": number})[:4000],
        )
    )
    db.commit()
    return {"task_id": task.id}


@app.get("/api/tasks")
def list_tasks(
    user: User = Depends(get_current_user), db: Session = Depends(_db)
) -> list[dict]:
    rows = (
        db.query(Task)
        .filter(Task.owner_id == user.id)
        .order_by(Task.id.desc())
        .limit(200)
        .all()
    )
    return [_task_summary(t) for t in rows]


@app.get("/api/tasks/{task_id}")
def task_detail(
    task_id: int, user: User = Depends(get_current_user), db: Session = Depends(_db)
) -> dict:
    from app.db.models import Memory, TaskEvent

    t = require_owned_task(task_id, user, db)
    events = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.id.asc())
        .all()
    )
    mems = (
        db.query(Memory)
        .filter(Memory.repository == t.repository, Memory.owner_id == user.id)
        .order_by(Memory.id.asc())
        .all()
    )
    out = _task_summary(t)
    out["events"] = [
        {"type": e.type, "data": e.data_json, "at": str(e.created_at)} for e in events
    ]
    out["memory"] = [{"path": m.path, "summary": m.summary} for m in mems]
    return out


@app.post("/api/tasks/{task_id}/run")
def rerun_task(
    task_id: int,
    request: Request,
    sync: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> dict:
    """Run entrypoint. Default enqueues to Redis (worker executes).

    Pass ?sync=true to run inline (explicit development/debug path only).
    When Redis is unavailable the task stays QUEUED and the failure is
    reported clearly — production never executes inline silently.
    """
    require_csrf(request)
    require_owned_task(task_id, user, db)
    if sync:
        from app.tasks.service import run_task_inline

        result = run_task_inline(task_id)
        return {"task_id": task_id, "result": result, "sync": True}
    from app.tasks import queue as _queue

    out = _queue.enqueue_task(task_id)
    if out.get("enqueued"):
        return {"task_id": task_id, "queued": True, "job_id": out.get("job_id")}
    if out.get("limited"):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=429,
            content={
                "task_id": task_id,
                "queued": False,
                "error": out.get("error", "concurrency limit reached"),
            },
        )
    return {
        "task_id": task_id,
        "queued": False,
        "error": out.get("error", "redis unavailable"),
    }


@app.get("/api/queue/health")
def queue_health(user: User = Depends(require_admin)) -> dict:
    """Phase 4.5: operational visibility requires ADMIN (not just login)."""
    from app.tasks import queue as _queue

    return _queue.queue_health()


@app.post("/api/tasks/{task_id}/cleanup")
def cleanup_task(
    task_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(_db),
) -> dict:
    """Phase 4.5: destructive workspace wipe requires ADMIN + ownership."""
    from app.tasks.service import cleanup_task_workspace

    require_csrf(request)
    require_owned_task(task_id, user, db)
    removed = cleanup_task_workspace(task_id)
    return {"task_id": task_id, "removed": removed}


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(
    task_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(_db),
) -> dict:
    """Request cancellation of a running task.

    Sets a flag the agent loop polls every iteration and before every tool
    call; the run stops promptly with status CANCELLED. Never raises 5xx.
    """
    from app.db.models import TaskEvent

    require_csrf(request)
    t = require_owned_task(task_id, user, db)
    if t.status in ("COMPLETED", "FAILED", "BLOCKED", "CANCELLED"):
        return {"task_id": task_id, "status": t.status, "already_terminal": True}
    if t.status == "QUEUED":
        # Never claimed: cancel outright so no worker picks it up.
        from app.tasks.service import cas_status

        cas_status(db, task_id, {"QUEUED"}, "CANCELLED")
        db.add(TaskEvent(task_id=t.id, type="CANCELLED", data_json='{"at": "cancel"}'))
        db.commit()
        return {"task_id": task_id, "status": "CANCELLED", "cancel_requested": True}
    t.cancel_requested = 1
    db.add(TaskEvent(task_id=t.id, type="CANCEL_REQUESTED", data_json="{}"))
    db.commit()
    return {"task_id": task_id, "status": t.status, "cancel_requested": True}


@app.post("/api/cron/ci-watch")
def cron_ci_watch(request: Request, user: User = Depends(require_admin)) -> dict:
    """Phase 4.5: manual CI-watch trigger requires ADMIN (plus CSRF)."""
    from app.tasks import ciwatch as _ciwatch

    require_csrf(request)
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
