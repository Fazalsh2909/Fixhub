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
        "You are Fixhub, a careful engineer. Investigate with read/search tools, "
        "then fix with edit_file (existing files) — never create_file over an "
        "existing file. Verify with run_test. Never claim a fix without test evidence."
    )
    if skills_ctx.strip():
        return base + "\n\n" + skills_ctx.strip()
    return base


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
    # 1. reproduce (real execution, never fabricated)
    transition(db, task, "REPRODUCING", "running reproduction")
    repro = run_in_sandbox(
        workdir, "pip install -q -r requirements.txt && python -m pytest tests/ -x -q"
    )
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
    for i in range(MAX_ITERS):
        try:
            resp = llm.tool_call(messages, tool_specs())
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

        for call, native in zip(valid_calls, native_calls):
            name = call["name"]
            try:
                args = json.loads(native["function"]["arguments"] or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            if name in ("run_command", "run_test"):
                out = run_command(
                    workdir, args.get("cmd") or args.get("target", "pytest -q")
                )
            elif name == "edit_file":
                from ..tools.registry import edit_file

                out = edit_file(
                    workdir,
                    args.get("path", ""),
                    args.get("old_string", ""),
                    args.get("new_string", ""),
                )
            elif name == "create_file":
                from ..tools.registry import create_file

                out = create_file(
                    workdir, args.get("path", ""), args.get("content", "")
                )
            elif name == "search_code":
                from ..intel.indexer import search_code

                out = {
                    "ok": True,
                    "output": str(search_code(workdir, args.get("pattern", ""))[:20]),
                }
            elif name == "read_file":
                from ..tools.registry import _resolve

                p = _resolve(workdir, args.get("path", ""))
                out = (
                    {
                        "ok": p.exists(),
                        "output": p.read_text(errors="ignore")[:4000]
                        if p.exists()
                        else "not found",
                    }
                    if p
                    else {"ok": False, "output": "path escapes workdir"}
                )
            elif name == "list_files":
                from ..tools.registry import _resolve

                base = _resolve(workdir, args.get("dir", "."))
                out = (
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
            else:
                out = {"ok": False, "output": "unknown tool"}
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
