"""Task queue with at-least-once delivery and crash recovery.

Redis is used in development/production. The in-memory backend remains available
for tests and demos, where process-level durability is not expected.

Production contract (Phase 15): dev/test may use memory, but a production
server must NOT silently fall back — /health/ready reports "degraded" and a
warning is logged at import when prod wants Redis and cannot reach it.

Duplicate-execution guards: every job is stamped with a unique job_id,
task_id, attempt number and enqueue timestamp. Workers de-duplicate by
(task_id, attempt): run_task_sync records an ATTEMPT event, and a second
delivery of the same attempt while the task is running is refused by the
launch_task/is_running guard; a re-delivery after completion is skipped for
terminal states unless force=True.

Known limitation (documented, not hidden): the memory backend is
process-local with no cross-process lease/visibility timeout. Two API
processes on one box could both dequeue logically-distinct jobs; the Redis
backend (brpoplpush + processing list + recover_processing) is the durable
path. SQS adapter plugs into the same enqueue/dequeue/ack interface via
QUEUE_BACKEND=sqs (see config.queue_backend).

Prod path (SQS): implement the same 4 functions (enqueue/dequeue/ack/
recover_processing) against SQS + DynamoDB lease table, select via
QUEUE_BACKEND=sqs. Interface is stable so the worker needs no change.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

try:
    import redis  # type: ignore[import]

    from .config import settings

    _r = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
    _r.ping()
    _BACKEND = "redis"
except Exception:  # no redis in demo/tests — fall back, never crash import
    _r = None  # type: ignore[assignment]
    _BACKEND = "memory"
    try:
        from .config import settings as _settings

        if _settings.is_prod:
            logging.getLogger("fixhub.queue").error(
                "production queue fallback: Redis unreachable at %s — "
                "using process-local memory queue (NOT durable). "
                "/health/ready reports degraded until Redis recovers.",
                _settings.redis_url,
            )
    except Exception:
        pass

_MEMORY: list[dict] = []
_QUEUE = "fixhub:tasks"
_PROCESSING = "fixhub:tasks:processing"


def enqueue(job: dict) -> str:
    stamped = {
        "job_id": job.get("job_id") or uuid.uuid4().hex,
        "attempt": int(job.get("attempt") or 1),
        "enqueued_at": job.get("enqueued_at") or time.time(),
        **{
            k: v
            for k, v in job.items()
            if k not in ("job_id", "attempt", "enqueued_at")
        },
    }
    if _BACKEND == "redis" and _r is not None:
        _r.rpush(_QUEUE, json.dumps(stamped))
    else:
        _MEMORY.append(stamped)
    return _BACKEND


def dequeue(timeout: int = 1) -> dict | None:
    if _BACKEND == "redis" and _r is not None:
        # Move the job into a processing list atomically. If a worker crashes
        # after dequeue, recover_processing() can requeue the unacknowledged job.
        blob = _r.brpoplpush(_QUEUE, _PROCESSING, timeout=timeout)
        if not blob:
            return None
        assert isinstance(blob, (str, bytes, bytearray)), "unexpected redis payload"
        return json.loads(blob)

    if _MEMORY:
        return _MEMORY.pop(0)
    return None


def ack(job: dict) -> None:
    """Acknowledge a successfully completed Redis job."""
    if _BACKEND == "redis" and _r is not None:
        _r.lrem(_PROCESSING, 1, json.dumps(job))


def recover_processing() -> int:
    """Requeue jobs left in processing after a worker restart."""
    if _BACKEND != "redis" or _r is None:
        return 0

    recovered = 0
    while True:
        blob = _r.rpoplpush(_PROCESSING, _QUEUE)
        if not blob:
            break
        recovered += 1
    return recovered


def backend() -> str:
    return _BACKEND
