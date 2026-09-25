"""Simple issue-fixing worker: the ONLY autonomous fixing path.

Flow:
    load issue + repo → create isolated workspace → retrieve ≤8 memories
    → build simple prompt → run mini-SWE-agent → git status/diff --check
    → save memory → publish (diff → branch → commit → push → PR)

No stages, no verification gates, no debugging loop, no retries.
Final states only: RUNNING → COMPLETED / FAILED / BLOCKED.
"No changes" is COMPLETED with a note (no meaningless PR).

The legacy Task table is reused as a simple execution record; legacy
state-machine columns are left untouched and never consulted.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..logging import get_logger, log_event
from ..models import Task, TaskEvent

logger = get_logger("fixhub.issue_worker")

RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
BLOCKED = "BLOCKED"

_SIMPLE_STATES = frozenset({RUNNING, COMPLETED, FAILED, BLOCKED})


def _event(db: Session, task_id: int, stage: str, message: str) -> None:
    db.add(TaskEvent(task_id=task_id, stage=stage, message=message[:2000]))
    db.commit()


def _set_state(db: Session, task: Task, state: str, message: str = "") -> None:
    assert state in _SIMPLE_STATES, f"unknown simple state {state}"
    prev = task.state or ""
    task.state = state
    db.add(
        TaskEvent(
            task_id=task.id,
            stage=state,
            message=message[:2000],
            prev_state=prev[:32],
            reason=message[:1024],
        )
    )
    db.commit()
    log_event(logger, "issue_state", task_id=task.id, stage=state)


def _intel_summary(workdir: Path, title: str, limit: int = 12) -> str:
    """Compact file:line hints for issue keywords. Best-effort, never fails."""
    try:
        from ..intel.indexer import search_code
    except Exception:
        return ""
    tokens = [t for t in re.split(r"\W+", title or "") if len(t) > 3][:3]
    seen: list[str] = []
    for tok in tokens:
        try:
            hits = search_code(workdir, re.escape(tok))[:20]
        except Exception:
            continue
        for h in hits:
            try:
                ref = f"{h.get('file')}:{h.get('line')}"
            except Exception:
                continue
            if ref not in seen:
                seen.append(ref)
            if len(seen) >= limit:
                return "; ".join(seen)
    return "; ".join(seen)


def _memory_facts(db: Session, repo_id: int, title: str, limit: int = 8) -> list[str]:
    try:
        from ..memory.store import retrieve

        mems = retrieve(db, repo_id, title, limit=limit)
    except Exception:
        return []
    return [f"[{m.type}] {m.fact[:300]}" for m in mems]


def _enrich_context(db: Session, task: Task, repo, issue_body: str) -> str:
    """Fetch real content behind issue references (fail-open, capped)."""
    try:
        from ..github.enrich import enrich_issue_context

        return enrich_issue_context(
            repo_full_name=getattr(repo, "full_name", "") or "",
            issue_number=task.issue_number or 0,
            title=task.title or "",
            body=issue_body or "",
            installation_id=getattr(repo, "installation_id", "") or "",
        )
    except Exception:
        return ""


def _followup_context(db: Session, task: Task) -> str:
    """Prior clarifying question + context for follow-up runs on one issue.

    When a human replies to the agent's ask-back comment, the new run sees
    the question that prompted the reply (its own TaskEvent row carries the
    comment text).
    """
    try:
        rows = (
            db.query(TaskEvent)
            .join(Task, Task.id == TaskEvent.task_id)
            .filter(
                Task.repo_id == task.repo_id,
                Task.issue_number == task.issue_number,
                TaskEvent.task_id != task.id,
                TaskEvent.stage == "COMMENT_POSTED",
            )
            .order_by(TaskEvent.id.desc())
            .limit(1)
            .all()
        )
    except Exception:
        return ""
    if not rows:
        return ""
    return (rows[0].message or "")[:800]


def _sanity_check(workdir: Path) -> tuple[bool, str, str]:
    """Very small post-run check: git status + diff --check. No gates.

    Returns (ok, diff, reason). ok=False means 'invalid diff' — reported,
    never retried or sent to debugging.
    """
    try:
        from ..repo.workspaces import git_diff_all, is_git_repo
    except Exception:
        return False, "", "workspace helpers unavailable"
    if not workdir.is_dir():
        return False, "", "workspace is gone"
    if not is_git_repo(workdir) and not (workdir / ".git").is_file():
        # Non-git snapshot (e.g. demo without history): treat file presence
        # as the diff signal via git_diff_all best-effort.
        try:
            diff = git_diff_all(workdir)
        except Exception:
            diff = ""
        return True, diff, "non-git workspace"
    try:
        p = subprocess.run(
            ["git", "diff", "--check", "--", "."],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        return False, "", f"diff check crashed: {e}"
    if p.returncode != 0:
        out = (p.stdout + p.stderr)[-1000:]
        return False, "", f"Agent produced an invalid diff: {out[:500]}"
    try:
        diff = git_diff_all(workdir)
    except Exception:
        diff = ""
    return True, diff, "ok"


def run_issue(run_id: int, *, _agent_runner=None) -> dict:
    """Execute one issue run. Own DB session (thread-safe).

    _agent_runner is a test seam: (workdir, prompt) -> MiniSweResult-like
    object with .exit_status/.diff/.changed_files/.steps. When None, the
    real mini-SWE-agent adapter runs (requires package + Docker + LLM key).
    """
    db: Session = SessionLocal()
    try:
        task = db.query(Task).filter_by(id=run_id).first()
        if task is None:
            return {"run_id": run_id, "state": FAILED, "error": "task not found"}
        _set_state(db, task, RUNNING, f"issue=#{task.issue_number} {task.title[:200]}")

        from ..models import Repository

        repo = db.query(Repository).filter_by(id=task.repo_id).first()
        if repo is None:
            msg = "repo not found for task — connect or clone it first, then re-run"
            _set_state(db, task, FAILED, msg)
            return {
                "run_id": task.id,
                "state": FAILED,
                "error": msg,
                "status_code": 400,
            }

        # Workspace: fresh isolated worktree (or snapshot). Base never written.
        try:
            from ..chat.router import resolve_workdir
            from ..repo.workspaces import create_task_workspace
        except Exception as e:
            msg = f"workspace helpers unavailable: {e}"
            _set_state(db, task, BLOCKED, msg)
            return {"run_id": task.id, "state": BLOCKED, "error": msg}
        try:
            base = resolve_workdir(repo)
        except Exception:
            msg = f"repo workspace not found for {repo.full_name} — clone it first"
            _set_state(db, task, FAILED, msg)
            return {
                "run_id": task.id,
                "state": FAILED,
                "error": msg,
                "status_code": 400,
            }
        if not base.is_dir():
            msg = f"repo workspace not found for {repo.full_name} — clone it first"
            _set_state(db, task, FAILED, msg)
            return {
                "run_id": task.id,
                "state": FAILED,
                "error": msg,
                "status_code": 400,
            }
        workdir, ws_sha = create_task_workspace(base, task.id)
        task.workspace_path = str(workdir)
        task.base_sha = ws_sha or ""
        db.commit()
        _event(
            db,
            task.id,
            "WORKSPACE",
            f"isolated workspace at base {ws_sha or 'snapshot'}",
        )

        # Context: memory + intel hints + fetched issue content
        # (context, not control flow).
        issue_body = _load_issue_body(db, task)
        mems = _memory_facts(db, task.repo_id, task.title)
        if mems:
            _event(db, task.id, "MEMORY", f"retrieved {len(mems)} relevant memories")
        intel = _intel_summary(workdir, task.title)
        linked_ctx = _enrich_context(db, task, repo, issue_body)
        if linked_ctx:
            _event(
                db,
                task.id,
                "CONTEXT",
                f"fetched linked issue content ({len(linked_ctx)} chars)",
            )
        followup_ctx = _followup_context(db, task)

        from .miniswe_adapter import (
            MiniSweAgentUnavailable,
            build_task_prompt,
            miniswe_available,
            model_spec_from_settings,
        )

        prompt = build_task_prompt(
            issue_title=f"#{task.issue_number} {task.title}"
            if task.issue_number
            else task.title,
            issue_body=issue_body,
            repo_name=repo.full_name if repo is not None else "",
            default_branch=(repo.default_branch if repo is not None else "") or "main",
            intel_summary=intel,
            memory_facts=mems,
            linked_context=linked_ctx,
            followup_context=followup_ctx,
        )
        _event(db, task.id, "AGENT", "agent started")

        # Agent run.
        if _agent_runner is not None:
            try:
                result = _agent_runner(workdir, prompt)
            except Exception as e:
                msg = f"agent crashed: {e}"[:500]
                _set_state(db, task, FAILED, msg)
                return {"run_id": task.id, "state": FAILED, "error": msg}
            exit_status = getattr(result, "exit_status", "")
            diff = getattr(result, "diff", "") or ""
            changed = list(getattr(result, "changed_files", []) or [])
            steps = list(getattr(result, "steps", []) or [])
            submission = getattr(result, "submission", "") or ""
        else:
            if not miniswe_available():
                msg = "mini-swe-agent not installed — pip install mini-swe-agent==2.4.6"
                _set_state(db, task, BLOCKED, msg)
                return {"run_id": task.id, "state": BLOCKED, "error": msg}
            model_name, model_kwargs, has_key = model_spec_from_settings()
            if not has_key:
                msg = "no model API key configured"
                _set_state(db, task, BLOCKED, msg)
                return {"run_id": task.id, "state": BLOCKED, "error": msg}
            from .miniswe_adapter import run_fix
            from ..sandbox.docker_runner import preferred_agent_image

            image = preferred_agent_image(
                settings.miniswe_image, settings.sandbox_image
            )
            try:
                result = run_fix(
                    workdir,
                    prompt,
                    model_name=model_name,
                    model_kwargs=model_kwargs,
                    image=image,
                    step_limit=settings.miniswe_step_limit,
                    wall_time_s=settings.miniswe_wall_time_s,
                )
            except MiniSweAgentUnavailable as e:
                msg = str(e)[:500]
                _set_state(db, task, BLOCKED, msg)
                return {"run_id": task.id, "state": BLOCKED, "error": msg}
            except Exception as e:
                msg = f"agent crashed: {e}"[:500]
                _set_state(db, task, FAILED, msg)
                return {"run_id": task.id, "state": FAILED, "error": msg}
            exit_status = result.exit_status
            diff = result.diff
            changed = result.changed_files
            steps = result.steps
            submission = result.submission

        for s in (steps or [])[:40]:
            try:
                cmd = getattr(s, "command", "")[:200]
                rc = getattr(s, "returncode", None)
                rc_s = f"rc={rc}" if rc is not None else "rc=?"
                step_no = getattr(s, "step", "?")
            except Exception:
                continue
            _event(db, task.id, "AGENT", f"step {step_no} :: {cmd} → {rc_s}")
        _event(
            db, task.id, "AGENT", f"agent finished (exit={exit_status or 'unknown'})"
        )

        # Sanity: git status + diff --check (no gates, no retry).
        ok, workspace_diff, reason = _sanity_check(workdir)
        final_diff = (workspace_diff or "").strip() or (diff or "").strip()
        if not ok:
            _set_state(db, task, COMPLETED, f"Agent produced an invalid diff. {reason}")
            return {"run_id": task.id, "state": COMPLETED, "error": reason, "diff": ""}
        if not final_diff or not changed and not final_diff:
            if "INSUFFICIENT_INFO" in (submission or "").upper():
                note = (
                    "No code changes produced (insufficient info): the issue "
                    "has no actionable description — add what should change, "
                    "then re-run."
                )
            elif (exit_status or "") == "LimitsExceeded":
                note = (
                    "No code changes produced (agent hit its step limit without "
                    "editing anything — the issue may lack actionable detail)."
                )
            else:
                note = "No code changes produced."
            comment_url = _maybe_ask_back(db, task, repo, note, steps)
            _set_state(db, task, COMPLETED, note)
            _save_result_memory(db, task, [], "", "")
            out = {
                "run_id": task.id,
                "state": COMPLETED,
                "note": note,
            }
            if comment_url:
                out["comment_url"] = comment_url
            return out
        if not changed:
            # Derive changed files locally (no verify dependency).
            changed = sorted(
                {
                    line[6:].strip()
                    for line in final_diff.splitlines()
                    if line.startswith(("+++ b/", "--- a/"))
                    and line[6:].strip() not in ("dev/null", "/dev/null")
                }
            )
        try:
            from ..models import Patch

            db.add(Patch(task_id=task.id, diff=final_diff[-20000:], branch=""))
            db.commit()
        except Exception:
            db.rollback()
        _event(db, task.id, "DIFF", f"{len(changed)} file(s) changed")

        # Publish: diff → branch → commit → push → PR.
        try:
            from ..github.publisher import publish_issue_fix

            pub = publish_issue_fix(
                db,
                task,
                repo,
                workdir,
                tests_note=_tests_note(steps),
            )
        except Exception as e:
            msg = f"publish failed: {e}"[:800]
            _set_state(db, task, FAILED, msg)
            return {"run_id": task.id, "state": FAILED, "error": msg}

        _save_result_memory(
            db, task, changed, pub.get("commit_sha", ""), pub.get("pr_url", "")
        )
        _set_state(
            db,
            task,
            COMPLETED,
            f"fix complete: {len(changed)} file(s), commit {pub.get('commit_sha', '')[:8]}, PR {pub.get('pr_url', '')}",
        )
        return {
            "run_id": task.id,
            "state": COMPLETED,
            "files_changed": changed,
            "commit_sha": pub.get("commit_sha", ""),
            "branch": pub.get("branch", ""),
            "pr_url": pub.get("pr_url", ""),
        }
    finally:
        db.close()


def _load_issue_body(db: Session, task: Task) -> str:
    """Best-effort issue text: all stored messages joined (body + comments +
    comment replies, oldest first). Never fails."""
    try:
        from ..models import ChatMessage

        rows = (
            db.query(ChatMessage)
            .filter_by(task_id=task.id)
            .order_by(ChatMessage.id.asc())
            .limit(8)
            .all()
        )
        texts = [(r.content or "").strip() for r in rows]
        texts = [t for t in texts if t]
        if texts:
            return "\n\n".join(texts)[:3000]
    except Exception:
        pass
    return ""


def _tests_note(steps) -> str:
    """Honest tests note from agent steps. Never fabricates results."""
    try:
        cmds = [str(getattr(s, "command", "")) for s in (steps or [])]
    except Exception:
        return "Not reported by agent."
    ran = sorted(
        {
            c[:120]
            for c in cmds
            if any(k in c for k in ("pytest", "npm test", "ruff", "mypy", "tsc"))
        }
    )
    if not ran:
        return "Not reported by agent."
    return "Agent ran: " + "; ".join(ran[:5])


def _maybe_ask_back(db: Session, task: Task, repo, note: str, steps) -> str:
    """Post one clarifying comment on the GitHub issue (fail-open).

    Only when there is a real issue number + installation to post with.
    The comment carries a marker so a human reply re-triggers the agent.
    Returns the comment URL or "".
    """
    try:
        issue_no = task.issue_number or 0
        installation_id = (getattr(repo, "installation_id", "") or "").strip()
        full_name = (getattr(repo, "full_name", "") or "").strip()
        if not issue_no or not installation_id or not full_name:
            return ""
        # One ask-back per run; skip if this run already posted.
        already = (
            db.query(TaskEvent)
            .filter_by(task_id=task.id, stage="COMMENT_POSTED")
            .count()
        )
        if already:
            return ""
        from ..github.app_auth import get_installation_token
        from ..github.publisher import build_ask_back_comment, post_issue_comment

        n_steps = len(list(steps or []))
        comment = build_ask_back_comment(
            issue_number=issue_no,
            issue_title=task.title or "",
            steps_taken=n_steps,
            tests_note=_tests_note(steps),
        )
        posted = post_issue_comment(
            get_installation_token(installation_id), full_name, issue_no, comment
        )
        url = str((posted or {}).get("html_url", ""))
        db.add(
            TaskEvent(task_id=task.id, stage="COMMENT_POSTED", message=comment[:2000])
        )
        db.commit()
        return url
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return ""


def _save_result_memory(
    db: Session, task: Task, changed: list[str], sha: str, pr_url: str
) -> None:
    """Durable result memory (context for future runs). Never blocks."""
    try:
        from ..memory.store import remember

        files = ", ".join(changed[:10]) if changed else "no files"
        fact = f"Fixed issue #{task.issue_number} ({task.title[:150]}): {files}."
        if sha:
            fact += f" Commit {sha[:8]}."
        if pr_url:
            fact += f" PR {pr_url}."
        remember(db, task.repo_id, "task", fact, commit_sha=sha or "")
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
