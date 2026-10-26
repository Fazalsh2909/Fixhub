"""Re-enqueue QUEUED tasks orphaned by Redis/worker/API restarts.

Tasks are created and enqueued in one request. If that enqueue failed (Redis
down, process killed between commit and enqueue, webhook timed out) or a worker
lost its lease after claiming, the row is left QUEUED with no runnable job.
This sweep finds those rows and retries them.

Safe by construction:
- the RQ job id is deterministic per task (`fixhub-task-<id>`), so concurrent
  sweeps collapse to one job;
- `claim_task` CAS-guards QUEUED -> RUNNING, so a live run is never restarted;
- terminal jobs (failed/finished/cancelled) are deleted before re-enqueueing,
  otherwise RQ's deterministic-id dedupe would refuse the retry forever;
- only connected repositories are retried — unconnected repos are cleaned up
  via POST /api/tasks/cancel-unconnected, never run.
"""

from __future__ import annotations

GRACE_S = 60
BATCH = 50

_LIVE_JOB_STATES = {"queued", "started", "deferred", "scheduled"}


def _connected_repo_names(db) -> set[str]:
    from app.db.models import Repository

    rows = (
        db.query(Repository.github_full_name)
        .filter(Repository.installation_id != "")
        .all()
    )
    return {name for (name,) in rows if name}


def requeue_orphaned(*, grace_s: int = GRACE_S, limit: int = BATCH) -> dict:
    """Retry orphaned QUEUED tasks. Returns a summary; never raises."""
    import json
    from datetime import timedelta

    from app.db.database import SessionLocal, utcnow
    from app.db.models import Task, TaskEvent
    from app.tasks import queue as _queue

    q = _queue.get_queue()
    if q is None:
        return {"ok": False, "error": "redis unavailable", "requeued": 0}

    db = SessionLocal()
    try:
        cutoff = utcnow() - timedelta(seconds=max(int(grace_s), 0))
        rows = (
            db.query(Task)
            .filter(Task.status == "QUEUED", Task.created_at < cutoff)
            .order_by(Task.id.asc())
            .limit(max(int(limit), 1))
            .all()
        )
        if not rows:
            return {"ok": True, "checked": 0, "requeued": 0, "skipped": 0}
        connected = _connected_repo_names(db)
        requeued: list[int] = []
        skipped = 0
        failed = 0
        for task in rows:
            if task.cancel_requested or task.repository not in connected:
                skipped += 1
                continue
            job_id = f"fixhub-task-{task.id}"
            try:
                job = q.fetch_job(job_id)
                if job is not None:
                    status = str(job.get_status(refresh=True) or "").lower()
                    if status in _LIVE_JOB_STATES:
                        # A live job exists; the worker (or a pending tick) owns it.
                        skipped += 1
                        continue
                    # Terminal leftover: remove it so the canonical id is reusable.
                    job.delete()
            except Exception:
                # Redis hiccup: never risk double execution.
                skipped += 1
                continue
            out = _queue.enqueue_task(task.id)
            if out.get("enqueued"):
                task.queue_job_id = str(out.get("job_id") or job_id)[:128]
                task.error = ""
                db.add(
                    TaskEvent(
                        task_id=task.id,
                        type="QUEUE_RETRIED",
                        data_json=json.dumps(
                            {"job": task.queue_job_id, "reason": "queued_reconciler"}
                        )[:4000],
                    )
                )
                requeued.append(task.id)
            elif out.get("limited"):
                skipped += 1
            else:
                failed += 1
                task.error = (
                    f"not started: {str(out.get('error') or 'queue unavailable')[:300]}"
                )
            db.commit()
        return {
            "ok": True,
            "checked": len(rows),
            "requeued": len(requeued),
            "skipped": skipped,
            "failed": failed,
            "task_ids": requeued,
        }
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)[:300], "requeued": 0}
    finally:
        db.close()
