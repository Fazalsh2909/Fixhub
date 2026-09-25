"""Autonomous agent: state machine + ReAct tool loop. Evidence before commit."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from ..llm.base import LLMProvider
from ..logging import get_logger, log_event
from ..memory.store import retrieve
from ..models import Task, TaskEvent
from ..sandbox.docker_runner import run_in_sandbox
from ..tools.registry import run_command, tool_specs

logger = get_logger("fixhub.agent")
# Phase 9: explicit durable states. Forward progress goes down the list;
# FAILED/BLOCKED/CANCELLED are terminal failures (retry re-enters explicitly).
STATES = [
    "CREATED",
    "ANALYZING",
    "REPRODUCING",
    "ROOT_CAUSE_FOUND",
    "PLANNING",
    "IMPLEMENTING",
    "TESTING",
    "DEBUGGING",
    "VERIFYING",
    "REVIEWING",
    "READY_FOR_APPROVAL",
    "APPROVED",
    "BRANCH_CREATED",
    "COMMITTED",
    "PUSHED",
    "PR_CREATING",
    "PR_CREATED",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "NEEDS_INFO",
]
# Allowed forward/retry edges. Anything not listed is rejected loudly —
# states must derive from executed operations, never from claims.
TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"ANALYZING", "NEEDS_INFO", "FAILED", "CANCELLED"}),
    "ANALYZING": frozenset({"REPRODUCING", "FAILED", "CANCELLED"}),
    "REPRODUCING": frozenset({"ROOT_CAUSE_FOUND", "PLANNING", "FAILED", "CANCELLED"}),
    "ROOT_CAUSE_FOUND": frozenset(
        {"PLANNING", "IMPLEMENTING", "VERIFYING", "FAILED", "CANCELLED"}
    ),
    "PLANNING": frozenset({"IMPLEMENTING", "VERIFYING", "FAILED", "CANCELLED"}),
    "IMPLEMENTING": frozenset(
        {"TESTING", "VERIFYING", "DEBUGGING", "FAILED", "CANCELLED"}
    ),
    "TESTING": frozenset({"VERIFYING", "DEBUGGING", "FAILED", "CANCELLED"}),
    "DEBUGGING": frozenset(
        {"ANALYZING", "IMPLEMENTING", "VERIFYING", "FAILED", "CANCELLED"}
    ),
    "VERIFYING": frozenset(
        {"READY_FOR_APPROVAL", "DEBUGGING", "FAILED", "BLOCKED", "CANCELLED"}
    ),
    # Explicit re-runs (Run button / force) rewind finished-but-unpublished
    # tasks into a fresh attempt. Audited via prev_state; published states
    # (APPROVED+) intentionally have no rewind edge.
    "REVIEWING": frozenset(
        {
            "ANALYZING",
            "APPROVED",
            "DEBUGGING",
            "READY_FOR_APPROVAL",
            "FAILED",
            "CANCELLED",
        }
    ),
    "READY_FOR_APPROVAL": frozenset(
        {"ANALYZING", "APPROVED", "DEBUGGING", "FAILED", "CANCELLED"}
    ),
    "APPROVED": frozenset({"BRANCH_CREATED", "COMMITTED", "FAILED", "CANCELLED"}),
    "BRANCH_CREATED": frozenset({"COMMITTED", "FAILED", "CANCELLED"}),
    "COMMITTED": frozenset({"PUSHED", "PR_CREATING", "FAILED", "CANCELLED"}),
    "PUSHED": frozenset({"PR_CREATING", "PR_CREATED", "FAILED", "CANCELLED"}),
    "PR_CREATING": frozenset({"PR_CREATED", "PUSHED", "FAILED", "CANCELLED"}),
    "PR_CREATED": frozenset(),
    "FAILED": frozenset({"ANALYZING", "CREATED", "CANCELLED"}),
    "BLOCKED": frozenset({"ANALYZING", "CREATED", "CANCELLED"}),
    "NEEDS_INFO": frozenset({"ANALYZING", "CANCELLED"}),
    "CANCELLED": frozenset(),
}
MAX_ITERS = 12


def _execute_tool(workdir: Path, name: str, args: dict) -> dict:
    """Single tool dispatch (pure — safe for parallel read-only batch)."""
    if name in ("run_command", "run_test"):
        cmd = args.get("cmd") or args.get("target") or ""
        program = args.get("program") or ""
        pargs = args.get("args")
        if not cmd and not program:
            cmd = "pytest -q"
        return run_command(
            workdir,
            cmd if isinstance(cmd, str) else "",
            program=program if isinstance(program, str) else "",
            args=pargs if isinstance(pargs, list) else None,
        )
    if name == "edit_file":
        from ..tools.registry import edit_file

        return edit_file(
            workdir,
            args.get("path", ""),
            args.get("old_string", ""),
            args.get("new_string", ""),
        )
    if name == "create_file":
        from ..tools.registry import create_file

        return create_file(workdir, args.get("path", ""), args.get("content", ""))
    if name == "search_code":
        from ..intel.indexer import search_code

        return {
            "ok": True,
            "output": str(search_code(workdir, args.get("pattern", ""))[:20]),
        }
    if name == "read_file":
        from ..tools.registry import read_file

        return read_file(workdir, args.get("path", ""))
    if name == "list_files":
        from ..tools.registry import _resolve

        base = _resolve(workdir, args.get("dir", "."))
        return (
            {
                "ok": True,
                "output": str(
                    [
                        str(x.relative_to(workdir))
                        for x in base.rglob("*")
                        if x.is_file()
                    ][:100]
                ),
            }
            if base and base.is_dir()
            else {"ok": False, "output": "path escapes workdir"}
        )
    return {"ok": False, "output": "unknown tool"}


_READ_ONLY = {"list_files", "read_file", "search_code"}


def _run_batch(workdir: Path, calls: list[tuple[str, dict]]) -> list[dict]:
    """Run one turn's tool calls. Read-only batches run in parallel; writes stay sequential."""
    if len(calls) > 1 and all(name in _READ_ONLY for name, _ in calls):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(4, len(calls))) as ex:
            return list(ex.map(lambda nc: _execute_tool(workdir, nc[0], nc[1]), calls))
    return [_execute_tool(workdir, name, args) for name, args in calls]


class InvalidTransitionError(ValueError):
    """Raised when code tries to jump between states with no allowed edge."""


def transition(db: Session, task: Task, state: str, message: str = "") -> None:
    """Durable state change with an explicit audit event.

    Every event records task_id, previous_state, new_state (stage),
    timestamp (created_at) and reason (message). Jumps with no allowed edge
    raise InvalidTransitionError — callers must walk the state machine.
    """
    assert state in STATES, f"unknown task state {state}"
    prev = task.state or "CREATED"
    allowed = TRANSITIONS.get(prev, frozenset())
    # First assignment onto a fresh row (CREATED→…) always passes; legacy
    # rows in unknown states fall through to the explicit edge check.
    if prev in STATES and state not in allowed and prev != state:
        raise InvalidTransitionError(f"illegal task transition {prev} → {state}")
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
    log_event(logger, "task_transition", task_id=task.id, stage=state)


def _system_message(skills_ctx: str = "") -> str:
    base = (
        "You are Fixhub, a careful engineer. For multi-step work keep a plan with "
        "write_todos (whole list each time, exactly one in_progress) and update it as you go. "
        "To learn how the codebase works, hand a self-contained question to the task tool "
        "(a read-only explorer; only its answer returns) instead of burning turns searching yourself. "
        "Fix with edit_file (existing files) — never create_file over an existing file. "
        "Verify with run_test. Never claim a fix without test evidence."
    )
    if skills_ctx.strip():
        return base + "\n\n" + skills_ctx.strip()
    return base


def _run_special(
    db: Session,
    task: Task,
    workdir: Path,
    llm: LLMProvider,
    name: str,
    args: dict,
    llm_state: dict | None = None,
) -> dict | None:
    """Tools needing loop context (db/task/llm). None = not special, run normally."""
    from ..config import settings as _settings

    if name == "write_todos":
        from .plan import write_todos

        todos = args.get("todos", [])
        return write_todos(db, task.id, todos if isinstance(todos, list) else [])
    if name == "task":
        from .subagent import explore

        question = str(args.get("description", ""))[:2000]
        if not question.strip():
            return {"ok": False, "output": "description required"}
        db.add(
            TaskEvent(
                task_id=task.id, stage="SUBAGENT", message=f"start :: {question[:300]}"
            )
        )
        db.commit()
        report = explore(
            workdir,
            question,
            llm,
            max_turns=_settings.subagent_max_turns,
            llm_state=llm_state,
        )
        db.add(
            TaskEvent(
                task_id=task.id, stage="SUBAGENT", message=f"done :: {report[:800]}"
            )
        )
        db.commit()
        return {"ok": True, "output": report[:4000]}
    return None


def engineer_issue(
    db: Session,
    task: Task,
    workdir: Path,
    llm: LLMProvider,
    llm_state: dict | None = None,
) -> dict:
    """Vertical-slice autonomous loop. Returns summary; persists every step so a crash resumes.

    llm_state is a shared {"calls": int} counter (one per task run): every
    logical LLM call — main loop and subagent turns — increments it, and the
    AGENT_MAX_LLM_CALLS budget is enforced against it. Callers that run
    multiple attempts (run_task_sync) pass one dict so the budget spans them;
    None means an ephemeral single-attempt budget.
    """
    from ..config import settings as _settings

    from .skills import build_skill_context

    mems = retrieve(db, task.repo_id, task.title)
    mem_ctx = "\n".join(f"- [{m.type}] {m.fact} (src: {m.source_path})" for m in mems)
    transition(db, task, "ANALYZING", f"issue={task.title}; memories={len(mems)}")
    # Skill injection: relevant expert workflows, budgeted. Off when dir missing.
    selection = (
        build_skill_context(
            task.title,
            skills_dir=_settings.skills_dir,
            top_k=_settings.skills_top_k,
            max_chars=_settings.skills_max_chars,
        )
        if _settings.skills_enabled
        else build_skill_context("")
    )
    if selection.skills:
        names = ",".join(s.name for s in selection.skills)
        db.add(
            TaskEvent(
                task_id=task.id,
                stage="SKILLS",
                message=f"injected={names} chars={len(selection.text)}",
            )
        )
        db.commit()
    # 1. reproduce (real execution, never fabricated; per-repo tolerant)
    transition(db, task, "REPRODUCING", "running reproduction")
    from ..verify.pipeline import detect_verification_config

    _cfg = detect_verification_config(workdir)
    _repro_cmd = _cfg.get("regression") or _cfg.get("suite") or "python -m pytest -q"
    _repro_root = (
        workdir / _cfg.get("project_root", "") if _cfg.get("project_root") else workdir
    )
    # P0-4: reproduction is untrusted repo-defined execution — it requires
    # isolation and fails closed without it. Never runs on the API host.
    # Runs in the detected project root so monorepo suites resolve, with the
    # same /deps volume the gates use (best-effort install first so the
    # before/after comparison isn't measuring missing modules).
    from ..sandbox.docker_runner import deps_volume_for_task
    from ..verify.pipeline import ensure_deps

    ensure_deps(
        workdir, _cfg.get("project_root", "") or "", _cfg.get("install"), task.id
    )
    repro = run_in_sandbox(
        _repro_root,
        _repro_cmd,
        require_isolation=True,
        deps_volume=deps_volume_for_task(task.id),
    )
    if repro.get("sandbox") == "unavailable":
        # Pre-flight: without isolated execution nothing below can run
        # honestly (every gate requires it). Fail loudly with zero LLM burn
        # instead of 12 doomed iters.
        msg = (
            "deterministic failure: isolated execution unavailable "
            "(Docker daemon unreachable) — start it and re-run; no LLM loop ran"
        )
        transition(db, task, "FAILED", msg)
        return {"verified": False, "results": [], "error": msg, "retryable": False}
    # 1b. baseline snapshot (pre-patch evidence). The AFTER gates are
    # compared against THIS — never against vibes. Skipped when a baseline
    # already exists (manual re-runs keep the original base commit record).
    from ..verify.pipeline import PHASE_BASELINE, load_results, run_verification

    baseline_results: list = []
    try:
        from ..models import VerificationRun as _VR

        has_baseline = (
            db.query(_VR).filter_by(task_id=task.id, phase=PHASE_BASELINE).count() > 0
        )
    except Exception:
        has_baseline = False
    if not has_baseline:
        baseline_results = run_verification(db, task, workdir, phase=PHASE_BASELINE)
        base_line = ", ".join(f"{r.check}:{r.status}" for r in baseline_results)[:500]
        db.add(
            TaskEvent(
                task_id=task.id,
                stage="BASELINE",
                message=f"pre-patch snapshot :: {base_line}",
            )
        )
        db.commit()
    else:
        baseline_results = load_results(db, task.id, PHASE_BASELINE)
    transition(
        db,
        task,
        "ROOT_CAUSE_FOUND" if not repro["ok"] else "PLANNING",
        f"repro ok={repro['ok']}",
    )
    # 2. tool loop (bounded, token-efficient: only mem slice + tool outputs in context)
    # Provider failures must NEVER 500 the task: record FAILED with the cause.
    from ..llm.openrouter import ProviderError

    messages: list[dict] = [
        {
            "role": "system",
            "content": _system_message(selection.text),
        },
        {
            "role": "user",
            "content": f"Issue #{task.issue_number}: {task.title}\nRelevant memory:\n{mem_ctx}\nRepro output:\n{repro['output'][:3000]}",
        },
    ]
    # A previous attempt's attribution verdict survives in TaskEvents: feed it
    # back so a manual re-run doesn't rediscover the same baseline failures.
    try:
        _prev_attr = (
            db.query(TaskEvent)
            .filter_by(task_id=task.id, stage="ATTRIBUTION")
            .order_by(TaskEvent.id.desc())
            .first()
        )
        if _prev_attr is not None and (_prev_attr.message or "").strip():
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Previous attempt verdict (do not relitigate settled "
                        f"facts):\n{(_prev_attr.message or '')[:1200]}"
                    ),
                }
            )
    except Exception:
        pass
    loop_error: str | None = None
    loop_fatal = False
    consecutive_failures = 0
    prev_turn_sig: tuple | None = None
    repeat_count = 0
    if llm_state is None:
        llm_state = {"calls": 0}
    try:
        from ..metrics import snapshot as _metrics_snapshot

        _cost_before = _metrics_snapshot()["est_cost_usd"]
    except Exception:
        _cost_before = 0.0
    for i in range(MAX_ITERS):
        # Per-task cost cap: abort before another billable call.
        try:
            from ..config import settings as _s

            from ..metrics import snapshot as _snap

            _cap = float(getattr(_s, "agent_max_cost_usd", 0.0) or 0.0)
            if _cap > 0 and (_snap()["est_cost_usd"] - _cost_before) >= _cap:
                loop_error = f"cost cap reached (${_cap:.2f}) — stopping to avoid spend"
                loop_fatal = True
                db.add(TaskEvent(task_id=task.id, stage="TOOL", message=loop_error))
                db.commit()
                break
            # Per-task LLM call budget: counts logical calls (free models
            # bill $0, so dollars can't guard them). Shared with subagents
            # and across retry attempts via llm_state.
            _max_calls = int(getattr(_s, "agent_max_llm_calls", 0) or 0)
            if _max_calls > 0 and int(llm_state.get("calls", 0)) >= _max_calls:
                loop_error = (
                    f"llm call budget reached ({_max_calls}) — stopping to avoid spend"
                )
                loop_fatal = True
                db.add(TaskEvent(task_id=task.id, stage="TOOL", message=loop_error))
                db.commit()
                break
        except Exception:
            pass
        # Per-turn context: budget the transcript, remind of git + plan.
        # Base `messages` stays canonical; the call sees the budgeted view.
        from .context import fit, git_reminder, strip_old_outputs
        from .plan import plan_prompt

        try:
            _budget = int(
                getattr(_settings, "agent_context_budget_chars", 60000) or 60000
            )
        except Exception:
            _budget = 60000
        refresh: list[str] = []
        try:
            _git = git_reminder(workdir)
            if _git:
                refresh.append(_git)
        except Exception:
            pass
        try:
            _plan = plan_prompt(db, task.id)
            if _plan:
                refresh.append(_plan)
        except Exception:
            pass
        call_messages = strip_old_outputs(fit(messages, _budget))
        if refresh:
            call_messages = call_messages + [
                {
                    "role": "user",
                    "content": "Context refresh:\n" + "\n\n".join(refresh)[:2000],
                }
            ]
        try:
            llm_state["calls"] = int(llm_state.get("calls", 0)) + 1
            resp = llm.tool_call(call_messages, tool_specs())
        except ProviderError as e:
            loop_error = str(e)[:500]
            break
        # Provider-native history: assistant tool_calls need id/type/function shape
        # and each tool result must carry the matching tool_call_id, or the next
        # request 400s. Ids are synthetic but consistent within the transcript.
        valid_calls = [
            c for c in resp.tool_calls if isinstance(c, dict) and c.get("name")
        ]
        native_calls: list[dict] = []
        for j, call in enumerate(valid_calls):
            raw_args = call.get("arguments", "{}") or "{}"
            arg_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args)
            native_calls.append(
                {
                    "id": f"call-{i}-{j}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": arg_str},
                }
            )
        messages.append(
            {
                "role": "assistant",
                "content": resp.text or "",
                "tool_calls": native_calls,
            }
        )
        if not native_calls:
            break

        # Parse args once. Special tools (task/write_todos) need loop context
        # and run inline; plain tools batch (read-only parallel, writes sequential).
        _SPECIAL = {"task", "write_todos"}
        parsed: list[tuple[str, dict, dict]] = []
        for call, native in zip(valid_calls, native_calls):
            try:
                args = json.loads(native["function"]["arguments"] or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            parsed.append(
                (call["name"], args if isinstance(args, dict) else {}, native)
            )
        plain = [(n, a) for n, a, _ in parsed if n not in _SPECIAL]
        outs: list[dict] = []
        plain_outs = _run_batch(workdir, plain) if plain else []
        pi = 0
        for name, args, _native in parsed:
            if name in _SPECIAL:
                outs.append(
                    _run_special(db, task, workdir, llm, name, args, llm_state)
                    or {"ok": False, "output": "unknown tool"}
                )
            else:
                outs.append(plain_outs[pi])
                pi += 1
        for (name, _args, native), out in zip(parsed, outs):
            db.add(
                TaskEvent(
                    task_id=task.id,
                    stage="TOOL",
                    message=f"{name} ok={out.get('ok')} :: {str(out.get('output', ''))[:500]}",
                )
            )
            db.commit()
            try:
                from ..metrics import record_tool_call

                record_tool_call()
            except Exception:
                pass
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": native["id"],
                    "content": str(out)[:4000],
                }
            )
        # Identical-failure breaker: repeating the exact same turn is never
        # progress (live runs burned 68 failed calls this way). Stop the loop
        # and fail loudly instead of spending 12 iters × retries.
        turn_sig = tuple(
            (
                n,
                json.dumps(a, sort_keys=True)[:300],
                bool(o.get("ok")),
                str(o.get("output", ""))[:300],
            )
            for (n, a, _), o in zip(parsed, outs)
        )
        if turn_sig and turn_sig == prev_turn_sig:
            repeat_count += 1
        else:
            repeat_count = 1 if turn_sig else 0
        prev_turn_sig = turn_sig
        if repeat_count >= 3:
            msg = (
                "deterministic failure: same turn repeated 3x "
                f"({turn_sig[0][0]} ok={turn_sig[0][2]}) — stopping to avoid spend"
            )
            db.add(TaskEvent(task_id=task.id, stage="BREAKER", message=msg[:500]))
            db.commit()
            transition(db, task, "FAILED", msg)
            return {"verified": False, "results": [], "error": msg, "retryable": False}
        # Stall detection + mid-loop reflection (no extra LLM call — context hint).
        turn_failed = all(not o.get("ok") for o in outs) if outs else False
        consecutive_failures = consecutive_failures + 1 if turn_failed else 0
        if consecutive_failures >= 3:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Hint: your last 3 turns all failed. Stop guessing args. "
                        'Use list_files {"dir": "."} then read_file, and only '
                        "run pytest/ruff/mypy/git/ls/cat commands."
                    ),
                }
            )
            db.add(
                TaskEvent(
                    task_id=task.id, stage="HINT", message="stall: 3 failed turns"
                )
            )
            db.commit()
            consecutive_failures = 0
        elif i == 5:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Reflection checkpoint (iter 6/12): summarize what you know, "
                        "what file the bug is in, and your next single edit + test. "
                        "Then do it — do not list more files."
                    ),
                }
            )
        log_event(logger, "agent_iter", task_id=task.id, iteration=i)
    if loop_error is not None:
        transition(
            db,
            task,
            "FAILED",
            loop_error if loop_fatal else f"provider error: {loop_error}",
        )
        err_out: dict = {"verified": False, "results": [], "error": loop_error}
        if loop_fatal:
            # Budget/cost abort: retrying cannot help — fail fast.
            err_out["retryable"] = False
        return err_out
    # 3. verify
    transition(db, task, "VERIFYING", "running verification pipeline")
    from ..verify.pipeline import (
        ATTR_NONE,
        ERROR,
        FAIL,
        PASS,
        SKIPPED,
        WITH_LIMITATIONS,
        attribute_verdict,
        build_verify_feedback,
        changed_files_from_diff,
        gate_signatures,
        persist_attribution,
        record_gate,
        regression_attribution,
        run_verification,
    )

    try:
        from ..repo.workspaces import git_diff_all as _git_diff_all

        _diff_now = _git_diff_all(workdir) if workdir.is_dir() else ""
        changed_now = changed_files_from_diff(_diff_now)
    except Exception:
        changed_now = []
    results = run_verification(
        db, task, workdir, phase="AFTER", changed_files=changed_now
    )
    # P0-6: real before/after regression record on the SAME suite command.
    # `repro` ran the suite before any edit; the suite gate ran it after.
    # This is a REQUIRED gate row (not just an event): a failed regression
    # blocks VERIFIED, and NOT_REPRODUCED is recorded honestly as SKIPPED.
    suite_after = next((r for r in results if r.check == "suite"), None)
    before_line = (
        f"regression {_repro_cmd}: {'FAIL' if not repro['ok'] else 'PASS'} — "
        f"{str(repro.get('output', '')).splitlines()[0][:200] if repro.get('output') else 'no output'}"
    )
    if repro["ok"]:
        db.add(
            TaskEvent(
                task_id=task.id,
                stage="REPRODUCTION",
                message=f"NOT_REPRODUCED: suite passed before any change ({_repro_cmd})",
            )
        )
    after_line = "suite gate did not run"
    if suite_after is not None:
        after_line = (
            f"regression {_repro_cmd}: {suite_after.status} — "
            f"{suite_after.output.splitlines()[0][:200] if suite_after.output else 'no output'}"
        )
    db.add(
        TaskEvent(
            task_id=task.id,
            stage="REGRESSION",
            message=f"before={before_line} after={after_line}",
        )
    )
    db.commit()
    if repro.get("sandbox") == "unavailable" or (
        suite_after is not None and suite_after.status == ERROR
    ):
        reg_status = ERROR
    elif repro["ok"]:
        reg_status = SKIPPED
    elif suite_after is not None and suite_after.status == PASS:
        reg_status = PASS
    else:
        reg_status = FAIL
    reg_output = (
        f"BEFORE (base): {before_line} | AFTER (fixed): {after_line}"
        if reg_status != SKIPPED
        else f"REPRODUCTION: NOT_REPRODUCED — suite passed before any change ({_repro_cmd})"
    )
    regression_gate = record_gate(
        db,
        task,
        "regression",
        reg_status,
        True,
        reg_output,
        signature=(
            list(suite_after.signature)
            or gate_signatures("suite", suite_after.output or "")
            if suite_after is not None
            else []
        ),
        attribution=(
            regression_attribution(
                str(repro.get("output", "")), suite_after, changed_now
            )
            if reg_status != SKIPPED
            else ATTR_NONE
        ),
    )
    results = [*results, regression_gate]
    # Attribution: compare AFTER gates against the pre-patch BASELINE so
    # pre-existing failures never send the agent back to debugging. Only new
    # task-attributed failures do.
    verdict, summary, debug_task, task_state = attribute_verdict(
        results, baseline_results, changed_now
    )
    persist_attribution(db, task.id, results)
    db.add(
        TaskEvent(
            task_id=task.id,
            stage="ATTRIBUTION",
            message=(
                f"verdict={verdict} new={len(summary.get('new', []))} "
                f"unchanged={summary.get('pre_existing', 0)} "
                f"resolved={len(summary.get('resolved', []))} "
                f"debug_task={debug_task}"
            )[:500],
        )
    )
    db.commit()
    if task_state == "READY_FOR_APPROVAL" and verdict == WITH_LIMITATIONS:
        db.add(
            TaskEvent(
                task_id=task.id,
                stage="READY_FOR_APPROVAL",
                message=(
                    "verified with limitations: "
                    f"{summary.get('pre_existing', 0)} pre-existing failure(s) "
                    "unchanged, 0 new — human approval decides"
                )[:500],
            )
        )
        db.commit()
    feedback = build_verify_feedback(results, summary, changed_now)
    transition(
        db,
        task,
        task_state,
        f"verify {verdict} new={len(summary.get('new', []))} "
        f"unchanged={summary.get('pre_existing', 0)}",
    )
    publishable = verdict in ("VERIFIED", WITH_LIMITATIONS)
    return {
        "verified": verdict == "VERIFIED",
        "publishable": publishable,
        "overall": verdict,
        "attribution": summary,
        "feedback": feedback,
        "results": results,
        "regression": {"before": before_line, "after": after_line},
    }
