"""Fixhub API: health, tasks, memories, demo trigger, metrics, eval."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, get_db, init_db
from .automation import router as automation_router
from .agent.api import router as agent_router
from .chat.router import router as chat_router
from .github.api import router as github_api_router
from .github.webhook import router as webhook_router
from .repo.files import router as repo_files_router
from .review.router import router as review_router
from .logging import get_logger
from .metrics import snapshot as metrics_snapshot
from .models import Memory, Task, TaskEvent, VerificationRun
from .security import require_api_token

logger = get_logger("fixhub.api")


class DemoTrigger(BaseModel):
    issue: str = "Expired authentication tokens return HTTP 500"


# --- simple in-memory rate limiter (per IP, per minute) for demo trigger ---
_hits: dict[str, list[float]] = {}


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    window = 60.0
    limit = settings.rate_limit_per_min
    hits = _hits.get(ip, [])
    hits = [h for h in hits if now - h < window]
    if len(hits) >= limit:
        _hits[ip] = hits
        return True
    hits.append(now)
    _hits[ip] = hits
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_prod()
    if settings.is_prod:
        # Production schema evolution goes through Alembic, never bare
        # create_all (Phase 16). Dev/test keep create_all (+ _ensure_columns).
        try:
            from alembic import command as _alembic_command
            from alembic.config import Config as _AlembicConfig

            cfg = _AlembicConfig()
            cfg.set_main_option("script_location", "alembic")
            _alembic_command.upgrade(cfg, "head")
        except Exception as e:
            logger.error(f"alembic upgrade failed, falling back to init_db: {e}")
            init_db()
    else:
        init_db()
    # Background threads die with the process: any task left mid-loop states
    # (ANALYZING..VERIFYING) has no owner anymore — mark it FAILED (audited)
    # instead of leaving it stuck forever. Re-running provisions a clean
    # workspace, so nothing is lost.
    try:
        from .automation import recover_interrupted_tasks

        db = SessionLocal()
        try:
            recovered = recover_interrupted_tasks(db)
            if recovered:
                logger.warning(f"recovered {recovered} interrupted task(s) as FAILED")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"interrupted-task recovery failed: {e}")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Fixhub", lifespan=lifespan)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        import uuid

        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Request-ID"] = request_id
        if settings.is_prod:
            # HSTS only in prod (requires TLS via Caddy/ALB).
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )
    app.include_router(webhook_router)
    app.include_router(github_api_router)
    app.include_router(chat_router)
    app.include_router(agent_router)
    app.include_router(review_router)
    app.include_router(automation_router)
    app.include_router(repo_files_router)

    @app.get("/health")
    def health() -> dict:
        base_url, _, model = settings.resolved_llm()
        return {
            "status": "ok",
            "model": model,
            "provider": settings.llm_provider,
            "provider_base_url": base_url,
        }

    @app.get("/health/ready")
    def ready() -> dict:
        """Prod readiness: app + DB + queue backend reachable (no secrets)."""
        checks: dict[str, str] = {}
        try:
            from sqlalchemy import text

            from .db import SessionLocal

            db = SessionLocal()
            try:
                db.execute(text("SELECT 1"))
                checks["database"] = "ok"
            finally:
                db.close()
        except Exception as e:
            checks["database"] = f"error: {type(e).__name__}"
        try:
            from .queue import backend as queue_backend

            checks["queue"] = queue_backend()
            if settings.is_prod and checks["queue"] == "memory":
                checks["queue"] = "memory (DEGRADED — Redis unreachable, not durable)"
        except Exception:
            checks["queue"] = "unknown"
        ok = checks.get("database") == "ok" and not str(
            checks.get("queue", "")
        ).startswith("memory (DEGRADED")
        return {"status": "ready" if ok else "degraded", "checks": checks}

    @app.get("/metrics")
    def metrics() -> dict:
        """LLM + task counters for the frontend header and interviews."""
        return metrics_snapshot()

    @app.get("/api/provider")
    def provider_info() -> dict:
        base_url, has_key, model = settings.resolved_llm()
        return {
            "provider": settings.llm_provider,
            "base_url": base_url,
            "model": model,
            "has_key": bool(has_key),
        }

    @app.get("/api/tasks")
    def list_tasks(
        repo: str = "", include_test: bool = False, db: Session = Depends(get_db)
    ) -> list[dict]:
        # Phase 10: optional repo scoping at the SQL level so one repo's
        # tasks are never mixed into another's view. Empty = all (dev).
        # Test/demo fixtures (demo/*, acme/*, test/*, ...) are hidden by
        # default so the UI never drowns in pytest rows. Pass
        # ?include_test=true to see them, or ?repo=demo/... explicitly.
        from .models import Repository as _Repo
        from .models import is_test_repo_name as _is_test

        q = db.query(Task).order_by(Task.id.desc())
        if repo.strip():
            r = db.query(_Repo).filter_by(full_name=repo.strip()).first()
            q = q.filter_by(repo_id=r.id if r else -1)
            rows = q.limit(50).all()
        else:
            rows = q.limit(200).all()
            if not include_test:
                filtered: list = []
                repo_cache: dict[int, str] = {}
                for t in rows:
                    name = repo_cache.get(t.repo_id)
                    if name is None:
                        rr = db.query(_Repo).filter_by(id=t.repo_id).first()
                        name = rr.full_name if rr else ""
                        repo_cache[t.repo_id] = name
                    if not _is_test(name):
                        filtered.append(t)
                    if len(filtered) >= 50:
                        break
                rows = filtered
        return [
            {
                "id": t.id,
                "title": t.title,
                "state": t.state,
                "issue": t.issue_number,
                "repo_id": t.repo_id,
            }
            for t in rows
        ]

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int, db: Session = Depends(get_db)) -> dict:
        t = db.query(Task).filter_by(id=task_id).first()
        if not t:
            return {"error": "not found"}
        mems = db.query(Memory).filter_by(repo_id=t.repo_id).limit(20).all()
        events = (
            db.query(TaskEvent)
            .filter_by(task_id=t.id)
            .order_by(TaskEvent.id.asc())
            .limit(200)
            .all()
        )
        runs = db.query(VerificationRun).filter_by(task_id=t.id).all()
        from .models import Approval, Patch, PullRequest

        patch = (
            db.query(Patch).filter_by(task_id=t.id).order_by(Patch.id.desc()).first()
        )
        pr = (
            db.query(PullRequest)
            .filter_by(task_id=t.id)
            .order_by(PullRequest.id.desc())
            .first()
        )
        approvals = (
            db.query(Approval)
            .filter_by(task_id=t.id)
            .order_by(Approval.id.desc())
            .limit(10)
            .all()
        )
        return {
            "id": t.id,
            "title": t.title,
            "state": t.state,
            "issue": t.issue_number,
            "repo_id": t.repo_id,
            "memories": [{"type": m.type, "fact": m.fact} for m in mems],
            "events": [
                {
                    "stage": e.stage,
                    "message": (e.message or ""),
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in events
            ],
            "verification": [
                {
                    "check": r.check,
                    "passed": r.passed,
                    "status": r.status or ("PASS" if r.passed else "FAIL"),
                    "required": bool(r.required),
                    "output": (r.output or "")[:2000],
                    "phase": r.phase or "AFTER",
                    "attribution": r.attribution or "NONE",
                    "signature": (r.signature or "").split(";") if r.signature else [],
                    "duration_ms": int(r.duration_ms or 0),
                }
                for r in runs
            ],
            "diff": (patch.diff if patch else "") or "",
            "branch": (patch.branch if patch else "") or "",
            "pr_url": pr.url if pr else "",
            "pr_number": pr.number if pr else 0,
            "approvals": [
                {"decision": a.decision, "approver": a.approver, "reason": a.reason}
                for a in approvals
            ],
        }

    @app.get("/api/eval")
    def eval_info() -> dict:
        from .eval.benchmark import EVAL_RESULTS, METRICS, TASKS

        return {"tasks": TASKS, "metrics": METRICS, "results": EVAL_RESULTS}

    @app.post("/api/demo/trigger", response_model=None)
    def demo_trigger(
        body: DemoTrigger | None = None,
        db: Session = Depends(get_db),
        request: Request = None,  # type: ignore[assignment]
        _auth: None = Depends(require_api_token),
    ):
        """Demo mode removed: demo/ fixtures were deleted per user request.

        Kept as a 410 so old UI builds fail loudly instead of silently
        creating demo/fastapi-jwt rows that pollute Source Control.
        Clone a real repo and POST /api/tasks instead.
        """
        return JSONResponse(
            status_code=410,
            content={
                "error": "demo mode removed — clone a real repo and POST /api/tasks"
            },
        )

    return app


app = create_app()
