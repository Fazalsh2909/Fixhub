"""Interactive coding sessions (OpenCode-style).

One user message → bounded tool loop (default 3 turns) → return. The frontend
auto-continues while status is `paused`, so long work streams in step by step
without holding one HTTP request open for minutes. Transcript persists as
AgentMessage rows; only the recent slice is sent to the model.

Edits apply directly to the repo workdir (like a local coding agent) through
the same jailed tools + sandbox as the issue agent. GitHub stays gated:
pushing a PR still requires the Approve flow on a verified task.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from ..llm.base import LLMProvider
from ..models import AgentMessage, AgentSession

SESSION_SYSTEM = """You are Fixhub's coding agent, working inside the user's repo workdir.

Work like this: understand the request, investigate with list_files/read_file/search_code (or hand a self-contained question to the task explorer), make the smallest edit that does the job with edit_file, then verify by running the tests with run_command (e.g. {"cmd": "python -m pytest -q"}).

Rules:
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


def message_dict(m) -> dict:
    """API shape for one AgentMessage. Args come from extra (best-effort)."""
    args: dict = {}
    try:
        import json as _json

        args = _json.loads(m.extra or "{}").get("arguments", {}) or {}
    except Exception:
        args = {}
    return {
        "id": m.id,
        "role": m.role,
        "tool": m.tool_name,
        "args": args,
        "content": m.content,
        "ok": m.ok,
    }


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
    from .orchestrator import _run_batch
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
        plain_outs = _run_batch(workdir, plain) if plain else []
        pi = 0
        for name, args, tool_id in parsed:
            if name == "task":
                question = str(args.get("description", ""))[:2000]
                if not question.strip():
                    out: dict = {"ok": False, "output": "description required"}
                else:
                    try:
                        turns = int(getattr(_settings, "subagent_max_turns", 6) or 6)
                    except Exception:
                        turns = 6
                    out = {
                        "ok": True,
                        "output": explore(workdir, question, llm, max_turns=turns)[
                            :4000
                        ],
                    }
            elif name == "write_todos":
                out = {
                    "ok": False,
                    "output": "plans belong to issue tasks — just do the work here",
                }
            else:
                out = plain_outs[pi]
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
                    extra=json.dumps({"tool_call_id": tool_id, "arguments": args}),
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
    }
