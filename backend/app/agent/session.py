"""Interactive coding sessions (OpenCode-style).

One user message → bounded tool loop (default 3 turns) → return. The frontend
auto-continues while status is `paused`, so long work streams in step by step
without holding one HTTP request open for minutes. Transcript persists as
AgentMessage rows; only the recent slice is sent to the model.

Edits apply directly to the repo workdir (like a local coding agent) through
the same jailed tools + sandbox as the issue agent. GitHub stays gated:
pushing a PR still requires the Approve flow on a verified task.

What the UI renders per turn (the opencode feel): the assistant's thinking
(reasoning_content when the provider exposes it), one row per tool call with
its args, duration and — for edits — a diff preview, plus the agent-owned
plan (write_todos) as a checklist. All of it rides on AgentMessage rows so
any client (batched POST or the live SSE stream) sees the same timeline.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from sqlalchemy.orm import Session

from ..llm.base import LLMProvider
from ..models import AgentMessage, AgentSession

SESSION_SYSTEM = """You are Fixhub's coding agent, working inside the user's repo workdir.

Work like this: understand the request, investigate with list_files/read_file/search_code (or hand a self-contained question to the task explorer), make the smallest edit that does the job with edit_file, then verify by running the tests with run_command (e.g. {"cmd": "python -m pytest -q"}).

Rules:
- Think out loud briefly before acting — one or two sentences on what you will do and why.
- For multi-step work keep a plan with write_todos (whole list each time, exactly one in_progress) and update it as you go.
- Edits apply immediately — say what you changed and what the tests said.
- Never claim a fix without running something that proves it.
- Keep answers short. File paths with line numbers, not essays.
- If you need info you don't have, ask one specific question and stop."""

HISTORY_LIMIT = 60


def _transcript(db: Session, session: AgentSession) -> list[dict]:
    import json as _json

    rows = (
        db.query(AgentMessage)
        .filter_by(session_id=session.id)
        .order_by(AgentMessage.id.desc())
        .limit(HISTORY_LIMIT)
        .all()
    )
    messages: list[dict] = [{"role": "system", "content": SESSION_SYSTEM}]
    for r in reversed(rows):
        if r.role == "plan":
            continue  # agent-owned plan lives outside the transcript; re-injected as context
        if r.role == "tool":
            try:
                call_id = _json.loads(r.extra or "{}").get(
                    "tool_call_id", f"sess-{r.id}"
                )
            except Exception:
                call_id = f"sess-{r.id}"
            messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": r.content[:4000]}
            )
        elif r.role == "assistant":
            try:
                calls = _json.loads(r.extra or "{}").get("tool_calls", [])
            except Exception:
                calls = []
            messages.append(
                {"role": "assistant", "content": r.content[:4000], "tool_calls": calls}
            )
        else:
            messages.append({"role": r.role, "content": r.content[:4000]})
    return messages


def _save(
    db: Session,
    session: AgentSession,
    role: str,
    content: str,
    tool: str = "",
    ok: bool = True,
    extra: str = "{}",
) -> AgentMessage:
    m = AgentMessage(
        session_id=session.id,
        role=role,
        tool_name=tool,
        content=content[:8000],
        ok=ok,
        extra=extra,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _changed_files(workdir: Path) -> list[str]:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        files = [
            line[3:].strip().split(" -> ")[-1]
            for line in out.splitlines()
            if line.strip()
        ]
        return files[:20]
    except Exception:
        return []


def _run_batch_timed(
    workdir: Path, calls: list[tuple[str, dict]]
) -> list[tuple[dict, int]]:
    """Same dispatch as orchestrator._run_batch, plus per-tool duration_ms."""
    from .orchestrator import _READ_ONLY, _execute_tool

    def _one(nc: tuple[str, dict]) -> tuple[dict, int]:
        start = time.monotonic()
        try:
            out = _execute_tool(workdir, nc[0], nc[1])
        except Exception as e:  # interactive turn must never die on one tool
            out = {"ok": False, "output": f"tool crashed: {e}"}
        return out, int((time.monotonic() - start) * 1000)

    if len(calls) > 1 and all(name in _READ_ONLY for name, _ in calls):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(4, len(calls))) as ex:
            return list(ex.map(_one, calls))
    return [_one(nc) for nc in calls]


def message_dict(m) -> dict:
    """API shape for one AgentMessage. Args/thinking/timing come from extra."""
    try:
        import json as _json

        extra = _json.loads(m.extra or "{}") or {}
    except Exception:
        extra = {}
    args = extra.get("arguments", {}) or {}
    todos = extra.get("todos", []) if m.role == "plan" else []
    # Engineering-event summary derived from the row itself (never private
    # reasoning): what the assistant said or which tool ran and whether it
    # worked. Old rows may still carry a `reasoning` blob — it is not served.
    if m.role == "assistant":
        summary = (m.content or "")[:160]
    elif m.role == "tool":
        summary = f"{m.tool_name} {'ok' if m.ok else 'failed'}"
    else:
        summary = ""
    return {
        "id": m.id,
        "role": m.role,
        "tool": m.tool_name,
        "args": args,
        "content": m.content,
        "ok": m.ok,
        # Opencode-style timeline fields (best-effort; missing on old rows).
        # `thinking` is intentionally always "" — private reasoning is never
        # exposed (Phase 12). Kept as a key for one release for client compat.
        "thinking": "",
        "summary": summary,
        "duration_ms": extra.get("duration_ms", 0) or 0,
        "diff": extra.get("diff", "") or "",
        "todos": todos,
    }


def latest_plan(db: Session, session: AgentSession) -> list[dict]:
    """Newest agent-owned plan for a session. [] when none written yet."""
    row = (
        db.query(AgentMessage)
        .filter_by(session_id=session.id, role="plan")
        .order_by(AgentMessage.id.desc())
        .first()
    )
    if row is None:
        return []
    try:
        import json as _json

        todos = _json.loads(row.extra or "{}").get("todos", [])
        return todos if isinstance(todos, list) else []
    except Exception:
        return []


def save_plan(db: Session, session: AgentSession, todos: list[dict]) -> dict:
    """Replace the session plan. Only the newest plan row is kept."""
    from .plan import _validate, plan_prompt_text

    if not isinstance(todos, list):
        return {"ok": False, "output": "todos must be a list"}
    err = _validate(todos)
    if err:
        return {"ok": False, "output": err}
    db.query(AgentMessage).filter_by(session_id=session.id, role="plan").delete()
    clean = [
        {
            "content": str(t.get("content", ""))[:200],
            "activeForm": str(t.get("activeForm", t.get("content", "")))[:200],
            "status": t.get("status", "pending"),
        }
        for t in todos
    ]
    prompt = plan_prompt_text(clean)
    if clean:
        db.add(
            AgentMessage(
                session_id=session.id,
                role="plan",
                content=prompt[:2000],
                ok=True,
                extra=json.dumps({"todos": clean}),
            )
        )
    db.commit()
    return {"ok": True, "output": prompt or "Todo list cleared."}


def plan_block(db: Session, session: AgentSession) -> str:
    todos = latest_plan(db, session)
    if not todos:
        return ""
    from .plan import plan_prompt_text

    return "Plan:\n" + plan_prompt_text(todos)


def run_session_turn(
    db: Session,
    session: AgentSession,
    workdir: Path,
    llm: LLMProvider,
    user_text: str | None = None,
    max_turns: int = 3,
) -> dict:
    """Run up to max_turns tool turns. Returns status done|paused|failed + new messages."""
    from ..config import settings as _settings
    from ..metrics import record_tool_call
    from ..metrics import snapshot as _metrics_snapshot
    from .context import fit, git_reminder, strip_old_outputs
    from .subagent import explore

    new_rows: list[AgentMessage] = []
    if user_text and user_text.strip():
        new_rows.append(_save(db, session, "user", user_text.strip()[:4000]))
        if session.title in ("session", "", None):
            session.title = user_text.strip()[:60]
            db.commit()

    tokens_before = _metrics_snapshot()["total_tokens"]
    try:
        cost_before = _metrics_snapshot()["est_cost_usd"]
    except Exception:
        cost_before = 0.0

    from ..llm.openrouter import ProviderError
    from ..tools.registry import tool_specs

    status = "done"
    error: str | None = None
    for _ in range(max(1, max_turns)):
        try:
            cap = float(getattr(_settings, "agent_max_cost_usd", 0.0) or 0.0)
            if cap > 0 and (_metrics_snapshot()["est_cost_usd"] - cost_before) >= cap:
                error = f"cost cap reached (${cap:.2f})"
                status = "failed"
                break
            budget = int(
                getattr(_settings, "agent_context_budget_chars", 60000) or 60000
            )
            call_messages = strip_old_outputs(fit(_transcript(db, session), budget))
            git = git_reminder(workdir)
            if git:
                call_messages = call_messages + [
                    {"role": "user", "content": "Context refresh:\n" + git[:1200]}
                ]
            plan = plan_block(db, session)
            if plan:
                call_messages = call_messages + [
                    {"role": "user", "content": "Context refresh:\n" + plan[:1200]}
                ]
            resp = llm.tool_call(call_messages, tool_specs())
        except ProviderError as e:
            error = str(e)[:500]
            status = "failed"
            break

        valid = [c for c in resp.tool_calls if isinstance(c, dict) and c.get("name")]
        # Persist the assistant row FIRST so tool call ids are stable per row
        # (rebuilt transcripts reuse them — providers require the pairing).
        assistant_row = _save(db, session, "assistant", resp.text or "")
        new_rows.append(assistant_row)
        native: list[dict] = []
        for j, call in enumerate(valid):
            raw = call.get("arguments", "{}") or "{}"
            arg_str = raw if isinstance(raw, str) else json.dumps(raw)
            native.append(
                {
                    "id": f"sess-{assistant_row.id}-{j}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": arg_str},
                }
            )
        # Phase 12: private chain-of-thought is never persisted. Only the
        # assistant's user-facing content and tool-call pairings are stored;
        # the UI renders WHAT the system did (tool rows + plan), not private
        # reasoning. `thinking` stays present-but-empty in message_dict for
        # one release so existing clients don't break on a missing key.
        assistant_row.extra = json.dumps({"tool_calls": native})
        db.commit()
        if not native:
            status = "done"
            break

        parsed: list[tuple[str, dict, str]] = []
        for call, nat in zip(valid, native):
            try:
                args = json.loads(nat["function"]["arguments"] or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            parsed.append(
                (call["name"], args if isinstance(args, dict) else {}, nat["id"])
            )
        plain = [(n, a) for n, a, _ in parsed if n not in ("task", "write_todos")]
        timed = _run_batch_timed(workdir, plain) if plain else []
        pi = 0
        for name, args, tool_id in parsed:
            duration_ms = 0
            if name == "task":
                question = str(args.get("description", ""))[:2000]
                if not question.strip():
                    out: dict = {"ok": False, "output": "description required"}
                else:
                    try:
                        turns = int(getattr(_settings, "subagent_max_turns", 6) or 6)
                    except Exception:
                        turns = 6
                    start = time.monotonic()
                    out = {
                        "ok": True,
                        "output": explore(workdir, question, llm, max_turns=turns)[
                            :4000
                        ],
                    }
                    duration_ms = int((time.monotonic() - start) * 1000)
            elif name == "write_todos":
                start = time.monotonic()
                out = save_plan(
                    db, session, args.get("todos", []) if isinstance(args, dict) else []
                )
                duration_ms = int((time.monotonic() - start) * 1000)
            else:
                out, duration_ms = timed[pi]
                pi += 1
            try:
                record_tool_call()
            except Exception:
                pass
            new_rows.append(
                _save(
                    db,
                    session,
                    "tool",
                    str(out.get("output", ""))[:8000],
                    tool=name,
                    ok=bool(out.get("ok")),
                    extra=json.dumps(
                        {
                            "tool_call_id": tool_id,
                            "arguments": args,
                            "duration_ms": duration_ms,
                            "diff": str(out.get("diff", ""))[:4000],
                        }
                    ),
                )
            )
        status = "paused"
    else:
        status = "paused" if status != "failed" else status

    tokens_used = _metrics_snapshot()["total_tokens"] - tokens_before
    return {
        "status": status,
        "error": error,
        "messages": [message_dict(m) for m in new_rows],
        "changed_files": _changed_files(workdir),
        "tokens_used": tokens_used,
        "plan": latest_plan(db, session),
    }
