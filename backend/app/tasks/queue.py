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


_LIVE_JOB_STATES = {"queued", "started", "deferred", "scheduled"}


def is_repair_round(task) -> bool:
    """True when a QUEUED row is a CI repair round, not an initial run.

    `ciwatch` records the attempt number before re-queuing, so the row itself
    says which job kind must be used. Initial CI tasks always have attempt 0.
    """
    try:
        return (
            str(getattr(task, "trigger_type", "") or "") == "ci"
            and int(getattr(task, "ci_attempt_count", 0) or 0) > 0
        )
    except (TypeError, ValueError):
        return False


def job_id_for_task(task) -> str:
    """Deterministic RQ job id for the row's CURRENT run kind.

    Repair rounds use `fixhub-repair-<id>-<attempt>` (ciwatch); everything else
    uses `fixhub-task-<id>`. Reconcilers must probe the same id the enqueue
    path will use, otherwise a live repair job looks like a missing job.
    """
    if is_repair_round(task):
        return f"fixhub-repair-{task.id}-{int(task.ci_attempt_count or 0)}"
    return f"fixhub-task-{task.id}"


def enqueue_for_task(task_id: int) -> dict[str, Any]:
    """Enqueue the correct job kind for an existing task row. Never raises."""
    from app.db.database import SessionLocal as _SessionLocal
    from app.db.models import Task as _Task

    db = _SessionLocal()
    try:
        task = db.query(_Task).filter(_Task.id == task_id).first()
        if task is None:
            return {"enqueued": False, "error": "task not found"}
        if is_repair_round(task):
            return enqueue_repair(task_id)
        return enqueue_task(task_id)
    finally:
        db.close()


def _concurrency_blocked(db, task) -> str:
    """Phase 4: global / per-user / per-repo running-task caps (0 = unlimited).

    Returns a reason string when the task must NOT be enqueued right now.
    """
    from app.db.models import Task as _Task

    # QUEUED is backlog, not execution: counting it here let a stalled queue
    # block every future enqueue (the exact "nothing ever starts" deadlock).
    live = ["RUNNING", "AWAITING_CI"]
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
    The DB claim remains the final arbiter against true races.

    A FINISHED/FAILED job keeps its id for `result_ttl` (24h). Treating that
    leftover as a live duplicate made an explicit retry silently do nothing,
    so terminal leftovers are deleted and the id reused.
    """
    try:
        existing = q.fetch_job(job_id)
    except Exception:
        existing = None
    if existing is not None:
        getter = getattr(existing, "get_status", None)
        if getter is None:
            # Legacy/fake job object: preserve the old duplicate contract.
            return {"enqueued": True, "job_id": existing.id, "duplicate": True}
        try:
            status = str(getter(refresh=True) or "").lower()
        except Exception:
            # Unknown queue state: never risk double execution; fail clear.
            return {
                "enqueued": False,
                "error": "queue job state unavailable; retry",
                "job_id": job_id,
            }
        if status in _LIVE_JOB_STATES:
            return {"enqueued": True, "job_id": existing.id, "duplicate": True}
        try:
            existing.delete()
        except Exception as exc:
            return {
                "enqueued": False,
                "error": f"stale queue job could not be replaced: {exc}"[:300],
                "job_id": job_id,
            }
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
            # Terminal state must free the active dedupe identity, exactly like
            # every other FAILED transition (see service._release_active_keys).
            try:
                from app.tasks.service import _release_active_keys

                _release_active_keys(task)
            except Exception:
                task.dedupe_key = None
                task.active_issue_key = None
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
    """Health for GET /api/queue/health. Never raises.

    `workers` is the number of live RQ workers attached to this queue (0 = the
    API can enqueue but nothing will ever run). `db_queued` is the number of
    tasks waiting in QUEUED; `db_queued_stale` those older than the reconciler
    grace window (should trend to 0 once the reconciler is running).
    """
    q = get_queue()
    if q is None:
        return {"ok": False, "error": "redis unavailable", "queue": settings.QUEUE_NAME}
    try:
        workers = -1
        try:
            from rq import Worker

            workers = int(Worker.count(queue=q))
        except Exception:
            workers = -1
        db_queued, stale = _db_queued_counts()
        return {
            "ok": True,
            "queue": settings.QUEUE_NAME,
            "count": len(q),
            "workers": workers,
            "db_queued": db_queued,
            "db_queued_stale": stale,
            "redis_url": settings.REDIS_URL.split("@")[-1],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300], "queue": settings.QUEUE_NAME}


def _db_queued_counts(stale_after_s: int = 60) -> tuple[int, int]:
    """(QUEUED tasks, QUEUED older than the reconciler grace window)."""
    try:
        from datetime import timedelta

        from app.db.database import SessionLocal, utcnow
        from app.db.models import Task

        s = SessionLocal()
        try:
            total = s.query(Task).filter(Task.status == "QUEUED").count()
            cutoff = utcnow() - timedelta(seconds=stale_after_s)
            stale = (
                s.query(Task)
                .filter(Task.status == "QUEUED", Task.created_at < cutoff)
                .count()
            )
            return int(total), int(stale)
        finally:
            s.close()
    except Exception:
        return 0, 0
