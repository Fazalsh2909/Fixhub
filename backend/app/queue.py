"""Task queue with at-least-once delivery and crash recovery.

Redis is used in development/production. The in-memory backend remains available
for tests and demos, where process-level durability is not expected.
"""

from __future__ import annotations

import json

try:
    import redis  # type: ignore[import]

    from .config import settings

    _r = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
    _r.ping()
    _BACKEND = "redis"
except Exception:  # no redis in demo/tests — fall back, never crash import
    _r = None  # type: ignore[assignment]
    _BACKEND = "memory"

_MEMORY: list[dict] = []
_QUEUE = "fixhub:tasks"
_PROCESSING = "fixhub:tasks:processing"


def enqueue(job: dict) -> str:
    if _BACKEND == "redis" and _r is not None:
        _r.rpush(_QUEUE, json.dumps(job))
    else:
        _MEMORY.append(job)
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
