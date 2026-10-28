#!/usr/bin/env python3
"""Report whether every RQ worker on the FixHub queue is idle.

Exit codes:
    0  all workers idle — safe to restart fixhub-worker
    1  at least one worker is executing a job — the caller waits or fails
    2  worker state could not be determined — the caller MUST fail closed

Why this exists: RQ workers finish their current job on SIGTERM (warm
shutdown), but systemd's TimeoutStopSec may SIGKILL a long task anyway
(JOB_TIMEOUT_S can be hours). A deployment therefore polls this check and
waits for the running job instead of relying on the unit's stop timeout.

Run on the server from the backend directory so app.config resolves .env:

    cd /home/ubuntu/Fixhub/backend
    /home/ubuntu/.venvs/fixhub312/bin/python ../infra/deploy/drain.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"


def _live_workers(redis_url: str):
    """Connect to Redis and return (workers, connection). Real rq only."""
    from redis import Redis
    from rq import Worker

    conn = Redis.from_url(redis_url)
    return Worker.all(conn), conn


def _collect_workers():
    """Load settings and return the live RQ workers (patch point for tests)."""
    sys.path.insert(0, str(BACKEND_DIR))
    from app.config import settings

    return _live_workers(settings.REDIS_URL)


def current_job(worker):
    """Return the job a worker is executing, or None (rq-version tolerant)."""
    getter = getattr(worker, "get_current_job", None)
    if getter is None:
        return None
    try:
        return getter()
    except TypeError:
        connection = getattr(worker, "connection", None)
        if connection is None:
            return None
        return getter(connection)


def worker_is_idle(workers) -> bool:
    """True when none of the given workers is executing a job."""
    for worker in workers:
        if current_job(worker) is not None:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)

    try:
        workers = _collect_workers()
    except Exception as exc:  # noqa: BLE001 — fail closed on any state error
        print(f"drain: cannot determine worker state: {exc}", file=sys.stderr)
        return 2

    busy = [
        getattr(worker, "name", str(worker))
        for worker in workers
        if current_job(worker) is not None
    ]
    if busy:
        print(f"drain: busy workers: {', '.join(busy)}")
        return 1
    print("drain: all workers idle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
