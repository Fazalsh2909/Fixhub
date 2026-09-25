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

import re
import threading

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
    {
        "REVIEWING",
        "READY_FOR_APPROVAL",
        "APPROVED",
        "BRANCH_CREATED",
        "COMMITTED",
        "PUSHED",
        "PR_CREATED",
    }
)

# States engineer_issue() can leave a task in when the server process dies# mid-run (dev restart, crash, OOM). None of these is ever a rest point —
# engineer_issue() always ends in FAILED / DEBUGGING / READY_FOR_APPROVAL —
# so their presence at startup means no background thread owns the task
# anymore. CREATED / DEBUGGING / FAILED / BLOCKED / CANCELLED are untouched:
# CREATED never started, the rest are legitimate points the operator (or a
# new trigger) resumes from with Run.
INTERRUPTED_STATES = frozenset(
    {
        "ANALYZING",
        "REPRODUCING",
        "ROOT_CAUSE_FOUND",
        "PLANNING",
        "IMPLEMENTING",
        "TESTING",
        "VERIFYING",
    }
)


def recover_interrupted_tasks(db: Session) -> int:
    """Mark runs that died with the server as FAILED (audited, re-runnable).

    Returns the number of tasks recovered. Each recovery is a legal
    FAILED transition with an explicit event, so the Agent Trace shows why
    the task never finished — instead of sitting in a mid-loop state forever.
    """
    from .agent.orchestrator import STATES, transition

    found = db.query(Task).filter(Task.state.in_(sorted(INTERRUPTED_STATES))).all()
    recovered = 0
    for task in found:
        if task.state not in STATES:
            continue
        try:
            transition(
                db,
                task,
                "FAILED",
                "run interrupted by server restart "
                "(background thread did not survive restart) — "
                "press Run to retry from a clean workspace",
            )
            recovered += 1
        except Exception:
            db.rollback()
    return recovered


class ApproveError(Exception):
    """Shared approve failure. Carries the HTTP status the endpoint should use."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_FALLBACK_TITLE_RE = re.compile(r"^issue #\d+$", re.IGNORECASE)
# "Fix #12: <url>" / "issue #3: ..." — the prefix adds junk words ("Fix",
# "#12") that defeat the word count below (live task 239 burned 25 agent
# steps on a URL-only instruction). Strip it before evaluating.
_FIX_PREFIX_RE = re.compile(r"^(fix|issue|fixes|closes?)\s+#\d+\s*:?\s*", re.IGNORECASE)


def is_thin_issue(title: str, body: str = "") -> bool:
    """True when an issue has no actionable content to spend LLM calls on.

    Thin = URL-only titles (live task 71), one-worders (tasks 117/122),
    our own "issue #N" fallback when the real title was never fetched, or
    a "Fix #N:"-prefixed URL with nothing actionable behind it (task 239).
    Healthy = 2+ words or 8+ meaningful chars after URLs are stripped.
    """
    text = _URL_RE.sub(" ", f"{title or ''} {body or ''}").strip()
    text = _FIX_PREFIX_RE.sub("", text).strip()
    if _FALLBACK_TITLE_RE.match(text):
        return True
    words = [w for w in re.split(r"\s+", text) if re.search(r"[A-Za-z0-9]", w)]
    chars = len(re.sub(r"\s+", "", text))
    return not (len(words) >= 2 or chars >= 8)


def mark_needs_info(db: Session, task: Task, reason: str) -> None:
    """Park a thin task as NEEDS_INFO (audited). Auto-launch skips it;
    explicit Run still works."""
    from .agent.orchestrator import transition

    transition(db, task, "NEEDS_INFO", reason)


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

    Verified pipeline (Phase 7/8/9): build the VerifiedArtifact from recorded
    evidence (refuses unless verdict is VERIFIED) → confirm the workspace
    still matches the artifact → policy gate → record approval → branch →
    commit → push → remote-SHA verify → PR. Each durable state is set only
    AFTER its operation succeeds. Failures land on the last good state,
    never on a faked forward state.
    """
    from pathlib import Path as _Path

    from .policy.engine import Decision, PolicyRequest, evaluate

    repo = db.query(Repository).filter_by(id=task.repo_id).first()
    if task.state not in ("REVIEWING", "READY_FOR_APPROVAL"):
        raise ApproveError(
            409, f"task is {task.state} — only REVIEWING tasks can be approved"
        )
    from .github.publisher import (
        PolicyDeniedError,
        PublishError,
        build_verified_artifact,
        verify_workspace_matches,
    )

    try:
        artifact = build_verified_artifact(db, task, repo)
    except PolicyDeniedError as e:
        raise ApproveError(409, str(e))
    branch = artifact.new_branch
    ok, why = verify_workspace_matches(task, artifact)
    if not ok:
        raise ApproveError(409, why)
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
    db.commit()
    approval = (
        db.query(Approval)
        .filter_by(task_id=task.id)
        .order_by(Approval.id.desc())
        .first()
    )
    artifact.approval_id = approval.id if approval else 0

    def _set_state(new: str, message: str) -> None:
        from .agent.orchestrator import STATES, TRANSITIONS

        assert new in STATES, f"unknown task state {new}"
        old = task.state or "CREATED"
        if (
            old in STATES
            and new not in TRANSITIONS.get(old, frozenset())
            and old != new
        ):
            raise ApproveError(500, f"illegal publish transition {old} → {new}")
        task.state = new
        db.add(
            TaskEvent(
                task_id=task.id,
                stage=new,
                message=message[:2000],
                prev_state=old,
                reason=f"approved by {approver}",
            )
        )
        try:
            from .memory.store import snapshot_task

            snapshot_task(db, task.repo_id, task.id, task.title, new, message[:300])
        except Exception:
            pass
        db.commit()

    _set_state(
        "APPROVED",
        f"approved by {approver}; branch={branch}; verification={artifact.verification_status}",
    )

    # No GitHub installation: real local commit when the isolated workspace
    # is a git repo, otherwise the decision is recorded locally (demo and
    # snapshot workspaces have no git history to commit to).
    if repo is None or not repo.installation_id:
        workdir = _Path(task.workspace_path) if task.workspace_path else None
        sha = ""
        if workdir is not None and workdir.is_dir() and (workdir / ".git").exists():
            try:
                from .github.publisher import commit_in_workspace

                sha = commit_in_workspace(
                    workdir, branch, f"{artifact.title}\n\n{artifact.body[:1500]}"
                )
            except PublishError as e:
                raise ApproveError(409, str(e))
        _set_state(
            "COMMITTED",
            f"approved by {approver}; branch={branch}; sha={sha or 'local-record'}",
        )
        log_event(logger, "task_approved_local", task_id=task.id, approver=approver)
        out: dict = {
            "status": "approved",
            "task_id": task.id,
            "branch": branch,
            "state": task.state,
            "note": "no GitHub installation — commit recorded locally",
        }
        if sha:
            out["commit_sha"] = sha
            out["note"] = "no GitHub installation — committed to local task branch"
        return out

    # Full pipeline: branch → commit → push → remote-SHA verify → PR.
    from .github.app_auth import get_installation_token
    from .github.publisher import (
        PRPublisher,
        commit_worktree,
        create_branch,
        push_branch,
        verify_remote_sha,
    )

    workdir = _Path(task.workspace_path) if task.workspace_path else None
    if workdir is None or not workdir.is_dir() or not (workdir / ".git").exists():
        raise ApproveError(
            409,
            "no git workspace for this task — re-run to provision one before publishing",
        )
    token = get_installation_token(repo.installation_id)
    remote_url = repo.clone_url or f"https://github.com/{repo.full_name}.git"
    try:
        create_branch(workdir, branch)
    except PublishError as e:
        raise ApproveError(409, str(e))
    _set_state(
        "BRANCH_CREATED",
        f"branch={branch} from base {artifact.base_sha[:8] if artifact.base_sha else 'snapshot'}",
    )
    try:
        sha = commit_worktree(workdir, f"{artifact.title}\n\n{artifact.body[:1500]}")
    except PublishError as e:
        raise ApproveError(502, f"branch created but commit failed: {e}")
    _set_state("COMMITTED", f"branch={branch}; sha={sha}")
    try:
        push_branch(workdir, remote_url, branch, token)
        remote_sha = verify_remote_sha(remote_url, branch, sha, token)
    except PublishError as e:
        # Commit exists locally — stay COMMITTED, do not fake PUSHED.
        db.add(
            TaskEvent(
                task_id=task.id,
                stage="PUSH_FAILED",
                message=str(e)[:2000],
                prev_state="COMMITTED",
                reason="push failed",
            )
        )
        db.commit()
        raise ApproveError(502, f"committed {sha[:8]} but push failed: {e}")
    _set_state("PUSHED", f"branch={branch}; remote sha={remote_sha}")
    try:
        pr = PRPublisher(token, default_branch=artifact.base_branch).publish(artifact)
    except Exception as e:
        # Pushed but no PR — stay PUSHED, do not fake PR_CREATED.
        db.add(
            TaskEvent(
                task_id=task.id,
                stage="PR_FAILED",
                message=f"pushed but PR creation failed: {e}"[:2000],
                prev_state="PUSHED",
                reason="pr failed",
            )
        )
        db.commit()
        raise ApproveError(502, f"pushed {remote_sha[:8]} but PR creation failed: {e}")
    db.add(
        PullRequest(
            task_id=task.id,
            url=pr.get("html_url", ""),
            number=pr.get("number", 0),
            commit_sha=remote_sha,
        )
    )
    _set_state("PR_CREATED", f"pr={pr.get('html_url', '')}; sha={remote_sha}")
    log_event(logger, "pr_created", task_id=task.id, approver=approver)
    return {
        "status": "pr_created",
        "task_id": task.id,
        "pr_url": pr.get("html_url", ""),
        "branch": branch,
        "commit_sha": remote_sha,
        "state": task.state,
    }


def _is_retryable_error(err: str | None) -> bool:
    """Provider 400/401/403/404 (bad key/permissions/unknown model) fail fast;
    429/5xx + verification FAIL retry."""
    if not err:
        return True
    low = err.lower()
    for code in (
        " 400",
        " 401",
        " 403",
        " 404",
        "provider 400",
        "provider 401",
        "provider 403",
        "provider 404",
    ):
        if code in low:
            return False
    if "unauthorized" in low or "forbidden" in low or "invalid api key" in low:
        return False
    if "not_found" in low or "not found" in low:
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
        # SIMPLE PATH (active): every run goes through run_issue()
        # (own session). The legacy state machine below is bypassed, not
        # deleted — it stays for history until the cleanup pass.
        if not force and task.state in ("COMPLETED", "BLOCKED"):
            return {"task_id": task.id, "state": task.state, "skipped": True}
        if not force and task.state == "NEEDS_INFO":
            # Parked thin issue: only an explicit Run (force=True) spends
            # agent budget on it.
            return {
                "task_id": task.id,
                "state": task.state,
                "skipped": True,
                "reason": "needs-info: describe the issue, then press Run",
            }
        if not force and task.state in TERMINAL_STATES:
            return {"task_id": task.id, "state": task.state, "skipped": True}
        from .agent.issue_worker import run_issue

        return run_issue(task_id)

        from .chat.router import resolve_workdir
        from .repo.workspaces import create_task_workspace

        repo = db.query(Repository).filter_by(id=task.repo_id).first()
        if repo is None:
            msg = "repo not found for task — connect or clone it first, then re-run"
            db.add(TaskEvent(task_id=task.id, stage="FAILED", message=msg))
            task.state = "FAILED"
            db.commit()
            return {"error": msg, "status_code": 400, "task_id": task.id}
        try:
            base = resolve_workdir(repo)
        except Exception:
            msg = (
                f"repo workspace not found for {repo.full_name} — clone it first "
                "(CLONE ANY OSS REPO / connect + clone), then re-run."
            )
            db.add(TaskEvent(task_id=task.id, stage="FAILED", message=msg))
            task.state = "FAILED"
            db.commit()
            return {"error": msg, "status_code": 400, "task_id": task.id}
        if not base.is_dir():
            name = repo.full_name if repo else "unknown"
            msg = (
                f"repo workspace not found for {name} — clone it first "
                "(CLONE ANY OSS REPO), then re-run"
            )
            db.add(TaskEvent(task_id=task.id, stage="FAILED", message=msg))
            task.state = "FAILED"
            db.commit()
            return {"error": msg, "status_code": 400, "task_id": task.id}

        # P0-1: every run provisions a FRESH isolated task workspace (detached
        # worktree at the base commit, or a snapshot copy). Retries never reuse
        # unknown dirty state — each attempt recreates a clean baseline.
        workdir, ws_sha = create_task_workspace(base, task.id)
        task.workspace_path = str(workdir)
        task.base_sha = ws_sha
        db.add(
            TaskEvent(
                task_id=task.id,
                stage="WORKSPACE_CREATED",
                message=f"isolated workspace {workdir} at base {ws_sha or 'snapshot'}",
            )
        )
        db.commit()

        from .agent.miniswe_adapter import miniswe_available
        from .agent.orchestrator import engineer_issue
        from .llm.openrouter import provider_from_settings
        from .verify.pipeline import build_proof, overall_status, run_verification

        _, api_key, _ = settings.resolved_llm()
        use_miniswe = bool(api_key and settings.miniswe_enabled and miniswe_available())
        if use_miniswe:
            # PRIMARY PATH: one mini-SWE-agent attempt (two at most, and only
            # for retryable provider/infrastructure errors — deterministic
            # verification outcomes never re-loop the agent).
            import time as _time

            from .agent.runner import run_miniswe_task

            _result: dict = {"verified": False, "results": []}
            _attempts = 0
            for _attempt in (1, 2):
                _attempts = _attempt
                if _attempt > 1:
                    workdir, ws_sha = create_task_workspace(base, task.id)
                    task.workspace_path = str(workdir)
                    task.base_sha = ws_sha
                    db.add(
                        TaskEvent(
                            task_id=task.id,
                            stage="WORKSPACE_CREATED",
                            message=(
                                f"retry attempt {_attempt}/2 after retryable "
                                f"error: clean baseline {workdir}"
                            ),
                        )
                    )
                    db.commit()
                _result = run_miniswe_task(db, task, workdir, repo)
                if not _result.get("retryable"):
                    break
                _err = _result.get("error") if isinstance(_result, dict) else None
                if not _is_retryable_error(_err if isinstance(_err, str) else None):
                    break
                if _attempt < 2:
                    db.add(
                        TaskEvent(
                            task_id=task.id,
                            stage="RETRYING",
                            message=f"retryable error ({(_err or '')[:200]}); retrying once",
                        )
                    )
                    db.commit()
                    _backoff = max(0.0, float(settings.agent_retry_backoff_s or 0.0))
                    if _backoff > 0:
                        _time.sleep(_backoff)
            record_task(bool(_result.get("verified")))
            out = {
                "task_id": task.id,
                "state": task.state,
                "attempts": _attempts,
                "max_attempts": 2,
                "mode": "mini-swe-agent",
                **_result,
            }
        elif not api_key:
            db.add(
                TaskEvent(
                    task_id=task.id,
                    stage="ANALYZING",
                    message="no LLM key — verification-only path",
                )
            )
            db.commit()
            results = run_verification(db, task, workdir)
            verdict = overall_status(results)
            verified = verdict == "VERIFIED"
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
            out = {
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
            # One call budget shared by every attempt (else each retry mints a
            # fresh 30). Threaded into engineer_issue + subagent turns.
            llm_state: dict = {"calls": 0}
            for attempt in range(1, max_attempts + 1):
                attempts = attempt
                if attempt > 1:
                    # Deterministic retry: clean baseline, clearly recorded.
                    workdir, ws_sha = create_task_workspace(base, task.id)
                    task.workspace_path = str(workdir)
                    task.base_sha = ws_sha
                    db.add(
                        TaskEvent(
                            task_id=task.id,
                            stage="WORKSPACE_CREATED",
                            message=(
                                f"retry attempt {attempt}/{max_attempts}: "
                                f"clean baseline {workdir} at {ws_sha or 'snapshot'}"
                            ),
                        )
                    )
                    db.commit()
                result = engineer_issue(
                    db, task, workdir, provider_from_settings(), llm_state
                )
                try:
                    from .repo.workspaces import git_diff_all

                    diff = (
                        git_diff_all(workdir)
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
                    if result.get("publishable", result.get("verified")):
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
                if result.get("publishable", result.get("verified")):
                    break
                if result.get("retryable", True) is False:
                    # Deterministic abort (breaker / pre-flight): retrying a
                    # fresh workspace cannot change the outcome — fail fast.
                    break
                err = result.get("error") if isinstance(result, dict) else None
                if err is None:
                    # Deterministic verification FAIL with no provider error:
                    # fail fast after attempt 1 instead of 2 more full loops.
                    break
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

        # Hands-free PR: publishable (VERIFIED or WITH_LIMITATIONS) + flag on
        # + installation attached. Proof documents any limitations.
        if settings.auto_pr_on_verified and out.get("publishable", out.get("verified")):
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


def try_acquire(task_id: int) -> bool:
    """Claim the run slot for a task. False when another run (background or
    synchronous) already holds it — the caller must refuse, never double-run.
    Pair with release() in a finally."""
    with _lock:
        if task_id in _runs:
            return False
        _runs[task_id] = "running"
        return True


def release(task_id: int) -> None:
    with _lock:
        _runs.pop(task_id, None)


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
        release(task_id)


def launch_task(task_id: int, force: bool = False) -> str:
    """Start a background agent run. Returns 'started', 'already-running',
    or 'needs-info' (thin issue parked — only force starts it).

    force=True re-runs even terminal (REVIEWING+) tasks — used for explicit
    user `run` requests. Default False skips already-finished work.
    """
    if not force:
        db: Session = SessionLocal()
        try:
            task = db.query(Task).filter_by(id=task_id).first()
            if task is not None and task.state == "NEEDS_INFO":
                return "needs-info"
        finally:
            db.close()
    if not try_acquire(task_id):
        return "already-running"
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
