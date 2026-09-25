"""Simple worker: issue run waiting → execute agent → publish.

Job payload: {"run_id": 123, "repository": "owner/repo", "issue_number": 123}
(task_id kept as back-compat alias for run_id.)
"""

from __future__ import annotations

import logging

from ..db import init_db
from ..queue import ack, backend, dequeue, recover_processing

log = logging.getLogger("fixhub.worker")


def handle_job(job: dict) -> dict:
    """One job → one issue run. Simple path first, legacy fallback."""
    run_id = int(job.get("run_id") or job.get("task_id"))
    from .issue_worker import run_issue

    try:
        return run_issue(run_id)
    except Exception:
        log.exception("simple run_issue failed for %s; trying legacy path", run_id)
        from ..automation import run_task_sync

        return run_task_sync(run_id, force=False)


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

        task_id = int(job.get("run_id") or job.get("task_id"))
        try:
            result = handle_job(job)
            log.info("task %s completed: %s", task_id, result)
            ack(job)
        except Exception:
            # Leave the Redis job in the processing list. A worker restart will
            # recover it instead of silently losing the task.
            log.exception("task %s failed; leaving job for recovery", task_id)


if __name__ == "__main__":
    main()
