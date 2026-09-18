"""Durable worker for autonomous Fixhub tasks."""

from __future__ import annotations

import logging

from ..automation import run_task_sync
from ..db import init_db
from ..queue import ack, backend, dequeue, recover_processing

log = logging.getLogger("fixhub.worker")


def main() -> None:
    init_db()

    recovered = recover_processing()
    if recovered:
        log.warning("requeued %d task(s) left by a previous worker", recovered)

    log.info("fixhub worker started (backend=%s)", backend())

    while True:
        job = dequeue(timeout=5)
        if job is None:
            continue

        task_id = int(job["task_id"])
        try:
            result = run_task_sync(task_id, force=False)
            log.info("task %s completed: %s", task_id, result)
            ack(job)
        except Exception:
            # Leave the Redis job in the processing list. A worker restart will
            # recover it instead of silently losing the task.
            log.exception("task %s failed; leaving job for recovery", task_id)


if __name__ == "__main__":
    main()
