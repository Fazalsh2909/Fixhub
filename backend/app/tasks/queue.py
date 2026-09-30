"""RQ/Redis queue for agent runs. Webhook enqueues; worker executes.

Fail-open for OSS dev: if redis/rq is missing or unreachable, enqueue_task()
returns {"enqueued": False} and callers fall back to manual /api/tasks/{id}/run.
Never raises.
"""
from __future__ import annotations

from typing import Any

from app.config import settings


def run_task_job(task_id: int, *, source: str = "", base: str = "main") -> dict:
    """RQ job entrypoint. Must stay importable as app.tasks.queue:run_task_job."""
    from app.tasks import service as _svc

    return _svc.run_task_inline(task_id, source=source, base=base)


def run_repair_job(task_id: int) -> dict:
    """RQ job entrypoint for CI repair rounds (same task/branch/PR)."""
    from app.tasks import service as _svc

    return _svc.run_task_inline(task_id, repair=True)


def enqueue_repair(task_id: int) -> dict[str, Any]:
    """Enqueue one CI repair round. Returns {enqueued, job_id?, error?}. Never raises."""
    q = get_queue()
    if q is None:
        return {"enqueued": False, "error": "redis unavailable (worker not running?)"}
    try:
        job = q.enqueue(
            run_repair_job,
            task_id,
            job_timeout=settings.JOB_TIMEOUT_S,
            result_ttl=86400,
            failure_ttl=86400,
            on_failure=job_failed,
        )
        return {"enqueued": True, "job_id": job.id}
    except Exception as exc:
        return {"enqueued": False, "error": str(exc)[:300]}


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
            db.add(TaskEvent(task_id=task_id, type="FAILED",
                             data_json=_json.dumps({"reason": "job_crash"})[:4000]))
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


def enqueue_task(task_id: int, *, source: str = "", base: str = "main") -> dict[str, Any]:
    """Enqueue one agent run. Returns {enqueued, job_id? , error?}. Never raises."""
    q = get_queue()
    if q is None:
        return {"enqueued": False, "error": "redis unavailable (worker not running?)"}
    try:
        job = q.enqueue(
            run_task_job,
            task_id,
            source=source,
            base=base,
            job_timeout=settings.JOB_TIMEOUT_S,
            result_ttl=86400,
            failure_ttl=86400,
            on_failure=job_failed,
        )
        return {"enqueued": True, "job_id": job.id}
    except Exception as exc:
        return {"enqueued": False, "error": str(exc)[:300]}


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
