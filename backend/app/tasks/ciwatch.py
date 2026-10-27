"""Post-publish CI watcher: COMPLETED only when GitHub checks pass.

Tasks whose PR was published enter AWAITING_CI. This watcher (triggered by a
60s in-process timer in the backend, or manually via POST /api/cron/ci-watch)
checks the pushed commit and either completes the task or enqueues a repair
job on the SAME task/branch/PR — never a second PR. Max 3 repair attempts;
then FAILED with the preserved failure. Never raises out of check_awaiting_ci.
"""

from __future__ import annotations

from app.config import settings

MAX_CI_REPAIRS = 3


def check_awaiting_ci() -> dict:
    """One watch pass over AWAITING_CI tasks. Returns outcome counts."""
    from app.db.database import SessionLocal
    from app.db.models import Task

    out = {
        "checked": 0,
        "completed": 0,
        "repair_enqueued": 0,
        "failed": 0,
        "pending": 0,
    }
    db = SessionLocal()
    try:
        tasks = db.query(Task).filter(Task.status == "AWAITING_CI").all()
        for t in tasks:
            try:
                res = _check_one(db, t)
                out["checked"] += 1
                out[res] = out.get(res, 0) + 1
            except Exception:
                db.rollback()
    finally:
        db.close()
    return out


def _repo_token(db, repository: str) -> str:
    try:
        from app.db.models import Repository
        from app.github import app_auth as _app_auth

        row = (
            db.query(Repository)
            .filter(Repository.github_full_name == repository)
            .first()
        )
        installation_id = row.installation_id if row else ""
        if not installation_id:
            return ""
        return _app_auth.installation_token(installation_id)
    except Exception:
        return ""


def _unobservable(db, task, conclusion: str) -> str:
    """Bounded patience for a check that cannot be observed. Returns pending|failed.

    `unknown` (no check reported) and observation failures (missing token,
    API error) are counted; `pending` (checks exist but are still running) is
    NOT, so a legitimately long CI job is never failed as "unobservable".
    """
    import json as _json

    from app.db.models import TaskEvent

    max_ticks = max(1, int(getattr(settings, "CI_UNOBSERVABLE_MAX_TICKS", 30)))
    task.ci_unobservable_ticks = int(task.ci_unobservable_ticks or 0) + 1
    if task.ci_unobservable_ticks >= max_ticks:
        task.status = "FAILED"
        task.error = (
            "CI result could not be observed after "
            f"{max_ticks} checks (conclusion={conclusion or 'unknown'}); "
            "verify GitHub App checks:read permissions or the run URL"
        )[:1000]
        from app.tasks.service import _release_active_keys

        _release_active_keys(task)
        db.add(
            TaskEvent(
                task_id=task.id,
                type="CI_UNOBSERVABLE",
                data_json=_json.dumps(
                    {"ticks": task.ci_unobservable_ticks, "conclusion": conclusion}
                )[:4000],
            )
        )
        db.commit()
        return "failed"
    db.commit()
    return "pending"


def _check_one(db, task) -> str:
    """Returns one of: completed | repair_enqueued | failed | pending."""
    import json as _json

    from app.db.models import TaskEvent
    from app.github import client as _gh

    token = _repo_token(db, task.repository)
    if not token or not task.commit_sha:
        return _unobservable(db, task, "unavailable")
    try:
        conclusion = _gh.sha_check_conclusion(
            token=token, full_name=task.repository, sha=task.commit_sha
        )
    except Exception:
        return _unobservable(db, task, "api_error")
    if conclusion == "success":
        task.status = "COMPLETED"
        task.error = ""
        task.ci_unobservable_ticks = 0
        from app.tasks.service import _release_active_keys

        _release_active_keys(task)
        db.add(
            TaskEvent(
                task_id=task.id,
                type="CI_PASSED",
                data_json=_json.dumps({"sha": task.commit_sha})[:4000],
            )
        )
        db.commit()
        return "completed"
    if conclusion == "pending":
        # Checks exist and are still running: observation works, keep waiting.
        task.ci_unobservable_ticks = 0
        db.commit()
        return "pending"
    if conclusion != "failure":
        # Permissions/outage can leave the check unobservable. Never wait
        # forever: after a bounded patience window, fail honestly.
        return _unobservable(db, task, conclusion)
    # Failure: fetch the tail for the repair round (best-effort). Use the
    # exact identified job when known; empty lets the client pick the actual
    # failed job instead of guessing the workflow name.
    try:
        tail = _gh.failed_log_tail(
            token=token,
            full_name=task.repository,
            run_id=task.ci_run_id,
            job_name=task.ci_job or "",
        )
    except Exception:
        tail = ""
    attempt = (task.ci_attempt_count or 0) + 1
    if attempt > MAX_CI_REPAIRS:
        task.status = "FAILED"
        task.error = (
            f"CI failed after {MAX_CI_REPAIRS} repairs "
            f"on {task.pr_url or task.branch}: {(tail or task.last_ci_failure or '')[:800]}"
        )[:1000]
        task.last_ci_failure = tail[:6000] if tail else task.last_ci_failure
        from app.tasks.service import _release_active_keys

        _release_active_keys(task)
        db.add(
            TaskEvent(
                task_id=task.id,
                type="CI_FAILED",
                data_json=_json.dumps({"attempt": attempt, "final": True})[:4000],
            )
        )
        db.commit()
        return "failed"
    task.ci_attempt_count = attempt
    if tail:
        task.last_ci_failure = tail[:6000]
    # Phase 4: repair re-enters through QUEUED + atomic claim (never direct
    # RUNNING). The CAS below also serializes twin watcher ticks: only one
    # moves AWAITING_CI -> QUEUED and enqueues the repair job.
    from app.tasks.service import cas_status

    if not cas_status(
        db,
        task.id,
        {"AWAITING_CI"},
        "QUEUED",
        ci_attempt_count=attempt,
        last_ci_failure=task.last_ci_failure,
        # A new repair round observes a new revision: old "unobservable"
        # ticks must not make the fresh revision fail prematurely.
        ci_unobservable_ticks=0,
    ):
        db.rollback()
        return "pending"  # another tick claimed it first
    db.add(
        TaskEvent(
            task_id=task.id,
            type="CI_FAILED",
            data_json=_json.dumps({"attempt": attempt, "sha": task.commit_sha})[:4000],
        )
    )
    db.add(
        TaskEvent(
            task_id=task.id,
            type="CI_REPAIR_STARTED",
            data_json=_json.dumps({"attempt": attempt, "of": MAX_CI_REPAIRS})[:4000],
        )
    )
    db.commit()
    try:
        from app.tasks import queue as _queue

        out = _queue.enqueue_repair(task.id)
        if out.get("enqueued"):
            return "repair_enqueued"
        task.status = "AWAITING_CI"  # redis down: stay watchable, retry next tick
        db.commit()
    except Exception:
        try:
            task.status = "AWAITING_CI"
            db.commit()
        except Exception:
            pass
    return "pending"
