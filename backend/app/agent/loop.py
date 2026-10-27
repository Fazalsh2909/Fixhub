"""Deterministic agent loop: observe -> reason -> act.

The LLM chooses actions; FixHub controls execution. Tool calls are executed as
requested and their results are appended to the conversation. The loop stops
only on the hard caps (iterations, runtime, usage budget), lease loss,
cancellation, or the model finishing on its own.
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
    events: list[dict] = field(
        default_factory=list
    )  # TOOL_CALL metadata for persistence
    cancelled: bool = False
    # Final hardening: set when the loop stopped because the task lost its
    # execution lease. Service maps this to FAILED/lease_lost (never a silent
    # retry) so a fenced worker never performs further side effects.
    lease_lost: bool = False
    # Final hardening: set when a per-user usage limit fired. Service maps
    # this to FAILED/usage_limit with a USAGE_LIMIT_HIT event; no further
    # provider calls are made after the limit.
    usage_limited: bool = False
    usage_limit_reason: str = ""


@dataclass
class AgentState:
    """Explicit runtime state shared with the service layer."""

    workspace_root: str = ""
    branch: str = ""
    base_commit: str = ""
    changed_files: list[str] = field(default_factory=list)
    commands_run: int = 0
    ci_attempt: int = 0
    last_failure: str = ""
    last_successful_action: str = ""


def run_agent(
    *,
    workspace: str,
    trigger_type: str,
    repository: str,
    default_branch: str = "main",
    issue_title: str = "",
    issue_body: str = "",
    ci_info: str = "",
    on_tool=None,
    is_cancelled=None,
    ctx=None,
    deadline_mono: float | None = None,
    lease_guard=None,
    request_guard=None,
) -> LoopResult:
    """Run the deterministic observe -> reason -> act loop.

    `on_tool` (optional) receives each TOOL_CALL event dict immediately after
    execution (live progress persistence). `is_cancelled` (optional callable)
    is checked every iteration and before every tool call; on cancel the loop
    stops with ``LoopResult.cancelled=True``. `ctx` (optional TaskContext)
    becomes the authoritative run context; loose kwargs are kept for callers
    that build it inline. `deadline_mono` (optional monotonic timestamp, Phase
    4.5) caps this invocation against a task-wide agent budget; the tighter
    of it and LLM_MAX_RUNTIME_S wins.
    `lease_guard` (optional callable -> bool, final hardening) is verified
    before every LLM request and after every tool execution; on loss the loop
    stops with ``LoopResult.lease_lost=True`` so the worker performs no
    further provider calls or side effects.
    `request_guard` (optional callable -> str, final hardening) is verified
    before every LLM request; it returns "" when another provider call is
    allowed or a non-empty limit reason when the per-user budget is spent.
    On a limit the loop stops with ``LoopResult.usage_limited=True`` without
    making the call.
    """
    from app.agent import context as _ctxmod

    if ctx is None:
        ctx = _ctxmod.TaskContext(
            workspace_root=workspace,
            repository=repository,
            default_branch=default_branch,
            trigger_type=trigger_type,
            issue=_ctxmod.IssueContext(title=issue_title, body=issue_body),
        )
        if trigger_type == "ci":
            ctx.ci = _ctxmod.CIContext(failure_logs=ci_info)
    # Phase 1: deterministic skill routing layered on the baseline prompt.
    # Unknown/unsupported triggers fail safely here (loop-level) as well as
    # service-level; never default to the wrong skill.
    from app.agent.skills import registry as _skills

    try:
        _skill = _skills.resolve_skill(ctx.trigger_type or trigger_type)
    except _skills.UnknownSkill as exc:
        return LoopResult(
            False, f"blocked: unknown trigger ({exc})", 0, 0, [], cancelled=False
        )
    if not ctx.skill:
        ctx.skill = _skill.name
    if not ctx.skill_instructions:
        try:
            ctx.skill_instructions = _skills.skill_prompt(_skill)
        except Exception:
            ctx.skill_instructions = ""
    workspace = ctx.workspace_root or workspace
    repository = ctx.repository or repository
    state = AgentState(
        workspace_root=workspace,
        branch=ctx.branch,
        base_commit=ctx.base_commit,
    )
    messages = [
        {
            "role": "system",
            "content": _prompt.SYSTEM_PROMPT.format(repository=repository),
        },
        {"role": "user", "content": _ctxmod.build_task_message(ctx)},
    ]
    events: list[dict] = []
    tool_calls = 0
    diagnosis = _diagnosis_brief(ctx)
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

    def _lease_ok() -> bool:
        if lease_guard is None:
            return True
        try:
            return bool(lease_guard())
        except Exception:
            return False

    def _lease_lost_result() -> LoopResult:
        _emit(
            {
                "tool": "lease_fence",
                "args": {"at": "pre_llm_request"},
                "ok": False,
                "blocked": True,
                "fenced": True,
                "step": tool_calls,
            }
        )
        return LoopResult(
            False,
            "lease lost before LLM request; stopping without side effects",
            iterations,
            tool_calls,
            events,
            lease_lost=True,
        )

    iteration_limit = int(settings.LLM_MAX_ITERATIONS)
    if (ctx.trigger_type or trigger_type) == "ci":
        iteration_limit = min(
            iteration_limit,
            max(1, int(getattr(settings, "LLM_CI_MAX_ITERATIONS", 24))),
        )

    governor = _Governor()
    for i in range(iteration_limit):
        iterations = i + 1
        if _cancelled():
            return LoopResult(
                False,
                "cancelled by user",
                iterations,
                tool_calls,
                events,
                cancelled=True,
            )
        # Final hardening: verify lease ownership before every LLM request.
        if not _lease_ok():
            return _lease_lost_result()
        # Final hardening: verify per-user usage budget before every LLM
        # request. The guard returns "" (allowed) or a limit reason. A guard
        # exception is fatal (fail closed): an unverifiable budget must never
        # authorize another paid provider call.
        if request_guard is not None:
            try:
                _limit_reason = request_guard()
            except Exception as exc:
                _limit_reason = (
                    f"budget check failed ({type(exc).__name__}); "
                    "stopping without further provider calls"
                )
            if _limit_reason:
                _emit(
                    {
                        "tool": "usage_limit",
                        "args": {"reason": _limit_reason},
                        "ok": False,
                        "blocked": True,
                        "limited": True,
                        "step": tool_calls,
                    }
                )
                return LoopResult(
                    False,
                    f"usage limit reached ({_limit_reason}); stopping without "
                    "further provider calls",
                    iterations,
                    tool_calls,
                    events,
                    usage_limited=True,
                    usage_limit_reason=_limit_reason,
                )
        elapsed = time.monotonic() - started
        cap = settings.LLM_MAX_RUNTIME_S
        if deadline_mono is not None:
            cap = min(cap, max(0.0, deadline_mono - time.monotonic()))
        # NOTE: >= (not >): coarse clocks can return identical readings, and
        # a zero/negative remaining budget must stop immediately, every time.
        if elapsed >= cap:
            return LoopResult(
                False,
                "agent stopped: max runtime exceeded",
                iterations,
                tool_calls,
                events,
            )
        outbound = _window(messages, events, diagnosis)
        assistant = _llm.chat_completion(outbound, tools=_tools.TOOL_SCHEMAS)
        if not assistant.tool_calls:
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
                return LoopResult(
                    False,
                    "cancelled by user",
                    iterations,
                    tool_calls,
                    events,
                    cancelled=True,
                )
            tool_calls += 1
            args = tc.arguments or {}
            blocked = governor.before(tc.name, args)
            if blocked is not None:
                result = blocked
                meta: dict = {}
                ok = False
                governor.blocked_step()
            else:
                cached = governor.cached(tc.name, args)
                result = (
                    cached
                    if cached is not None
                    else _compact_tool_result(
                        tc.name, _execute(workspace, tc.name, args)
                    )
                )
                meta = _parse_command_result(result) if tc.name == "run_command" else {}
                # exit_code is authoritative (null = timeout). A non-zero
                # command is a failed command, not a successful action: it must
                # never count as progress or investigation evidence.
                failed_command = bool(meta) and (
                    meta.get("timed_out") is True
                    or (
                        meta.get("exit_code") is not None
                        and meta.get("exit_code") != 0
                    )
                )
                ok = not result.startswith("ERROR") and not failed_command
                governor.after(tc.name, args, result, ok)
            if ok:
                state.last_successful_action = tc.name
                if tc.name in ("write_file", "edit_file"):
                    p = str(args.get("path", ""))
                    if p and p not in state.changed_files:
                        state.changed_files.append(p)
                if tc.name == "run_command":
                    state.commands_run += 1
            else:
                state.last_failure = result[:500]
            ev = {
                "tool": tc.name,
                "args": _summarise_args(args),
                "ok": ok,
                "step": tool_calls,
            }
            ev.update(meta)
            if blocked is not None:
                ev["blocked"] = True
                ev["governor"] = True
            _emit(ev)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            # Recovery nudge after a stretch of redundant activity; stop
            # honestly if the run cannot produce new evidence.
            note = governor.nudge()
            if note:
                messages.append({"role": "user", "content": note})
            if governor.stalled():
                _emit(
                    {
                        "tool": "stall",
                        "args": {},
                        "ok": False,
                        "blocked": True,
                        "stalled": True,
                        "step": tool_calls,
                    }
                )
                return LoopResult(
                    False,
                    "stalled: repeated actions produced no new evidence",
                    iterations,
                    tool_calls,
                    events,
                )
            # Final hardening: revalidate after every tool execution. A slow
            # tool (or a reap during execution) must not continue the batch
            # or reach the next LLM request on a dead lease.
            if not _lease_ok():
                _emit(
                    {
                        "tool": "lease_fence",
                        "args": {"at": "post_tool", "after": tc.name},
                        "ok": False,
                        "blocked": True,
                        "fenced": True,
                        "step": tool_calls,
                    }
                )
                return LoopResult(
                    False,
                    "lease lost after tool execution; stopping without " "side effects",
                    iterations,
                    tool_calls,
                    events,
                    lease_lost=True,
                )
    return LoopResult(
        False, "agent stopped: max iterations exceeded", iterations, tool_calls, events
    )


def _clip_middle(text: str, limit: int) -> str:
    """Bound context while preserving both the beginning and the diagnostic tail."""
    text = str(text or "")
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    marker = f"\n...[omitted {len(text) - limit} characters]...\n"
    if limit <= len(marker):
        return text[:limit]
    budget = limit - len(marker)
    head = int(budget * 0.65)
    tail = budget - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _message_size(message: dict) -> int:
    """Approximate serialized request size in characters (includes tool args)."""
    try:
        return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return len(str(message))


def _compact_tool_result(name: str, result: str) -> str:
    """Keep tool output useful, but prevent one log/diff from dominating context."""
    base = max(1000, int(getattr(settings, "LLM_TOOL_RESULT_MAX_CHARS", 3000) or 3000))
    limits = {
        "list_directory": min(base, 1800),
        "read_file": min(base, 3500),
        "search_code": min(base, 3000),
        "write_file": min(base, 500),
        "edit_file": min(base, 500),
        "run_command": min(base, 4000),
        "git_status": min(base, 1800),
        "git_diff": min(base, 3500),
    }
    return _clip_middle(result, limits.get(name, base))


def _compact_history_message(message: dict) -> dict:
    """Copy a historical message, omitting bulky payloads already applied to disk."""
    import copy as _copy

    out = _copy.deepcopy(message)
    if isinstance(out.get("content"), str):
        cap = 600 if out.get("role") == "assistant" and out.get("tool_calls") else 1200
        out["content"] = _clip_middle(out["content"], cap)
    calls = out.get("tool_calls") or []
    for call in calls:
        fn = call.get("function") or {}
        name = str(fn.get("name") or "")
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError):
            fn["arguments"] = "{}"
            continue
        if name == "write_file":
            content = args.get("content")
            if isinstance(content, str) and len(content) > 160:
                args["content"] = f"[{len(content)} characters omitted from history; file was written]"
        elif name == "edit_file":
            for field_name in ("old", "new"):
                value = args.get(field_name)
                if isinstance(value, str):
                    args[field_name] = _clip_middle(value, 240)
        elif name == "run_command" and isinstance(args.get("command"), str):
            args["command"] = _clip_middle(args["command"], 500)
        elif name == "search_code" and isinstance(args.get("pattern"), str):
            args["pattern"] = _clip_middle(args["pattern"], 240)
        fn["arguments"] = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    if out.get("role") == "tool" and isinstance(out.get("content"), str):
        out["content"] = _compact_tool_result("tool", out["content"])
    return out


def _fit_history_span(span: list[dict], budget: int) -> list[dict] | None:
    """Shrink a single exchange enough to fit without breaking tool-call pairing."""
    import copy as _copy

    fitted = _copy.deepcopy(span)
    tool_messages = [m for m in fitted if m.get("role") == "tool"]
    if not tool_messages:
        return fitted if sum(_message_size(m) for m in fitted) <= budget else None
    # Tool outputs share the remaining budget. Keep both ends (test failures often
    # appear at the end of stderr), and keep every response paired to its call ID.
    fixed = sum(_message_size(m) for m in fitted if m.get("role") != "tool")
    per_result = max(120, (budget - fixed - 200) // max(1, len(tool_messages)))
    for message in tool_messages:
        message["content"] = _clip_middle(str(message.get("content", "")), per_result)
    if sum(_message_size(m) for m in fitted) <= budget:
        return fitted
    return None


class _Governor:
    """General-purpose runtime safeguards (never dictates the plan).

    Observes tool activity and refuses *redundant execution*: repeated reads
    are answered from a mutation-invalidated cache, identical failing
    commands/edits are bounded, and a run with no new evidence is nudged once
    and then stopped honestly. The model remains free to choose any action.
    """

    READ_TOOLS = {
        "read_file",
        "list_directory",
        "search_code",
        "git_status",
        "git_diff",
    }
    MUTATION_TOOLS = {"write_file", "edit_file"}

    def __init__(self) -> None:
        self.read_cache: dict[str, str] = {}
        self.read_hits: dict[str, int] = {}
        self.failed_edits: dict[str, int] = {}
        self.cmd_results: dict[str, str] = {}
        self.cmd_failures: dict[str, int] = {}
        self.no_evidence = 0
        self.nudges = 0

    @staticmethod
    def _key(name: str, args: dict) -> str:
        try:
            return name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            return name + ":" + str(args)

    def before(self, name: str, args: dict) -> str | None:
        """Return a suppression/block message, or None to execute."""
        key = self._key(name, args)
        if name == "edit_file":
            if self.failed_edits.get(key, 0) >= int(
                getattr(settings, "LLM_REPEAT_SUPPRESS_FAILED_EDITS", 5)
            ):
                return (
                    "ERROR: this exact edit already failed repeatedly. Re-read the "
                    "file (or the closest lines from the last error), then make a "
                    "different edit."
                )
        if name == "run_command":
            if self.cmd_failures.get(key, 0) >= int(
                getattr(settings, "LLM_REPEAT_SUPPRESS_COMMANDS", 3)
            ):
                return (
                    "ERROR: this exact command already failed repeatedly. Change "
                    "the command, inspect the error, or take a different approach."
                )
        if name in self.READ_TOOLS:
            hits = self.read_hits.get(key, 0)
            cap = int(getattr(settings, "LLM_REPEAT_SUPPRESS_READS", 3))
            if hits > cap:
                return (
                    "ERROR: identical read suppressed (already returned "
                    f"{cap + 1} times). The workspace has not changed; use a "
                    "different path/range or act on what you have."
                )
        return None

    def cached(self, name: str, args: dict) -> str | None:
        """Cached read result (with a provenance note), or None to execute."""
        if name not in self.READ_TOOLS:
            return None
        key = self._key(name, args)
        if key not in self.read_cache:
            return None
        self.read_hits[key] = self.read_hits.get(key, 0) + 1
        return (
            self.read_cache[key]
            + "\n[cached: identical read; workspace unchanged since the first result]"
        )

    def after(self, name: str, args: dict, result: str, ok: bool) -> None:
        key = self._key(name, args)
        evidence = False
        if name in self.MUTATION_TOOLS:
            if ok:
                # A real mutation invalidates every cached read.
                self.read_cache.clear()
                self.read_hits.clear()
                self.failed_edits.pop(key, None)
                evidence = True
            else:
                if name == "edit_file":
                    self.failed_edits[key] = self.failed_edits.get(key, 0) + 1
        elif name in self.READ_TOOLS:
            if key not in self.read_cache:
                self.read_cache[key] = result
                evidence = True
            self.read_hits[key] = self.read_hits.get(key, 0) + 1
        elif name == "run_command":
            # Commands can mutate the workspace (formatters, git checkout,
            # codegen), so a cached read may be stale afterwards. Drop the
            # cached content but keep hit counts: the anti-repeat bounds and
            # stall detector must still apply.
            self.read_cache.clear()
            previous = self.cmd_results.get(key)
            if previous is None or previous != result:
                evidence = True
            self.cmd_results[key] = result
            if ok:
                self.cmd_failures.pop(key, None)
            else:
                self.cmd_failures[key] = self.cmd_failures.get(key, 0) + 1
        if evidence:
            self.no_evidence = 0
            self.nudges = 0
        else:
            self.no_evidence += 1

    def blocked_step(self) -> None:
        """A suppressed/blocked tool call produced no new evidence.

        Without this, a model looping on a suppressed read would never trip
        the stall detector (blocked calls skip `after`), spinning until the
        hard iteration cap.
        """
        self.no_evidence += 1

    def nudge(self) -> str | None:
        window = max(2, int(getattr(settings, "LLM_STALL_WINDOW", 8)))
        if self.no_evidence >= window and self.nudges < 2:
            self.nudges += 1
            return (
                "Runtime note: the last several actions produced no new evidence "
                "(identical reads/commands or rejected edits). Stop repeating them: "
                "read a different region, change the command, or make the concrete "
                "edit you already have enough information for."
            )
        return None

    def stalled(self) -> bool:
        window = max(2, int(getattr(settings, "LLM_STALL_WINDOW", 8)))
        return self.no_evidence >= 2 * window


def _diagnosis_brief(ctx) -> str:
    """One-line failure pointer that survives every compaction.

    The task message is clipped when large and older exchanges are condensed,
    so without this the model loses the error it is meant to fix and re-reads
    the same file. Deterministic, bounded, never raises.
    """
    try:
        if (getattr(ctx, "trigger_type", "") or "") != "ci":
            return ""
        from app.agent import context as _ctxmod

        ci = getattr(ctx, "ci", None)
        if ci is None:
            return ""
        parts: list[str] = []
        if ci.job:
            parts.append(f"job={str(ci.job)[:80]}")
        if ci.step:
            parts.append(f"step={str(ci.step)[:120]}")
        signal = _ctxmod.failure_signal(ci.failure_logs)
        if signal:
            parts.append(f"error={signal}")
        if ci.changed_files:
            parts.append(f"files={str(ci.changed_files)[:160]}")
        if not parts:
            return ""
        return _clip_middle("CI failure — " + "; ".join(parts), 500)
    except Exception:
        return ""


def _state_brief(events: list[dict], brief: str = "") -> str:
    """Deterministic state brief from persisted tool events (no LLM call).

    Replaces the generic "tool X ran N times" recap when older exchanges are
    condensed out of the window: files read, files changed, commands with
    exit codes, and failure counts, so a resumed context knows the story
    instead of just the statistics. `brief` carries the trigger diagnosis
    (job/step/error), which must survive compaction. Bounded; never raises.
    """
    try:
        if not events:
            return brief or "No tool calls yet."
        reads: list[str] = []
        changed: list[str] = []
        commands: list[str] = []
        failed = 0
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            name = str(ev.get("tool", "?"))
            args = ev.get("args", {}) or {}
            ok = bool(ev.get("ok", True))
            if name == "read_file" and ok:
                path = str(args.get("path", ""))[:120]
                if path and path not in reads:
                    reads.append(path)
            elif name in ("write_file", "edit_file") and ok:
                path = str(args.get("path", ""))[:120]
                if path and path not in changed:
                    changed.append(path)
            elif name == "run_command":
                cmd = str(args.get("command", ""))[:100]
                commands.append(f"`{cmd}` -> exit {ev.get('exit_code', '?')}")
            if not ok:
                failed += 1
        parts = []
        if reads:
            parts.append("Read: " + ", ".join(reads[-8:]))
        if changed:
            parts.append("Changed: " + ", ".join(changed[-8:]))
        if commands:
            parts.append("Ran: " + "; ".join(commands[-4:]))
        if failed:
            parts.append(f"Failed calls: {failed}")
        if not parts:
            return brief or "No completed tool calls yet."
        return _clip_middle(
            (brief + " | " if brief else "") + " | ".join(parts), 700
        )
    except Exception:
        return brief or "Earlier exchanges condensed."


def _window(messages: list[dict], events: list[dict], brief: str = "") -> list[dict]:
    """Token-conscious conversation window with a hard serialized-character budget.

    The provider counts prompt tokens again on every stateless request. We keep
    system/task context, a few recent exchanges, compact historical write args,
    and only bounded tool output. Older exchanges are replaced by a short recap.
    """
    max_chars = max(8000, int(getattr(settings, "LLM_CONTEXT_MAX_CHARS", 8000) or 8000))
    keep = max(1, min(4, int(getattr(settings, "LLM_HISTORY_GROUPS", 2) or 2)))
    head = [dict(m) for m in messages[:2]]
    if len(head) < 2:
        return head

    # Reserve roughly one third for recent evidence. Keep system instructions intact;
    # clip only the task payload if the incoming webhook/context is unusually large.
    system_size = _message_size(head[0])
    task_budget = max(2500, max_chars - system_size - min(5000, max_chars // 3))
    if isinstance(head[1].get("content"), str):
        head[1]["content"] = _clip_middle(head[1]["content"], task_budget)
    head_size = sum(_message_size(m) for m in head)

    rest = messages[2:]
    spans: list[list[dict]] = []
    j = 0
    while j < len(rest):
        m = rest[j]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = {tc.get("id") for tc in m["tool_calls"]}
            k = j + 1
            while (
                k < len(rest)
                and rest[k].get("role") == "tool"
                and rest[k].get("tool_call_id") in ids
            ):
                k += 1
            spans.append([_compact_history_message(x) for x in rest[j:k]])
            j = k
        else:
            spans.append([_compact_history_message(m)])
            j += 1

    selected: list[list[dict]] = []
    reserve_summary = 900 if spans else 0
    remaining = max(0, max_chars - head_size - reserve_summary)
    for span in reversed(spans):
        if len(selected) >= keep:
            break
        size = sum(_message_size(m) for m in span)
        if size <= remaining:
            selected.append(span)
            remaining -= size
            continue
        if not selected and remaining >= 1200:
            fitted = _fit_history_span(span, remaining)
            if fitted is not None:
                selected.append(fitted)
                remaining -= sum(_message_size(m) for m in fitted)
        break
    selected.reverse()

    dropped = len(spans) - len(selected)
    out = head
    if dropped > 0:
        brief = _state_brief(events, brief)
        out.append({
            "role": "user",
            "content": _clip_middle(
                f"[Earlier exchanges condensed to stay within the context budget. "
                f"Progress so far: {brief} "
                f"Use the latest outputs and current repository state.]",
                800,
            ),
        })
    for span in selected:
        out.extend(span)
    # In the unlikely event of a large single message/metadata edge case, enforce
    # the cap on the final payload while retaining the mandatory system/task head.
    while len(out) > 2 and sum(_message_size(m) for m in out) > max_chars:
        tool_idx = next((i for i in range(2, len(out)) if out[i].get("role") == "tool"), None)
        if tool_idx is None:
            break
        content = str(out[tool_idx].get("content", ""))
        out[tool_idx]["content"] = _clip_middle(content, max(120, len(content) // 2))
    return out

def _require(args: dict, *fields: str) -> str | None:
    """Validate required tool args. Returns an ERROR message or None when ok.

    Missing/empty content is rejected (never silently wipe a file), and the
    message tells the model exactly how to retry within its iteration budget.
    """
    if not isinstance(args, dict) or "_raw" in args:
        return "ERROR: arguments were not valid JSON — retry the call with proper JSON arguments"
    for f in fields:
        if (
            f not in args
            or args[f] is None
            or (isinstance(args[f], str) and not args[f].strip())
        ):
            return f'ERROR: {f} is required and must be non-empty — retry the call with "{f}" included'
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
            if err := _require(args, "path"):
                return err
            return fn(
                workspace,
                args["path"],
                int(args.get("offset", 1)),
                int(args.get("limit", 200)),
            )
        if name == "search_code":
            if err := _require(args, "pattern"):
                return err
            return fn(workspace, args["pattern"], args.get("include", ""))
        if name == "write_file":
            if err := _require(args, "path", "content"):
                return err
            return fn(workspace, args["path"], args["content"])
        if name == "edit_file":
            if err := _require(args, "path", "old", "new"):
                return err
            return fn(workspace, args["path"], args["old"], args["new"])
        if name == "run_command":
            if err := _require(args, "command"):
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
    "Changed apps/api/tests/test_x.py".
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
        return f"Wrote {args.get('path', '?')}" + (
            "" if ev.get("ok", True) else " (failed)"
        )
    if name == "edit_file":
        return f"Edited {args.get('path', '?')}" + (
            "" if ev.get("ok", True) else " (failed)"
        )
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
    return name.replace("_", " ")
