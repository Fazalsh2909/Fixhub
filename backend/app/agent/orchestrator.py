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
STATES = [
    "CREATED",
    "ANALYZING",
    "REPRODUCING",
    "ROOT_CAUSE_FOUND",
    "PLANNING",
    "IMPLEMENTING",
    "TESTING",
    "VERIFYING",
    "REVIEWING",
    "READY_FOR_APPROVAL",
]
MAX_ITERS = 12


def _execute_tool(workdir: Path, name: str, args: dict) -> dict:
    """Single tool dispatch (pure — safe for parallel read-only batch)."""
    if name in ("run_command", "run_test"):
        return run_command(workdir, args.get("cmd") or args.get("target", "pytest -q"))
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
        from ..tools.registry import _resolve

        p = _resolve(workdir, args.get("path", ""))
        return (
            {
                "ok": p.exists(),
                "output": p.read_text(errors="ignore")[:4000]
                if p.exists()
                else "not found",
            }
            if p
            else {"ok": False, "output": "path escapes workdir"}
        )
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


def transition(db: Session, task: Task, state: str, message: str = "") -> None:
    assert state in STATES + [
        "FAILED",
        "CANCELLED",
        "COMMITTED",
        "PUSHED",
        "PR_CREATED",
        "DEBUGGING",
    ]
    task.state = state
    db.add(TaskEvent(task_id=task.id, stage=state, message=message[:2000]))
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
    db: Session, task: Task, workdir: Path, llm: LLMProvider, name: str, args: dict
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
        report = explore(workdir, question, llm, max_turns=_settings.subagent_max_turns)
        db.add(
            TaskEvent(
                task_id=task.id, stage="SUBAGENT", message=f"done :: {report[:800]}"
            )
        )
        db.commit()
        return {"ok": True, "output": report[:4000]}
    return None


def engineer_issue(db: Session, task: Task, workdir: Path, llm: LLMProvider) -> dict:
    """Vertical-slice autonomous loop. Returns summary; persists every step so a crash resumes."""
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
    _repro_cmd = _cfg.get("suite") or "python -m pytest -q"
    repro = run_in_sandbox(workdir, _repro_cmd)
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
    loop_error: str | None = None
    consecutive_failures = 0
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
                    _run_special(db, task, workdir, llm, name, args)
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
        transition(db, task, "FAILED", f"provider error: {loop_error}")
        return {"verified": False, "results": [], "error": loop_error}
    # 3. verify
    transition(db, task, "VERIFYING", "running verification pipeline")
    from ..verify.pipeline import run_verification

    results = run_verification(db, task, workdir)
    ok = all(r[1] for r in results)
    transition(db, task, "READY_FOR_APPROVAL" if ok else "DEBUGGING", f"verify ok={ok}")
    return {"verified": ok, "results": results}
