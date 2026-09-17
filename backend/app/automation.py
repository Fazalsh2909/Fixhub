"""Automation core: background agent runs + hands-free verified PRs.

Two pre-authorized behaviors (env-gated):
- AUTO_RUN: task creation immediately launches the agent in a daemon
  thread; the frontend polls the Agent Trace for transparency.
- AUTO_PR_ON_VERIFIED: a VERIFIED run opens a PR without a manual click.

Safety holds in both modes: verified-only, real non-empty diff, policy
ALLOW, never the default branch. Every auto approval is recorded as an
AUTO_APPROVED approval row — "nothing reaches GitHub without an Approval
row" stays literally true.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .logging import get_logger, log_event
from .models import Approval, Patch, PullRequest, Repository, Task, TaskEvent

logger = get_logger("fixhub.automation")

# task_id -> "running" for background runs (prevents double-run pileups).
_runs: dict[int, str] = {}
_lock = threading.Lock()

TERMINAL_STATES = frozenset(
    {"REVIEWING", "READY_FOR_APPROVAL", "COMMITTED", "PUSHED", "PR_CREATED"}
)


class ApproveError(Exception):
    """Shared approve failure. Carries the HTTP status the endpoint should use."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def task_branch(task: Task, patch: Patch | None) -> str:
    """Branch for a task. Custom (issue 0) tasks get a per-task branch."""
    if patch and patch.branch:
        return patch.branch
    if task.issue_number:
        return f"fixhub/issue-{task.issue_number}"
    return f"fixhub/task-{task.id}"


def approve_task(
    db: Session,
    task: Task,
    approver: str = "dev",
    decision: str = "APPROVED",
    reason: str = "",
) -> dict:
    """Approve + commit + publish PR. Shared by the review endpoint and auto-PR.

    Raises ApproveError(status_code, detail) on any refusal — the endpoint
    maps these to HTTP codes, the auto path records them as task events.
    """
    from .policy.engine import Decision, PolicyRequest, evaluate

    repo = db.query(Repository).filter_by(id=task.repo_id).first()
    if task.state not in ("REVIEWING", "READY_FOR_APPROVAL"):
        raise ApproveError(
            409, f"task is {task.state} — only REVIEWING tasks can be approved"
        )
    patch = db.query(Patch).filter_by(task_id=task.id).order_by(Patch.id.desc()).first()
    if (
        patch is None
        or not patch.diff.strip()
        or patch.diff.strip() == "(no files changed)"
    ):
        raise ApproveError(409, "no verified diff to commit yet")
    branch = task_branch(task, patch)
    if (
        evaluate(PolicyRequest(action="CREATE_BRANCH", task_id=task.id, branch=branch))
        == Decision.DENY
    ):
        raise ApproveError(403, "branch denied by policy")
    if (
        evaluate(PolicyRequest(action="CREATE_PR", task_id=task.id, branch=branch))
        == Decision.DENY
    ):
        raise ApproveError(403, "PR denied by policy")

    db.add(
        Approval(
            task_id=task.id,
            decision=decision,
            approver=approver[:255],
            reason=reason[:2000],
        )
    )
    db.add(
        TaskEvent(
            task_id=task.id,
            stage="COMMITTED",
            message=f"approved by {approver}; branch={branch}",
        )
    )
    task.state = "COMMITTED"
    try:
        from .memory.store import snapshot_task

        snapshot_task(
            db,
            task.repo_id,
            task.id,
            task.title,
            "COMMITTED",
            f"approved by {approver}",
        )
    except Exception:
        pass
    db.commit()

    # Push + open PR only with a GitHub installation; otherwise the commit
    # decision is recorded locally (demo/cloned repos without App token).
    if repo is not None and repo.installation_id:
        try:
            from .github.app_auth import get_installation_token
            from .github.publisher import PRPublisher, VerifiedArtifact

            token = get_installation_token(repo.installation_id)
            artifact = VerifiedArtifact(
                repo_full_name=repo.full_name,
                base_branch=repo.default_branch or "main",
                new_branch=branch,
                patch_diff=patch.diff,
                title=f"Fix #{task.issue_number}: {task.title}"
                if task.issue_number
                else task.title,
                body=(
                    f"Proof of Fix for #{task.issue_number}\n\nApproved by {approver}.\n"
                    if task.issue_number
                    else f"Approved by {approver}.\n"
                ),
                proof_passed=True,
            )
            pr = PRPublisher(token).publish(artifact)
            db.add(
                PullRequest(
                    task_id=task.id,
                    url=pr.get("html_url", ""),
                    number=pr.get("number", 0),
                )
            )
            task.state = "PR_CREATED"
            db.add(
                TaskEvent(
                    task_id=task.id,
                    stage="PR_CREATED",
                    message=f"pr={pr.get('html_url', '')}",
                )
            )
            try:
                from .memory.store import snapshot_task as _snap

                _snap(db, task.repo_id, task.id, task.title, "PR_CREATED", "pr opened")
            except Exception:
                pass
            db.commit()
            log_event(logger, "pr_created", task_id=task.id, approver=approver)
            return {
                "status": "pr_created",
                "task_id": task.id,
                "pr_url": pr.get("html_url", ""),
                "branch": branch,
                "state": task.state,
            }
        except Exception as e:
            db.add(
                TaskEvent(
                    task_id=task.id,
                    stage="REVIEWING",
                    message=f"publish failed, still approved: {e}",
                )
            )
            task.state = "REVIEWING"
            db.commit()
            raise ApproveError(502, f"approved but publish failed: {e}")
    log_event(logger, "task_approved_local", task_id=task.id, approver=approver)
    return {
        "status": "approved",
        "task_id": task.id,
        "branch": branch,
        "state": task.state,
        "note": "no GitHub installation — commit recorded locally",
    }


def _is_retryable_error(err: str | None) -> bool:
    """Provider 400/401/403 (bad key/permissions) fail fast; 429/5xx + verification FAIL retry."""
    if not err:
        return True
    low = err.lower()
    for code in (
        " 400",
        " 401",
        " 403",
        "provider 400",
        "provider 401",
        "provider 403",
    ):
        if code in low:
            return False
    if "unauthorized" in low or "forbidden" in low or "invalid api key" in low:
        return False
    return True


def run_task_sync(task_id: int, force: bool = False) -> dict:
    """Run the full agent pipeline for a task. Own DB session (thread-safe).

    Retries failed attempts up to 1 + settings.agent_max_retries (default 3
    total): verification FAIL (DEBUGGING) and retryable provider errors
    (429/5xx) retry with backoff; 400/401/403 fail fast. Every attempt is a
    TaskEvent so the Agent Trace shows what happened.

    force=False (background auto-runs) skips tasks already past the running
    states so duplicate triggers don't redo work. force=True (explicit Run
    button / API call) always runs.
    """
    from .metrics import record_task

    db: Session = SessionLocal()
    try:
        task = db.query(Task).filter_by(id=task_id).first()
        if not task:
            return {"error": "task not found", "status_code": 404, "task_id": task_id}
        if not force and task.state in TERMINAL_STATES:
            return {"task_id": task.id, "state": task.state, "skipped": True}

        from .chat.router import resolve_workdir

        repo = db.query(Repository).filter_by(id=task.repo_id).first()
        workdir = resolve_workdir(repo) if repo else Path(".")
        if not workdir.is_dir():
            name = repo.full_name if repo else "unknown"
            msg = (
                f"repo workspace not found for {name} — clone it first "
                "(CLONE ANY OSS REPO), then re-run"
            )
            db.add(TaskEvent(task_id=task.id, stage="FAILED", message=msg))
            task.state = "FAILED"
            db.commit()
            return {"error": msg, "status_code": 400, "task_id": task.id}

        from .agent.orchestrator import engineer_issue
        from .llm.openrouter import provider_from_settings
        from .verify.pipeline import build_proof, run_verification

        _, api_key, _ = settings.resolved_llm()
        if not api_key:
            db.add(
                TaskEvent(
                    task_id=task.id,
                    stage="ANALYZING",
                    message="no LLM key — verification-only path",
                )
            )
            db.commit()
            results = run_verification(db, task, workdir)
            verified = all(ok for _, ok in results)
            db.add(Patch(task_id=task.id, diff="(no files changed)", branch=""))
            task.state = "REVIEWING" if verified else "DEBUGGING"
            try:
                from .memory.store import snapshot_task

                snapshot_task(
                    db,
                    task.repo_id,
                    task.id,
                    task.title,
                    task.state,
                    "verification-only run",
                )
            except Exception:
                pass
            db.commit()
            record_task(verified)
            proof = build_proof(
                task,
                results,
                "(no files changed)",
                "regression run",
                "PASS" if verified else "FAIL",
            )
            out: dict = {
                "task_id": task.id,
                "verified": verified,
                "proof": proof,
                "mode": "verification-only (no LLM key)",
                "state": task.state,
            }
        else:
            import time

            max_attempts = max(1, 1 + int(settings.agent_max_retries or 0))
            backoff = max(0.0, float(settings.agent_retry_backoff_s or 0.0))
            result: dict = {"verified": False, "results": []}
            attempts = 0
            for attempt in range(1, max_attempts + 1):
                attempts = attempt
                if attempt > 1:
                    db.add(
                        TaskEvent(
                            task_id=task.id,
                            stage="ANALYZING",
                            message=f"retry attempt {attempt}/{max_attempts}",
                        )
                    )
                    db.commit()
                result = engineer_issue(db, task, workdir, provider_from_settings())
                try:
                    from .repo.workspace import git_diff

                    diff = (
                        git_diff(workdir)
                        if (workdir / ".git").exists()
                        else "(no git repo — see TOOL edit events)"
                    )
                    db.add(
                        Patch(
                            task_id=task.id,
                            diff=diff[-20000:],
                            branch=task_branch(task, None),
                        )
                    )
                    if result.get("verified"):
                        task.state = "REVIEWING"
                    try:
                        from .memory.store import snapshot_task

                        snapshot_task(
                            db,
                            task.repo_id,
                            task.id,
                            task.title,
                            task.state,
                            f"agent run done (attempt {attempt}/{max_attempts})",
                        )
                    except Exception:
                        pass
                    db.commit()
                except Exception:
                    pass
                if result.get("verified"):
                    break
                err = result.get("error") if isinstance(result, dict) else None
                if not _is_retryable_error(err if isinstance(err, str) else None):
                    break
                if attempt < max_attempts:
                    db.add(
                        TaskEvent(
                            task_id=task.id,
                            stage="RETRYING",
                            message=(
                                f"attempt {attempt} failed "
                                f"({(err or 'verification FAIL')[:300]}); "
                                f"retrying in {backoff * attempt:.0f}s"
                            ),
                        )
                    )
                    db.commit()
                    if backoff > 0:
                        time.sleep(backoff * attempt)
            record_task(bool(result.get("verified")))
            out = {
                "task_id": task.id,
                "state": task.state,
                "attempts": attempts,
                "max_attempts": max_attempts,
                **result,
            }

        # Hands-free PR: verified + flag on + installation attached.
        if settings.auto_pr_on_verified and out.get("verified"):
            fresh = db.query(Task).filter_by(id=task_id).first()
            if fresh is not None and fresh.state in ("REVIEWING", "READY_FOR_APPROVAL"):
                try:
                    pub = approve_task(
                        db,
                        fresh,
                        approver="auto",
                        decision="AUTO_APPROVED",
                        reason="auto-pr on verified",
                    )
                    out.update(
                        {
                            "auto_pr": pub.get("status"),
                            "pr_url": pub.get("pr_url", ""),
                            "state": pub.get("state", fresh.state),
                        }
                    )
                except ApproveError as e:
                    db.add(
                        TaskEvent(
                            task_id=task_id,
                            stage="REVIEWING",
                            message=f"auto-pr skipped: {e.detail}",
                        )
                    )
                    db.query(Task).filter_by(id=task_id).update({"state": "REVIEWING"})
                    db.commit()
                    out["auto_pr"] = f"skipped: {e.detail}"
                    out["state"] = "REVIEWING"
        return out
    finally:
        db.close()


def is_running(task_id: int) -> bool:
    with _lock:
        return task_id in _runs


def _thread_main(task_id: int, force: bool = False) -> None:
    try:
        run_task_sync(task_id, force=force)
    except Exception as e:  # background runs must never die silently
        db: Session = SessionLocal()
        try:
            t = db.query(Task).filter_by(id=task_id).first()
            if t is not None:
                db.add(
                    TaskEvent(
                        task_id=task_id,
                        stage="FAILED",
                        message=f"background run crashed: {e}",
                    )
                )
                t.state = "FAILED"
                db.commit()
            log_event(logger, "background_run_crash", task_id=task_id, error=str(e))
        except Exception:
            pass
        finally:
            db.close()
    finally:
        with _lock:
            _runs.pop(task_id, None)


def launch_task(task_id: int, force: bool = False) -> str:
    """Start a background agent run. Returns 'started' or 'already-running'.

    force=True re-runs even terminal (REVIEWING+) tasks — used for explicit
    user `run` requests. Default False skips already-finished work.
    """
    with _lock:
        if task_id in _runs:
            return "already-running"
        _runs[task_id] = "running"
    threading.Thread(target=_thread_main, args=(task_id, force), daemon=True).start()
    log_event(logger, "task_launched", task_id=task_id)
    return "started"


router = APIRouter(prefix="/api", tags=["automation"])


@router.get("/automation")
def automation_status() -> dict:
    """Safe automation + provider status for the UI header (no secrets)."""
    from .github.app_auth import app_configured

    _, api_key, model = settings.resolved_llm()
    return {
        "auto_run": settings.auto_run,
        "auto_pr_on_verified": settings.auto_pr_on_verified,
        "auto_trigger_on_issue": settings.auto_trigger_on_issue,
        "llm_configured": bool(api_key),
        "provider": settings.llm_provider,
        "model": model,
        "app_configured": app_configured(),
    }
