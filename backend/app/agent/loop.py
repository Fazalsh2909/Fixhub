"""Deterministic agent loop: observe -> reason -> act -> verify.

The LLM chooses actions; FixHub controls execution. Every tool call passes a
duplicate/failure policy evaluated against explicit AgentState BEFORE
execution, every result updates state before the next call, and stop
conditions (solved / caps / repetition threshold / security block / cancel)
are checked per call, not just per turn.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from app.agent import prompt as _prompt
from app.agent import tools as _tools
from app.config import settings
from app.llm import client as _llm


@dataclass
class LoopResult:
    finished: bool
    summary: str
    iterations: int
    tool_calls: int = 0
    events: list[dict] = field(default_factory=list)  # TOOL_CALL metadata for persistence
    cancelled: bool = False


@dataclass
class AgentState:
    """Explicit runtime state: the loop reasons from this, never asks the
    model to rediscover it."""

    workspace_root: str = ""
    branch: str = ""
    base_commit: str = ""
    changed_files: list[str] = field(default_factory=list)
    recent_signatures: list[str] = field(default_factory=list)
    commands_run: int = 0
    validations: list[str] = field(default_factory=list)
    ci_attempt: int = 0
    last_failure: str = ""
    last_successful_action: str = ""
    memory_context: str = ""
    # Bumps on every successful file-changing call. Duplicate policy keys off
    # it: identical calls are only redundant within the same version, so test
    # reruns after edits are always allowed.
    state_version: int = 0


# Identical failure repeated this many times (without state change) is
# blocked with a strategy-change instruction instead of executed again.
MAX_IDENTICAL_FAILURES = 2
# Cap cached results / tracked signatures (bounded memory).
MAX_TRACKED_SIGNATURES = 100


def run_agent(
    *,
    workspace: str,
    trigger_type: str,
    repository: str,
    default_branch: str = "main",
    issue_title: str = "",
    issue_body: str = "",
    ci_info: str = "",
    memory_overview: str = "",
    on_tool=None,
    is_cancelled=None,
    ctx=None,
) -> LoopResult:
    """Run the deterministic observe -> reason -> act -> verify loop.

    `on_tool` (optional) receives each TOOL_CALL event dict immediately after
    execution (live progress persistence). `is_cancelled` (optional callable)
    is checked every iteration and before every tool call; on cancel the loop
    stops with ``LoopResult.cancelled=True``. `ctx` (optional TaskContext)
    becomes the authoritative run context; loose kwargs are kept for callers
    that build it inline.
    """
    from app.agent import context as _ctxmod

    if ctx is None:
        ctx = _ctxmod.TaskContext(
            workspace_root=workspace, repository=repository,
            default_branch=default_branch, trigger_type=trigger_type,
            issue=_ctxmod.IssueContext(title=issue_title, body=issue_body),
            memory_overview=memory_overview,
        )
        if trigger_type == "ci":
            ctx.ci = _ctxmod.CIContext(failure_logs=ci_info)
    workspace = ctx.workspace_root or workspace
    repository = ctx.repository or repository
    state = AgentState(
        workspace_root=workspace, branch=ctx.branch, base_commit=ctx.base_commit,
        memory_context=ctx.memory_overview,
    )
    messages = [
        {"role": "system", "content": _prompt.SYSTEM_PROMPT.format(repository=repository)},
        {"role": "user", "content": _ctxmod.build_task_message(ctx)},
    ]
    events: list[dict] = []
    tool_calls = 0
    file_changing_calls = 0
    nudges = 0
    rewrites: dict[str, int] = {}
    seen: dict[str, dict] = {}  # signature -> {fails, version, step, result}
    started = time.monotonic()
    iterations = 0

    def _emit(ev: dict) -> None:
        ev["summary"] = summarize_event(ev)
        events.append(ev)
        if on_tool is not None:
            try:
                on_tool(ev)
            except Exception:
                pass

    def _cancelled() -> bool:
        try:
            return bool(is_cancelled and is_cancelled())
        except Exception:
            return False

    for i in range(settings.LLM_MAX_ITERATIONS):
        iterations = i + 1
        if _cancelled():
            return LoopResult(False, "cancelled by user", iterations, tool_calls, events, cancelled=True)
        if time.monotonic() - started > settings.LLM_MAX_RUNTIME_S:
            return LoopResult(False, "agent stopped: max runtime exceeded", iterations, tool_calls, events)
        outbound = _window(messages, events)
        assistant = _llm.chat_completion(outbound, tools=_tools.TOOL_SCHEMAS)
        if not assistant.tool_calls:
            # Guard against premature "done": a text-only finish with zero
            # file changes usually means the model narrated the next step
            # instead of doing it. Nudge (bounded) rather than accept.
            if file_changing_calls == 0 and nudges < 2:
                nudges += 1
                messages.append({"role": "assistant", "content": assistant.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        "You have not modified any file yet. The task requires changing "
                        "the repository (add/fix code or tests) and verifying with a "
                        "command. Continue working with tool calls now — do not "
                        "summarize until changes exist, or explain with evidence why "
                        "no change is needed."
                    ),
                })
                _emit({"tool": "nudge_no_changes", "args": {"n": nudges}, "ok": True})
                continue
            summary = (assistant.content or "").strip()[: settings.LLM_MAX_OUTPUT_BYTES]
            messages.append({"role": "assistant", "content": assistant.content or ""})
            return LoopResult(True, summary or "done", iterations, tool_calls, events)
        # execute tools
        messages.append(
            {
                "role": "assistant",
                "content": assistant.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in assistant.tool_calls
                ],
            }
        )
        for tc in assistant.tool_calls:
            if _cancelled():
                return LoopResult(False, "cancelled by user", iterations, tool_calls, events, cancelled=True)
            tool_calls += 1
            args = tc.arguments or {}
            # Rewrite cap first: identical rewrites must still converge-stop.
            if tc.name in ("write_file", "edit_file"):
                file_changing_calls += 1
                path = str(args.get("path", ""))
                rewrites[path] = rewrites.get(path, 0) + 1
                if rewrites[path] > settings.LLM_MAX_REWRITES_PER_PATH:
                    note = (
                        f"stopped: {path} rewritten {rewrites[path]} times without converging; "
                        f"finishing with current state for review"
                    )
                    messages.append({"role": "assistant", "content": assistant.content or ""})
                    return LoopResult(True, note, iterations, tool_calls, events)
            sig = _signature(tc.name, args)
            rec = seen.get(sig)
            if rec is not None and rec["version"] != state.state_version:
                rec = None  # workspace state changed: history no longer applies
            if rec is not None:
                if rec["ok"]:
                    # Identical success, nothing changed since: reuse, don't re-execute.
                    note = (
                        f"(same action as step {rec['step']}: workspace unchanged since — "
                        f"cached result reused. If you need fresh information, change a "
                        f"file or vary the action.)\n{rec['result']}"
                    )
                    _emit({"tool": tc.name, "args": _summarise_args(args),
                           "ok": True, "cached": True, "step": tool_calls})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": note})
                    continue
                if rec["fails"] >= MAX_IDENTICAL_FAILURES:
                    # Third identical failure: block and force a strategy change.
                    msg = (
                        "Previous action failed and repeating it is not allowed. "
                        "Choose a different diagnostic or implementation strategy: "
                        "inspect the directory structure, try another path, or run a "
                        "different command. Do not issue this exact call again."
                    )
                    _emit({"tool": tc.name, "args": _summarise_args(args),
                           "ok": False, "blocked": True, "step": tool_calls})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": msg})
                    state.last_failure = msg
                    continue
            result = _execute(workspace, tc.name, args)
            ok = not result.startswith("ERROR")
            meta = _parse_command_result(result) if tc.name == "run_command" and ok else {}
            if ok:
                state.state_version += 1
                state.last_successful_action = tc.name
                if tc.name in ("write_file", "edit_file"):
                    p = str(args.get("path", ""))
                    if p and p not in state.changed_files:
                        state.changed_files.append(p)
                if tc.name == "run_command":
                    state.commands_run += 1
            else:
                state.last_failure = result[:500]
            seen[sig] = {"fails": (rec["fails"] + 1) if rec and not ok else (0 if ok else 1),
                         "version": state.state_version if ok else state.state_version,
                         "step": tool_calls, "result": result, "ok": ok}
            if len(seen) > MAX_TRACKED_SIGNATURES:
                seen.pop(next(iter(seen)))
            ev = {"tool": tc.name, "args": _summarise_args(args), "ok": ok, "step": tool_calls}
            ev.update(meta)
            _emit(ev)
            state.recent_signatures.append(sig)
            del state.recent_signatures[:-50]
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return LoopResult(False, "agent stopped: max iterations exceeded", iterations, tool_calls, events)


def _window(messages: list[dict], events: list[dict]) -> list[dict]:
    """Sliding conversation window: system + task message always, then the most
    recent tool-exchange blocks. Older blocks are replaced by a one-line
    summary (built from persisted event metadata) instead of being resent
    every call — this is what keeps input tokens from snowballing on long runs.

    Only whole assistant+tool-response blocks are ever dropped, so tool_call
    ids always have their matching responses.
    """
    keep = max(1, settings.LLM_HISTORY_GROUPS)
    head = messages[:2]
    rest = messages[2:]
    # Split rest into spans: lone message, or assistant-with-tool_calls plus
    # all following tool responses.
    spans: list[list[dict]] = []
    j = 0
    while j < len(rest):
        m = rest[j]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = {tc.get("id") for tc in m["tool_calls"]}
            k = j + 1
            while k < len(rest) and rest[k].get("role") == "tool" and rest[k].get("tool_call_id") in ids:
                k += 1
            spans.append(rest[j:k])
            j = k
        else:
            spans.append([m])
            j += 1
    if len(spans) <= keep:
        return messages
    dropped = len(spans) - keep
    tool_names: dict[str, int] = {}
    for ev in events:
        tool_names[ev.get("tool", "?")] = tool_names.get(ev.get("tool", "?"), 0) + 1
    recap = ", ".join(f"{n} {t}" for t, n in sorted(tool_names.items(), key=lambda x: -x[1])[:6])
    summary = {
        "role": "user",
        "content": (
            f"[context condensed: {dropped} earlier step(s) omitted to save tokens. "
            f"Tool calls so far — {recap or 'none'}. Continue from the latest state below.]"
        ),
    }
    out = head + [summary]
    for span in spans[-keep:]:
        out.extend(span)
    return out


def _require(args: dict, *fields: str) -> str | None:
    """Validate required tool args. Returns an ERROR message or None when ok.

    Missing/empty content is rejected (never silently wipe a file), and the
    message tells the model exactly how to retry within its iteration budget.
    """
    if not isinstance(args, dict) or "_raw" in args:
        return "ERROR: arguments were not valid JSON — retry the call with proper JSON arguments"
    for f in fields:
        if f not in args or args[f] is None or (isinstance(args[f], str) and not args[f].strip()):
            return f"ERROR: {f} is required and must be non-empty — retry the call with \"{f}\" included"
    return None


def _execute(workspace: str, name: str, args: dict) -> str:
    fn = _tools.DISPATCH.get(name)
    if not fn:
        return f"ERROR: unknown tool: {name}"
    try:
        if name in ("git_status", "git_diff"):
            return fn(workspace)
        if name == "list_directory":
            return fn(workspace, args.get("path", "."))
        if name == "read_file":
            if (err := _require(args, "path")):
                return err
            return fn(workspace, args["path"], int(args.get("offset", 1)), int(args.get("limit", 200)))
        if name == "search_code":
            if (err := _require(args, "pattern")):
                return err
            return fn(workspace, args["pattern"], args.get("include", ""))
        if name == "write_file":
            if (err := _require(args, "path", "content")):
                return err
            return fn(workspace, args["path"], args["content"])
        if name == "edit_file":
            if (err := _require(args, "path", "old", "new")):
                return err
            return fn(workspace, args["path"], args["old"], args["new"])
        if name == "run_command":
            if (err := _require(args, "command")):
                return err
            return fn(workspace, args["command"], args.get("cwd", ".") or ".")
        return f"ERROR: unhandled tool: {name}"
    except (ValueError, OSError) as exc:
        return f"ERROR: {exc}"


def _summarise_args(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        s = str(v)
        out[k] = s[:200] + ("..." if len(s) > 200 else "")
    return out


def _signature(name: str, args: dict) -> str:
    """Normalized call signature for duplicate detection.

    File contents are length-hashed (rewrites of the same path with different
    content are distinct calls; the rewrite cap handles convergence). `cwd`
    participates so the same command in different directories is distinct.
    """
    canon: dict[str, str] = {}
    for k in sorted((args or {})):
        v = args[k]
        if k in ("content", "old", "new") and isinstance(v, str):
            canon[k] = f"<{len(v)} chars>"
        else:
            canon[k] = str(v)[:200]
    if name == "run_command":
        canon.setdefault("cwd", ".")
    return name + ":" + json.dumps(canon, sort_keys=True)


def _parse_command_result(text: str) -> dict:
    """Structured fields from our own stable rendering (tools._format_command_result).

    Returns {} when the text is not a command result (e.g. ERROR lines).
    """
    if not text.startswith("exit_code:"):
        return {}
    out: dict = {}
    m = re.search(r"^exit_code:\s*(null|\d+)", text, re.MULTILINE)
    out["exit_code"] = None if not m or m.group(1) == "null" else int(m.group(1))
    m = re.search(r"^cwd:\s*(.+)$", text, re.MULTILINE)
    if m:
        out["cwd"] = m.group(1).strip()[:200]
    m = re.search(r"^duration_ms:\s*(\d+)", text, re.MULTILINE)
    if m:
        out["duration_ms"] = int(m.group(1))
    m = re.search(r"^timed_out:\s*(true|false)", text, re.MULTILINE)
    if m:
        out["timed_out"] = m.group(1) == "true"
    return out


def summarize_event(ev: dict) -> str:
    """One-line operational summary for UI display (no chain-of-thought).

    Examples: "Read apps/api/app/core/config.py", "Ran pytest tests - 2 failed",
    "Changed apps/api/tests/test_x.py", "Local validation passed".
    """
    name = ev.get("tool", "?")
    args = ev.get("args", {}) or {}
    if name == "read_file":
        return f"Read {args.get('path', '?')}"
    if name == "list_directory":
        return f"Listed {args.get('path', '.')}"
    if name == "search_code":
        return f"Searched for `{str(args.get('pattern', ''))[:80]}`"
    if name == "write_file":
        return f"Wrote {args.get('path', '?')}" + ("" if ev.get("ok", True) else " (failed)")
    if name == "edit_file":
        return f"Edited {args.get('path', '?')}" + ("" if ev.get("ok", True) else " (failed)")
    if name == "run_command":
        code = ev.get("exit_code", "?")
        cmd = str(args.get("command", ""))[:100]
        if ev.get("timed_out"):
            return f"Ran `{cmd}` — timed out"
        if code is None and not ev.get("ok", True):
            return f"Ran `{cmd}` — blocked/failed"
        return f"Ran `{cmd}` — exit {code}"
    if name == "git_status":
        return "Checked git status"
    if name == "git_diff":
        return "Inspected git diff"
    if name == "nudge_no_changes":
        return "Prompted agent to continue (no changes yet)"
    if name == "validation":
        return str(args.get("note", "Validation step"))
    return name.replace("_", " ")
