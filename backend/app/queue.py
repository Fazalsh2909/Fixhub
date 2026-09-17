"""Task queue: Redis in dev/prod, in-memory fallback for tests/demo.

Jobs are tiny dicts: {"task_id": int, "repo": str, "issue": int}.
At-least-once delivery; consumers must be idempotent (see webhook gateway).
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
QUEUE_KEY = "fixhub:tasks"


def enqueue(job: dict) -> str:
    if _BACKEND == "redis" and _r is not None:
        _r.rpush(QUEUE_KEY, json.dumps(job))
    else:
        _MEMORY.append(job)
    return _BACKEND


def dequeue(timeout: int = 1) -> dict | None:
    if _BACKEND == "redis" and _r is not None:
        # redis-py stubs conflate sync/async blpop overloads; unwrap defensively.
        item = _r.blpop(QUEUE_KEY, timeout=timeout)  # type: ignore[arg-type]
        if not item:
            return None
        blob = item[1] if isinstance(item, (list, tuple)) else item
        assert isinstance(blob, (str, bytes, bytearray)), "unexpected redis payload"
        return json.loads(blob)
    if _MEMORY:
        return _MEMORY.pop(0)
    return None


def backend() -> str:
    return _BACKEND
