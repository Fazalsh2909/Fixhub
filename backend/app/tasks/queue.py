"""RQ/Redis queue for agent runs. Webhook enqueues; worker executes.

Phase 4: Redis is required infrastructure in production. enqueue_*() returns
{"enqueued": False} when Redis is down and callers must fail clearly — never
silently execute inline. Job IDs are deterministic per task so duplicate
delivery collapses to a single queued job instead of N executions.
Never raises.
"""

from __future__ import annotations

import os
import socket
from typing import Any

from app.config import settings


def worker_identity() -> str:
    """Stable-ish worker id for claim attribution (host:pid)."""
    try:
        host = socket.gethostname()
    except Exception:
        host = "worker"
    return f"{host}:{os.getpid()}"


def run_task_job(task_id: int, *, source: str = "", base: str = "main") -> dict:
    """RQ job entrypoint. Must stay importable as app.tasks.queue:run_task_job."""
    from app.tasks import service as _svc

    return _svc.run_task_inline(
        task_id, source=source, base=base, worker_id=worker_identity()
    )


def run_repair_job(task_id: int) -> dict:
    """RQ job entrypoint for CI repair rounds (same task/branch/PR)."""
    from app.tasks import service as _svc

    return _svc.run_task_inline(task_id, repair=True, worker_id=worker_identity())


def _concurrency_blocked(db, task) -> str:
    """Phase 4: global / per-user / per-repo running-task caps (0 = unlimited).

    Returns a reason string when the task must NOT be enqueued right now.
    """
    from app.db.models import Task as _Task

    live = ["QUEUED", "RUNNING", "AWAITING_CI"]
    if settings.MAX_RUNNING_TASKS_GLOBAL > 0:
        n = db.query(_Task).filter(_Task.status.in_(live)).count()
        if n >= settings.MAX_RUNNING_TASKS_GLOBAL:
            return "global concurrency limit reached"
    if task.owner_id is not None and settings.MAX_RUNNING_PER_USER > 0:
        n = (
            db.query(_Task)
            .filter(_Task.owner_id == task.owner_id, _Task.status.in_(live))
            .count()
        )
        if n >= settings.MAX_RUNNING_PER_USER:
            return "per-user concurrency limit reached"
    if settings.MAX_RUNNING_PER_REPO > 0:
        n = (
            db.query(_Task)
            .filter(_Task.repository == task.repository, _Task.status.in_(live))
            .count()
        )
        if n >= settings.MAX_RUNNING_PER_REPO:
            return "per-repository concurrency limit reached"
    return ""


def _usage_blocked(db, task) -> str:
    """Final hardening: daily per-user execution bound (DB-backed, so every
    API replica enforces the same budget). Repair rounds re-run the SAME task
    and never consume daily budget."""
    if task.owner_id is None:
        return ""
    try:
        daily_max = int(settings.MAX_TASKS_PER_USER_PER_DAY or 0)
    except (TypeError, ValueError):
        return ""
    if daily_max <= 0:
        return ""
    from app.db.database import utcnow
    from app.db.models import Task as _Task

    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n = (
        db.query(_Task)
        .filter(_Task.owner_id == task.owner_id, _Task.created_at >= day_start)
        .count()
    )
    if n > daily_max:
        return f"daily task limit reached ({daily_max}/day)"
    return ""


def _enqueue_deduped(q, fn, task_id: int, job_id: str, **kwargs) -> dict[str, Any]:
    """Enqueue with a deterministic job id. A repeat delivery of the same
    task collapses to the already-queued job instead of a second execution.
    The DB claim remains the final arbiter against true races."""
    try:
        existing = q.fetch_job(job_id)
    except Exception:
        existing = None
    if existing is not None:
        return {"enqueued": True, "job_id": existing.id, "duplicate": True}
    try:
        job = q.enqueue(fn, task_id, job_id=job_id, **kwargs)
        return {"enqueued": True, "job_id": job.id}
    except Exception as exc:
        # Lost a concurrent enqueue race: report the winner as duplicate.
        try:
            winner = q.fetch_job(job_id)
        except Exception:
            winner = None
        if winner is not None:
            return {"enqueued": True, "job_id": winner.id, "duplicate": True}
        return {"enqueued": False, "error": str(exc)[:300]}


def enqueue_repair(task_id: int) -> dict[str, Any]:
    """Enqueue one CI repair round. Returns {enqueued, job_id?, error?}. Never raises."""
    q = get_queue()
    if q is None:
        return {"enqueued": False, "error": "redis unavailable (worker not running?)"}
    from app.db.database import SessionLocal as _SessionLocal
    from app.db.models import Task as _Task

    db = _SessionLocal()
    try:
        task = db.query(_Task).filter(_Task.id == task_id).first()
        attempt = task.ci_attempt_count if task else 0
    finally:
        db.close()
    return _enqueue_deduped(
        q,
        run_repair_job,
        task_id,
        f"fixhub-repair-{task_id}-{attempt}",
        job_timeout=settings.JOB_TIMEOUT_S,
        result_ttl=86400,
        failure_ttl=86400,
        on_failure=job_failed,
    )


def job_failed(job, exc=None, *args, **kwargs) -> None:
    """RQ on_failure handler: never leave a task stuck RUNNING. Never raises.

    RQ >= 1.x invokes on_failure(job, connection, exc_type, exc_value,
    traceback) — five positionals. Older callers/tests may pass (job, exc).
    Both conventions are accepted; the real error is extracted defensively.
    """
    try:
        task_id = (job.args or [None])[0]
        if not isinstance(task_id, int):
            return
        err = kwargs.get("value", exc)
        if err is None:
            for candidate in (exc, *args):
                if isinstance(candidate, BaseException):
                    err = candidate
                    break
        if err is None and args:
            err = args[-1]
        from datetime import datetime, timezone

        from app.db.database import SessionLocal
        from app.db.models import Task, TaskEvent

        import json as _json

        db = SessionLocal()
        try:
            task = db.query(Task).filter(Task.id == task_id).first()
            if not task or task.status != "RUNNING":
                return
            task.status = "FAILED"
            task.error = f"worker job crashed: {err}"[:1000]
            task.updated_at = datetime.now(timezone.utc)
            db.add(
                TaskEvent(
                    task_id=task_id,
                    type="FAILED",
                    data_json=_json.dumps({"reason": "job_crash"})[:4000],
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def _redis_connection():
    try:
        import redis as _redis
    except ImportError:
        return None
    try:
        conn = _redis.Redis.from_url(settings.REDIS_URL, socket_timeout=5)
        conn.ping()
        return conn
    except Exception:
        return None


def get_queue():
    """Return an RQ Queue or None when Redis is unavailable (dev/test fallback)."""
    try:
        from rq import Queue as _Queue
    except ImportError:
        return None
    conn = _redis_connection()
    if conn is None:
        return None
    try:
        return _Queue(settings.QUEUE_NAME, connection=conn)
    except Exception:
        return None


def enqueue_task(
    task_id: int, *, source: str = "", base: str = "main"
) -> dict[str, Any]:
    """Enqueue one agent run. Returns {enqueued, job_id? , error?}. Never raises.

    Phase 4: concurrency caps are enforced here (fail-clear 429-style), and the
    job id is deterministic per task so repeat delivery collapses.
    Caps are checked before Redis is touched (no queue contact when limited).
    """
    from app.db.database import SessionLocal as _SessionLocal
    from app.db.models import Task as _Task

    db = _SessionLocal()
    try:
        task = db.query(_Task).filter(_Task.id == task_id).first()
        if task is None:
            return {"enqueued": False, "error": "task not found"}
        blocked = _concurrency_blocked(db, task)
        if blocked:
            return {"enqueued": False, "error": blocked, "limited": True}
        usage_hit = _usage_blocked(db, task)
        if usage_hit:
            return {"enqueued": False, "error": usage_hit, "limited": True}
    finally:
        db.close()
    q = get_queue()
    if q is None:
        return {"enqueued": False, "error": "redis unavailable (worker not running?)"}
    return _enqueue_deduped(
        q,
        run_task_job,
        task_id,
        f"fixhub-task-{task_id}",
        source=source,
        base=base,
        job_timeout=settings.JOB_TIMEOUT_S,
        result_ttl=86400,
        failure_ttl=86400,
        on_failure=job_failed,
    )


def queue_health() -> dict[str, Any]:
    """Health for GET /api/queue/health. Never raises."""
    q = get_queue()
    if q is None:
        return {"ok": False, "error": "redis unavailable", "queue": settings.QUEUE_NAME}
    try:
        return {
            "ok": True,
            "queue": settings.QUEUE_NAME,
            "count": len(q),
            "redis_url": settings.REDIS_URL.split("@")[-1],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300], "queue": settings.QUEUE_NAME}
