"""RQ worker entrypoint: python -m app.tasks.worker.

Listens on QUEUE_NAME and runs run_task_job. One worker handles one job at a
time; scale with `docker compose up --scale worker=N` (Phase 4: the DB claim
guard keeps multi-worker execution safe).

Shutdown: RQ stops after the current job on SIGTERM; hung jobs die at
JOB_TIMEOUT_S via on_failure -> FAILED. A SIGKILLed worker's RUNNING task
holds a lease (TASK_LEASE_S); the recovery sweep re-queues it on expiry, and
the next claim reuses the same task branch (never a second branch/PR).
"""

from __future__ import annotations

import logging

logging.basicConfig(level="INFO")
log = logging.getLogger("fixhub.worker")


def main() -> None:
    from redis import Redis
    from rq import Connection, Queue, SimpleWorker, Worker

    from app.config import settings

    conn = Redis.from_url(settings.REDIS_URL)
    conn.ping()
    # Gate 0: Alembic is authoritative; in prod a failed migration raises
    # MigrationFailed and stops the worker (no create_all continuation).
    # Production never calls create_all/ensure_columns (Alembic only).
    from app.db import migrate as _migrate
    from app.db.database import Base, engine, ensure_columns

    _migrate.upgrade_head()
    if settings.ENV.strip().lower() != "prod":
        Base.metadata.create_all(bind=engine)
        ensure_columns()
    # Gate 0 (P0-2): reap crashed-worker microVMs from the sandbox host's
    # worker process (same host as the jail dirs). The API tier never reaps:
    # only this host's manager may destroy this host's VMs, and only with a
    # provably dead owner (host + pid + stale heartbeat + no live DB lease).
    try:
        from app.sandbox import firecracker as _fc

        cleaned = _fc.destroy_orphans()
        if cleaned:
            log.info("reclaimed %d orphan microVM(s) at worker startup", cleaned)
    except Exception as exc:
        log.warning("orphan microVM sweep failed (non-fatal): %s", exc)
    with Connection(conn):
        q = Queue(settings.QUEUE_NAME)
        log.info(
            "fixhub worker listening on queue=%s redis=%s",
            q.name,
            settings.REDIS_URL.split("@")[-1],
        )
        worker: Worker = SimpleWorker([q]) if settings.ENV == "dev" else Worker([q])
        worker.work()


if __name__ == "__main__":
    main()
