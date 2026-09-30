"""RQ worker entrypoint: python -m app.tasks.worker.

Listens on QUEUE_NAME and runs run_task_job. One worker handles one job at a
time; scale with `docker compose up --scale worker=N`.
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
    # Ensure tables exist on the shared DB (idempotent; backend does the same).
    from app.db.database import Base, engine, ensure_columns

    Base.metadata.create_all(bind=engine)
    ensure_columns()
    with Connection(conn):
        q = Queue(settings.QUEUE_NAME)
        log.info("fixhub worker listening on queue=%s redis=%s", q.name, settings.REDIS_URL.split("@")[-1])
        worker: Worker = SimpleWorker([q]) if settings.ENV == "dev" else Worker([q])
        worker.work()


if __name__ == "__main__":
    main()
