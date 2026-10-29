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


_VALID_ENVS = {"dev", "staging", "prod"}


def _env_name() -> str:
    """Validated environment name. A typo must never silently disable guards.

    Before this check `ENV=production` (or any unrecognized value) skipped
    every production guard because each one compared against the literal
    "prod". Unknown values now refuse startup instead of running unprotected.
    """
    env = (settings.ENV or "").strip().lower()
    if env not in _VALID_ENVS:
        raise RuntimeError(
            f"refusing startup: unknown ENV={settings.ENV!r} "
            "(expected dev|staging|prod)"
        )
    return env


@app.on_event("startup")
def _create_tables() -> None:
    from app.db import migrate as _migrate
    from app.db.database import ensure_columns

    # Phase 4.5: production/staging fail-fast. Never silently run a deployed
    # environment on SQLite, insecure cookies, or without credential encryption.
    _enforce_production_guards()
    # Final hardening: timeout/usage coherence validated on every boot.
    _validate_startup_timeouts()
    from app.config import validate_usage_limits as _validate_usage

    try:
        _validate_usage(settings)
    except ValueError as exc:
        # Both deployed environments and dev refuse an incoherent usage config;
        # the message differs only in whether guards are environment-scoped.
        raise RuntimeError(f"refusing startup: {exc}") from exc
    # Gate 0: Alembic is the schema authority. In production/staging a failed
    # migration stops startup (MigrationFailed propagates — NO create_all
    # fallback, NO partial schema). Dev/test keep create_all bootstrapping
    # (explicit local convenience only, loudly).
    _migrate.upgrade_head()
    if _env_name() == "dev":
        Base.metadata.create_all(bind=engine)
        ensure_columns()
    # Production incident fix (#70/#71): a deployed boot whose workspace
    # root this process identity cannot use MUST fail here — the failure
    # mode if allowed to proceed is a publish-time `git add` Permission
    # denied half-way through staging a task's fix. Dev logs a warning.
    try:
        from app.repo import workspace_preflight as _preflight

        _ws_failures = _preflight.preflight_workspace_root()
    except Exception as exc:  # preflight must never crash the boot itself
        _ws_failures = [f"<preflight-error: {str(exc)[:120]}>"]
    if _ws_failures:
        if _env_name() in {"staging", "prod"}:
            raise RuntimeError(
                "refusing startup: workspace root unusable: "
                + "; ".join(_ws_failures[:3])
            )
        logging.getLogger("fixhub.workspace").warning(
            "workspace root unusable in dev: %s", "; ".join(_ws_failures[:3])
        )
    _promote_admins()
    _sweep_stale_running_tasks()
    _start_ci_watcher()
    _start_recovery_sweep()
    _start_queued_reconciler()


def _enforce_production_guards() -> None:
    """Phase 4.5 + final hardening: refuse to boot a deployed env unsafe.

    Applies to `staging` as well as `prod`: staging holds real repositories,
    real user rows, and (often) real credentials, so the same data/secret/
    sandbox invariants must hold. Dev stays permissive. ENV is validated
    first so a typo cannot bypass every guard.
    """
    from app.config import validate_timeout_ladder, validate_usage_limits

    env = _env_name()
    if env not in {"staging", "prod"}:
        return
    # Keep the historical "production startup" wording (existing runbooks and
    # tests match it); staging gets its own explicit label.
    label = "production" if env == "prod" else env
    url = (settings.DATABASE_URL or "").strip().lower()
    if url.startswith("sqlite"):
        raise RuntimeError(
            f"refusing {label} startup on SQLite (set a postgresql DATABASE_URL)"
        )
    if not url.startswith("postgres"):
        raise RuntimeError(
            f"refusing {label} startup on unsupported database: {url[:32]}"
        )
    if not bool(settings.AUTH_COOKIE_SECURE):
        raise RuntimeError(
            f"refusing {label} startup with insecure auth cookie "
            "(set AUTH_COOKIE_SECURE=1)"
        )
    if not (settings.FIXHUB_CREDENTIAL_ENCRYPTION_KEY or "").strip():
        raise RuntimeError(
            f"refusing {label} startup without FIXHUB_CREDENTIAL_ENCRYPTION_KEY"
        )
    # Final hardening: unbounded workers and incoherent ladders fail closed.
    try:
        validate_usage_limits(settings)
    except ValueError as exc:
        raise RuntimeError(f"refusing {label} startup: {exc}") from exc
    try:
        validate_timeout_ladder(settings)
    except ValueError as exc:
        raise RuntimeError(f"refusing {label} startup: {exc}") from exc
    # Phase 5: deployed environments require the real microVM backend. The
    # legacy host subprocess sandbox must never serve untrusted code.
    # Checked last so each earlier guard keeps its own error (and its tests).
    backend = str(getattr(settings, "SANDBOX_BACKEND", "host") or "host").strip().lower()
    if backend != "firecracker":
        raise RuntimeError(
            f"refusing {label} startup on SANDBOX_BACKEND={backend!r} "
            "(deployed environments require SANDBOX_BACKEND=firecracker)"
        )
    try:
        from app.sandbox import images as _images

        prereq = _images.verify_artifacts()
        if not prereq["ok"]:
            raise RuntimeError(
                f"refusing {label} startup: firecracker prerequisites missing: "
                + "; ".join(prereq["missing"])
            )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"refusing {label} startup: sandbox check failed: {exc}")


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
        is_deployed = _env_name() in {"staging", "prod"}
        if is_deployed:
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

# Phase 6: retries orphaned QUEUED tasks (Redis restart, crash, webhook timeout).
_queued_reconciler_started = False


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
                # Terminal transition frees the active dedupe identity.
                t.dedupe_key = None
                t.active_issue_key = None
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
    # Durable webhook inbox: re-apply deliveries stuck in `received` (a crash
    # between the durable commit and its effects). Handlers are idempotent, so
    # replay can never duplicate a task.
    try:
        import logging as _logging

        from app.github.webhook import reprocess_pending_deliveries

        out = reprocess_pending_deliveries()
        if out.get("failed"):
            _logging.getLogger("fixhub.webhook").warning(
                "pending webhook replay: checked=%s failed=%s",
                out.get("checked"),
                out.get("failed"),
            )
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


def _reconcile_queued_tasks() -> None:
    """One reconciler tick. Never raises."""
    try:
        import logging

        from app.tasks import reconciler as _reconciler

        out = _reconciler.requeue_orphaned()
        if not out.get("ok", True):
            logging.getLogger("fixhub.reconciler").warning(
                "queued reconciler unavailable: %s", out.get("error", "unknown")
            )
        elif out.get("requeued") or out.get("failed"):
            logging.getLogger("fixhub.reconciler").info(
                "queued reconciler requeued=%s skipped=%s failed=%s",
                out.get("requeued"),
                out.get("skipped"),
                out.get("failed"),
            )
    except Exception:
        pass


def _start_queued_reconciler() -> None:
    """Re-enqueue orphaned QUEUED tasks every 60s (startup tick included)."""
    global _queued_reconciler_started
    if _queued_reconciler_started:
        return
    _queued_reconciler_started = True

    import threading
    import time as _time

    _reconcile_queued_tasks()

    def _tick() -> None:
        while True:
            _time.sleep(60)
            if _shutdown is not None and _shutdown.is_set():
                return
            _reconcile_queued_tasks()

    threading.Thread(target=_tick, daemon=True, name="queued-reconciler").start()


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
    from app.github.webhook import _active_issue_task, _start_task

    dup = _active_issue_task(db, repo_name=full_name, number=number)
    if dup is not None:
        out = {
            "task_id": dup.id,
            "queued": dup.status in ("QUEUED", "RUNNING", "AWAITING_CI"),
            "duplicate": "active_task",
        }
        if dup.status == "QUEUED":
            # Make the Fix button immediate even when the original enqueue was
            # lost (Redis restart/crash); the reconciler also covers this.
            started = _start_task(db, dup.id)
            out["queued"] = bool(started.get("enqueued"))
            if not out["queued"]:
                out["error"] = str(started.get("error") or "queue unavailable")[:300]
        return out
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
    queue = _start_task(db, task.id)
    out = {"task_id": task.id, "queued": bool(queue.get("enqueued"))}
    if not queue.get("enqueued"):
        out["error"] = str(queue.get("error") or "queue unavailable")[:300]
    return out


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
    from app.db.models import TaskEvent

    t = require_owned_task(task_id, user, db)
    events = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.id.asc())
        .all()
    )
    out = _task_summary(t)
    out["events"] = [
        {"type": e.type, "data": e.data_json, "at": str(e.created_at)} for e in events
    ]
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

    A FAILED/BLOCKED/CANCELLED/NEEDS_REVIEW task is first moved back to QUEUED
    with an atomic CAS. Without that transition `claim_task` (correctly) never
    claims it and the endpoint looked successful while doing nothing. The CAS
    also serializes double-clicks: exactly one request re-queues.
    """
    require_csrf(request)
    row = require_owned_task(task_id, user, db)
    status = str(row.status or "").upper()
    if status in {"FAILED", "BLOCKED", "CANCELLED", "NEEDS_REVIEW"}:
        from sqlalchemy.exc import IntegrityError

        from app.tasks.service import _active_keys_for, cas_status

        fields: dict = {
            "error": "",
            "claimed_by": "",
            "claimed_at": None,
            "lease_expires_at": None,
        }
        # Re-take the active identity in the SAME update (a crash between two
        # commits would otherwise leave a terminal row holding the key).
        fields.update(_active_keys_for(row))
        try:
            moved = cas_status(db, task_id, {status}, "QUEUED", **fields)
        except IntegrityError:
            db.rollback()
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=409,
                content={
                    "task_id": task_id,
                    "queued": False,
                    "error": (
                        "another active task already covers this issue/revision"
                    ),
                },
            )
        if not moved:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=409,
                content={
                    "task_id": task_id,
                    "queued": False,
                    "error": "task state changed; refresh and retry",
                },
            )
        db.expire_all()
    elif status not in {"QUEUED", "RUNNING"}:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=409,
            content={
                "task_id": task_id,
                "queued": False,
                "error": (
                    f"task is {status}; it cannot be re-queued"
                    + (
                        " (it will be repaired automatically when CI finishes)"
                        if status == "AWAITING_CI"
                        else ""
                    )
                ),
            },
        )
    if sync:
        from app.tasks.service import run_task_inline

        result = run_task_inline(task_id)
        return {"task_id": task_id, "result": result, "sync": True}
    from app.tasks import queue as _queue

    out = _queue.enqueue_for_task(task_id)
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


@app.post("/api/tasks/cancel-unconnected")
def cancel_unconnected_tasks(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(_db),
) -> dict:
    """Cancel QUEUED tasks whose repository is not connected in FixHub.

    Cleanup for rows created before the connected-repo guard, when an
    account-wide GitHub App installation was mistaken for opt-in consent. Only
    QUEUED rows are touched; running work is never killed by this endpoint.
    """
    import json as _json

    from app.db.models import TaskEvent

    require_csrf(request)
    connected = {
        name
        for (name,) in (
            db.query(Repository.github_full_name)
            .filter(Repository.installation_id != "")
            .all()
        )
    }
    rows = (
        db.query(Task)
        .filter(Task.status == "QUEUED", Task.cancel_requested == 0)
        .order_by(Task.id.asc())
        .all()
    )
    ids: list[int] = []
    for t in rows:
        if t.repository in connected:
            continue
        t.status = "CANCELLED"
        t.cancel_requested = 1
        t.error = "repository not connected in FixHub; task cancelled"[:1000]
        # Terminal transition frees the active dedupe identity (a cancelled
        # issue task must not block a later trigger via the unique index).
        t.dedupe_key = None
        t.active_issue_key = None
        db.add(
            TaskEvent(
                task_id=t.id,
                type="CANCELLED",
                data_json=_json.dumps({"reason": "repository_not_connected"})[:4000],
            )
        )
        ids.append(t.id)
    db.commit()
    return {"cancelled": len(ids), "task_ids": ids}


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
