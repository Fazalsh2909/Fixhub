"""Primary autonomous coding path: FixHub prepares, mini-SWE-agent codes.

FixHub Task
    ↓ prepare workspace (caller) + baseline (here)
    ↓ prepare prompt/context (here)
    ↓ mini-SWE-agent (adapter)
    ↓ workspace modifications
    ↓ verify + classify + proof (here)
    ↓ return control to FixHub (caller: approval/publish)

Single attempt. Deterministic outcomes (verified / baseline-failure /
environment / blocked) never re-loop the agent. Only retryable provider or
infrastructure errors are marked retryable for the caller.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..logging import get_logger, log_event
from ..memory.store import retrieve, snapshot_task
from ..models import Patch, Repository, Task, TaskEvent
from ..verify.pipeline import PHASE_AFTER, PHASE_BASELINE, load_results
from ..verify.simple import (
    build_simple_proof,
    decide_verdict,
    persist_simple_attribution,
    run_four_checks,
    verdict_summary,
)
from .miniswe_adapter import (
    MiniSweAgentUnavailable,
    build_task_prompt,
    miniswe_available,
    model_spec_from_settings,
    run_fix,
)
from .orchestrator import transition

logger = get_logger("fixhub.miniswe_runner")


def _intel_summary(workdir: Path, title: str, limit: int = 12) -> str:
    """Compact relevant-code hints: file:line matches for issue keywords."""
    from ..intel.indexer import search_code

    tokens = [t for t in re.split(r"\W+", title or "") if len(t) > 3][:3]
    seen: list[str] = []
    for tok in tokens:
        try:
            hits = search_code(workdir, re.escape(tok))[:20]
        except Exception:
            continue
        for h in hits:
            ref = f"{h.get('file')}:{h.get('line')}"
            if ref not in seen:
                seen.append(ref)
            if len(seen) >= limit:
                return "; ".join(seen)
    return "; ".join(seen)


def _memory_facts(db: Session, repo_id: int, title: str, limit: int = 8) -> list[str]:
    try:
        mems = retrieve(db, repo_id, title, limit=limit)
    except Exception:
        return []
    return [f"[{m.type}] {m.fact[:300]}" for m in mems]


def _event(db: Session, task_id: int, stage: str, message: str) -> None:
    db.add(TaskEvent(task_id=task_id, stage=stage, message=message[:2000]))
    db.commit()


def run_miniswe_task(
    db: Session, task: Task, workdir: Path, repo: Repository | None
) -> dict:
    """One autonomous attempt. Returns a result dict for run_task_sync."""
    if not miniswe_available():
        msg = "mini-swe-agent not installed — pip install mini-swe-agent==2.4.6"
        _event(db, task.id, "BLOCKED", msg)
        transition(db, task, "BLOCKED", msg)
        return {"verified": False, "error": msg, "retryable": False}

    model_name, model_kwargs, has_key = model_spec_from_settings()
    if not has_key:
        msg = "no model API key configured — set TOKENROUTER_API_KEY (or provider key)"
        _event(db, task.id, "BLOCKED", msg)
        transition(db, task, "BLOCKED", msg)
        return {"verified": False, "error": msg, "retryable": False}

    repo_name = repo.full_name if repo is not None else ""
    issue_no = task.issue_number or 0
    transition(db, task, "ANALYZING", f"issue={task.title}; engine=mini-swe-agent")

    # 1. BASELINE before the agent touches anything.
    transition(db, task, "REPRODUCING", "recording pre-patch baseline")
    try:
        baseline = run_four_checks(db, task, workdir, PHASE_BASELINE, [])
    except Exception as e:
        msg = f"baseline crashed: {e}"
        _event(db, task.id, "BLOCKED", msg)
        transition(db, task, "BLOCKED", msg)
        return {"verified": False, "error": msg, "retryable": False}
    base_line = ", ".join(f"{r.check}:{r.status}" for r in baseline)[:500]
    _event(db, task.id, "BASELINE", f"pre-patch snapshot :: {base_line}")

    # 2. Compact prompt: issue + hints, never dumps.
    mems = _memory_facts(db, task.repo_id, task.title)
    intel = _intel_summary(workdir, task.title)
    if mems:
        _event(db, task.id, "MEMORY", f"retrieved {len(mems)} relevant memories")
    prompt = build_task_prompt(
        issue_title=f"#{issue_no} {task.title}" if issue_no else task.title,
        repo_name=repo_name,
        intel_summary=intel,
        memory_facts=mems,
        verification_expectations=(
            "Fix the issue, then run the repo's tests. "
            "The regression test must fail before and pass after your change."
        ),
    )

    # 3. AGENT (FixHub observes; mini-SWE-agent drives).
    _event(
        db,
        task.id,
        "RUNNING",
        f"mini-swe-agent started :: model={model_name} steps={settings.miniswe_step_limit}",
    )
    transition(db, task, "PLANNING", "agent exploring and implementing")
    image = settings.miniswe_image.strip() or settings.sandbox_image
    traj_path = workdir.parent / f"task-{task.id}.traj.json"
    try:
        result = run_fix(
            workdir,
            prompt,
            model_name=model_name,
            model_kwargs=model_kwargs,
            image=image,
            step_limit=settings.miniswe_step_limit,
            wall_time_s=settings.miniswe_wall_time_s,
            traj_path=traj_path,
        )
    except MiniSweAgentUnavailable as e:
        msg = str(e)[:500]
        _event(db, task.id, "BLOCKED", msg)
        transition(db, task, "BLOCKED", msg)
        return {"verified": False, "error": msg, "retryable": False}
    except Exception as e:
        from ..automation import _is_retryable_error

        msg = f"agent crashed: {e}"[:500]
        retryable = _is_retryable_error(str(e))
        _event(db, task.id, "FAILED", msg)
        transition(db, task, "FAILED", msg)
        return {"verified": False, "error": msg, "retryable": retryable}

    for s in result.steps[:40]:
        rc = f"rc={s.returncode}" if s.returncode is not None else "rc=?"
        _event(db, task.id, "AGENT", f"step {s.step} :: {s.command[:200]} → {rc}")
    log_event(logger, "miniswe_done", task_id=task.id, exit_status=result.exit_status)

    # No changes = nothing to verify. Say so loudly instead of running
    # verification theater on an untouched tree (the publisher would refuse
    # an empty diff anyway).
    if not result.changed_files and not (result.diff or "").strip():
        msg = (
            f"agent made no changes (exit={result.exit_status or 'unknown'}) — "
            "nothing to verify; refine the issue and re-run"
        )
        _event(db, task.id, "FAILED", msg)
        transition(db, task, "FAILED", msg)
        return {"verified": False, "error": msg, "retryable": False}

    # 4. VERIFY the workspace the agent left behind.
    transition(db, task, "VERIFYING", "running 4-check verification")
    try:
        after = run_four_checks(db, task, workdir, PHASE_AFTER, result.changed_files)
    except Exception as e:
        msg = f"verification crashed: {e}"
        _event(db, task.id, "BLOCKED", msg)
        transition(db, task, "BLOCKED", msg)
        return {"verified": False, "error": msg, "retryable": False}
    verdict = decide_verdict(
        load_results(db, task.id, PHASE_BASELINE), after, result.changed_files
    )
    persist_simple_attribution(db, task.id, verdict)

    issue_ref = (
        f"Issue: #{issue_no} {task.title}" if issue_no else f"Issue: {task.title}"
    )
    proof = build_simple_proof(
        issue_ref=issue_ref,
        base_sha=task.base_sha or "",
        before=load_results(db, task.id, PHASE_BASELINE),
        after=after,
        verdict=verdict,
        changed_files=result.changed_files,
        diff_head=result.diff,
    )
    try:
        db.add(
            Patch(
                task_id=task.id,
                diff=result.diff[-20000:] or "(no files changed)",
                branch="",
            )
        )
        db.commit()
    except Exception:
        db.rollback()
    try:
        snapshot_task(
            db,
            task.repo_id,
            task.id,
            task.title,
            verdict.task_state,
            f"mini-swe-agent {result.exit_status}; verdict={verdict.verdict}",
        )
    except Exception:
        pass
    _event(
        db,
        task.id,
        "VERDICT",
        f"{verdict.verdict} new={len(verdict.new_failure_lines)} "
        f"pre_existing={verdict.pre_existing} debug={verdict.debug} :: "
        f"{verdict_summary(verdict)}",
    )
    transition(
        db,
        task,
        verdict.task_state,
        f"verify {verdict.verdict} new={len(verdict.new_failure_lines)} "
        f"unchanged={verdict.pre_existing}",
    )
    return {
        "verified": verdict.verified,
        "publishable": verdict.publishable,
        "overall": verdict.verdict,
        "proof": proof,
        "verdict": verdict.verdict,
        "debug": verdict.debug,
        "new_failures": verdict.new_failure_lines,
        "pre_existing": verdict.pre_existing,
        "changed_files": result.changed_files,
        "steps": len(result.steps),
        "exit_status": result.exit_status,
        "retryable": False,
    }
