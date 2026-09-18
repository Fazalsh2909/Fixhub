"""Fixhub API: health, tasks, memories, demo trigger, metrics, eval."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db, init_db
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
    init_db()
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
        except Exception:
            checks["queue"] = "unknown"
        ok = checks.get("database") == "ok"
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
    def list_tasks(db: Session = Depends(get_db)) -> list[dict]:
        return [
            {"id": t.id, "title": t.title, "state": t.state, "issue": t.issue_number}
            for t in db.query(Task).order_by(Task.id.desc()).limit(50)
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
                    "message": e.message,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in events
            ],
            "verification": [
                {"check": r.check, "passed": r.passed, "output": r.output[:2000]}
                for r in runs
            ],
            "diff": patch.diff if patch else "",
            "branch": patch.branch if patch else "",
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
        """Demo mode: no GitHub needed. Runs real pytest repro on demo repo."""
        client_ip = request.client.host if request and request.client else "unknown"
        if _rate_limited(client_ip):
            return JSONResponse(
                status_code=429,
                content={"error": "rate limited — try again in a minute"},
            )

        from .agent.orchestrator import engineer_issue
        from .llm.openrouter import provider_from_settings
        from .metrics import record_task
        from .models import Repository

        repo = db.query(Repository).filter_by(full_name="demo/fastapi-jwt").first()
        if repo is None:
            repo = Repository(full_name="demo/fastapi-jwt")
            db.add(repo)
            db.commit()
            db.refresh(repo)
        issue_text = (
            body.issue if body else "Expired authentication tokens return HTTP 500"
        )
        task = Task(
            repo_id=repo.id, issue_number=142, title=issue_text, state="CREATED"
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        _, api_key, _ = settings.resolved_llm()
        if not api_key:
            # deterministic fallback so demo works with zero keys: run real verification only
            from .models import TaskEvent
            from .verify.pipeline import run_verification

            db.add(
                TaskEvent(
                    task_id=task.id,
                    stage="ANALYZING",
                    message="demo mode, no LLM key — verification-only path",
                )
            )
            db.commit()
            workdir = Path(__file__).resolve().parents[2] / "demo" / "fastapi-jwt"
            results = run_verification(db, task, workdir)
            verified = all(ok for _, ok in results)
            task.state = "READY_FOR_APPROVAL" if verified else "DEBUGGING"
            db.commit()
            record_task(verified)
            from .verify.pipeline import build_proof as _proof

            return {
                "task_id": task.id,
                "verified": verified,
                "mode": "verification-only (no LLM key)",
                "proof": _proof(
                    task,
                    results,
                    "(no files changed)",
                    "regression test FAILED (expired JWT -> HTTP 500)",
                    "FAIL (see verification rows)",
                ),
            }
        result = engineer_issue(
            db,
            task,
            Path(__file__).resolve().parents[2] / "demo" / "fastapi-jwt",
            provider_from_settings(),
        )
        record_task(bool(result.get("verified")))
        # Proof of Fix from real evidence: verification rows + edited files (TOOL events).
        from .models import TaskEvent
        from .verify.pipeline import build_proof

        edited = sorted(
            {
                m.split("edited ", 1)[1].split(" ::")[0]
                for (m,) in db.query(TaskEvent.message)
                .filter(
                    TaskEvent.task_id == task.id,
                    TaskEvent.stage == "TOOL",
                    TaskEvent.message.like("edit_file ok=True%"),
                )
                .all()
                if "edited " in m
            }
        )
        proof = build_proof(
            task,
            result.get("results", []),
            diff="\n".join(edited) or "(no files changed)",
            before="regression test FAILED (expired JWT -> HTTP 500)",
            after="PASS" if result.get("verified") else "FAIL (see verification rows)",
        )
        return {"task_id": task.id, "proof": proof, **result}

    return app


app = create_app()
