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
    # Bumps on every successful file-changing call. Duplicate policy keys off
    # it: identical calls are only redundant within the same version, so test
    # reruns after edits are always allowed.
    state_version: int = 0


# An identical *command* failure is never retried: the second attempt is
# blocked with a strategy-change instruction. Repeating a failed command burns
# the budget and was the observed CI failure mode (two identical `pip install`
# failures). Other tools (e.g. a write gated before diagnosis) may be retried
# once after new evidence, so they keep the larger limit.
MAX_IDENTICAL_COMMAND_FAILURES = 1
MAX_IDENTICAL_FAILURES = 2
# Cap cached results / tracked signatures (bounded memory).
MAX_TRACKED_SIGNATURES = 100
# A turn is "stalled" when it produced no new evidence: no edit, no new path,
# no new search, no new command. Two stalled turns force a convergence
# directive; three stop the run. Without this a model can alternate two
# read-only tools forever while every call looks "new".
STALL_NUDGE_AT = 2
MAX_STALLED_TURNS = 3
# A path may be read twice (head window + a second offset window). Further
# reads of the same unchanged path return a digest instead of the body.
MAX_READS_PER_PATH = 2
# Once a CI task is DIAGNOSED, read-only exploration is over: at most two
# reads are allowed before the loop demands an edit or a reproduction. This is
# what turns "15 calls, 0 edits" into an edit by call ~5.
MAX_POST_DIAGNOSIS_READS = 2
_READ_ONLY_TOOLS = frozenset(
    {"read_file", "list_directory", "search_code", "git_status", "git_diff"}
)
# Commands that only look around. After diagnosis they count against the same
# read budget as read_file: exploration is over, reproduction/edits are not.
_EXPLORATORY_COMMAND = re.compile(
    r"^\s*(?:ls|dir|find|tree|pwd|git\s+(?:status|diff|log|show|branch))\b",
    re.IGNORECASE,
)
# Budget fractions: at CONVERGE the loop tells the model to stop exploring;
# at CRITICAL it repeats the directive with the remaining work spelled out.
# The hard cap still applies - these only change behaviour earlier.
CONVERGE_FRACTION = 0.65
CRITICAL_FRACTION = 0.85

# `cat/head/tail <path>` is a read of <path> wearing a command's clothes.
_READ_ONLY_COMMAND = re.compile(
    r"^\s*(?:cat|head|tail|less|more)\s+(?:-[A-Za-z0-9]+\s+)*([^\s|;&><]+)\s*$"
)
# Commands that change working-tree content (so cached reads go stale).
_MUTATING_COMMAND = re.compile(
    r"(?:^|[\s;&|])("
    r"git\s+(?:checkout|switch|reset|clean|apply|stash|pull|clone|merge|rebase|restore)"
    r"|rm|mv|cp|mkdir|touch|patch|sed\s+-i"
    r")(?:\s|$)",
    re.IGNORECASE,
)


def _read_target(command: str) -> str:
    """Repository path read by a simple `cat/head/tail <path>` command, else ""."""
    m = _READ_ONLY_COMMAND.match(str(command or ""))
    return m.group(1).strip("\"'") if m else ""


def _norm_path(path: str) -> str:
    """Canonical repository-relative path for read-budget accounting."""
    p = str(path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _mutates_workspace(command: str) -> bool:
    """True when a shell command can change files the read cache depends on."""
    return bool(_MUTATING_COMMAND.search(str(command or "")))


def _read_digest(result: str) -> str:
    """Short fingerprint of content already shown to the model."""
    body = str(result or "")
    head = " ".join(body[:240].split())
    return f"{len(body)} chars; starts: {head}"


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
    budget_guard=None,
) -> LoopResult:
    """Run the deterministic observe -> reason -> act -> verify loop.

    `on_tool` (optional) receives each TOOL_CALL event dict immediately after
    execution (live progress persistence). `is_cancelled` (optional callable)
    is checked every iteration and before every tool call; on cancel the loop
    stops with ``LoopResult.cancelled=True``. `ctx` (optional TaskContext)
    becomes the authoritative run context; loose kwargs are kept for callers
    that build it inline. `deadline_mono` (optional monotonic timestamp, Phase
    4.5) caps this invocation against a task-wide agent budget shared with
    gate-fix rounds; the tighter of it and LLM_MAX_RUNTIME_S wins.
    `lease_guard` (optional callable -> bool, final hardening) is verified
    before every LLM request and after every tool execution; on loss the loop
    stops with ``LoopResult.lease_lost=True`` so the worker performs no
    further provider calls or side effects.
    `request_guard` (optional callable -> str, final hardening) is verified
    before every LLM request; it returns "" when another provider call is
    allowed or a non-empty limit reason when the per-user budget is spent.
    On a limit the loop stops with ``LoopResult.usage_limited=True`` without
    making the call.
    `budget_guard` (optional callable -> float in 0..1) reports the fraction
    of the task budget already consumed. It never stops the loop; at
    CONVERGE_FRACTION it injects a converge directive so a slow model spends
    its remaining calls writing instead of re-reading.
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
    from app.agent import phase as _phase
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
    tracker = _phase.InvestigationTracker(skill=ctx.skill or _skill.name)
    if ctx.trigger_type == "ci":
        if (ctx.ci.workflow_content or "").strip() and (
            ctx.ci.workflow_file or ""
        ).strip():
            tracker.seed_ci_workflow_context(ctx.ci.workflow_file)
        # The webhook/worker already fetched the real failing log. Treat it as
        # supplied evidence so the gate does not force a workflow re-read.
        if _ctxmod.has_failure_signal(ctx.ci.failure_logs):
            tracker.seed_ci_failure(
                _ctxmod.failure_signal(ctx.ci.failure_logs) or "failure-log-supplied"
            )
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
    file_changing_calls = 0
    nudges = 0
    rewrites: dict[str, int] = {}
    seen: dict[str, dict] = {}  # signature -> {fails, version, step, result}
    # Progress accounting (see STALL_* constants). `path_seen`/`search_seen`/
    # `dir_seen`/`cmd_seen` track distinct evidence, so a legitimate multi-file
    # investigation never trips the stall breaker.
    path_seen: set[str] = set()
    search_seen: set[str] = set()
    dir_seen: set[str] = set()
    cmd_seen: set[str] = set()
    read_budget: dict[str, dict] = {}  # path -> {count, version, digest}
    stalled_turns = 0
    post_diag_reads = 0
    converged = 0
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
        # request. The guard returns "" (allowed) or a limit reason.
        if request_guard is not None:
            try:
                _limit_reason = request_guard()
            except Exception:
                _limit_reason = ""
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
        # Budget-aware convergence: a stateless provider re-charges the whole
        # context on every call, so a model that keeps exploring runs out of
        # budget with nothing written. Tell it to converge while it still can.
        if budget_guard is not None:
            try:
                _frac = float(budget_guard())
            except Exception:
                _frac = 0.0
            if _frac >= CRITICAL_FRACTION and converged < 2:
                converged = 2
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"BUDGET CRITICAL: {int(_frac * 100)}% of this task's "
                            "budget is spent. Stop all exploration. Make the smallest "
                            "fix you are confident in now with edit_file/write_file, "
                            "then run one focused check. If you cannot fix it, say "
                            "exactly what blocks you - do not read another file."
                        ),
                    }
                )
                _emit(
                    {
                        "tool": "budget_converge",
                        "args": {"fraction": round(_frac, 2), "level": "critical"},
                        "ok": True,
                        "step": tool_calls,
                    }
                )
            elif _frac >= CONVERGE_FRACTION and converged < 1:
                converged = 1
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"BUDGET: {int(_frac * 100)}% of this task's budget is "
                            "spent. You already have the evidence in this "
                            "conversation. Converge: stop reading and make the fix "
                            "with edit_file/write_file, or state the single missing "
                            "fact you need in one command."
                        ),
                    }
                )
                _emit(
                    {
                        "tool": "budget_converge",
                        "args": {"fraction": round(_frac, 2), "level": "warn"},
                        "ok": True,
                        "step": tool_calls,
                    }
                )
        outbound = _window(messages, events, diagnosis)
        assistant = _llm.chat_completion(outbound, tools=_tools.TOOL_SCHEMAS)
        if not assistant.tool_calls:
            # Guard against premature "done": a text-only finish with zero
            # file changes usually means the model narrated the next step
            # instead of doing it. Nudge (bounded) rather than accept.
            if file_changing_calls == 0 and nudges < 2:
                nudges += 1
                messages.append(
                    {"role": "assistant", "content": assistant.content or ""}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have not modified any file yet. The task requires changing "
                            "the repository (add/fix code or tests) and verifying with a "
                            "command. Continue working with tool calls now — do not "
                            "summarize until changes exist, or explain with evidence why "
                            "no change is needed."
                        ),
                    }
                )
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
        # Two calls in one assistant message that resolve to the same action
        # are one action: execute the first, redirect the rest.
        batch_sigs: dict[str, int] = {}
        turn_progress = False
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
            # Phase 1 runtime gating: prohibited camouflage always blocked;
            # write/edit before sufficient investigation blocked with evidence hint.
            if tc.name in ("write_file", "edit_file"):
                _prohibited = _phase.prohibited_reason(tc.name, args)
                if _prohibited:
                    _msg = _phase.prohibited_error(_prohibited)
                    _emit(
                        {
                            "tool": tc.name,
                            "args": _summarise_args(args),
                            "ok": False,
                            "blocked": True,
                            "prohibited": True,
                            "step": tool_calls,
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": _msg}
                    )
                    state.last_failure = _msg[:500]
                    seen[_signature(tc.name, args)] = {
                        "fails": 1,
                        "version": state.state_version,
                        "step": tool_calls,
                        "result": _msg,
                        "ok": False,
                    }
                    continue
                _allowed, _missing = tracker.can_write()
                if not _allowed:
                    _msg = _phase.gated_write_error(tracker.skill, _missing)
                    _emit(
                        {
                            "tool": tc.name,
                            "args": _summarise_args(args),
                            "ok": False,
                            "blocked": True,
                            "gated": True,
                            "phase": tracker.phase,
                            "step": tool_calls,
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": _msg}
                    )
                    state.last_failure = _msg[:500]
                    seen[_signature(tc.name, args)] = {
                        "fails": 1,
                        "version": state.state_version,
                        "step": tool_calls,
                        "result": _msg,
                        "ok": False,
                    }
                    continue
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
                    messages.append(
                        {"role": "assistant", "content": assistant.content or ""}
                    )
                    return LoopResult(True, note, iterations, tool_calls, events)
            sig = _signature(tc.name, args)
            # Same-batch duplicate: two calls in one message that resolve to
            # the same action are one action.
            if sig in batch_sigs:
                note = (
                    f"DUPLICATE: same action as call #{batch_sigs[sig]} in this "
                    "message. It already ran; use that result instead of repeating it."
                )
                _emit(
                    {
                        "tool": tc.name,
                        "args": _summarise_args(args),
                        "ok": True,
                        "duplicate": True,
                        "step": tool_calls,
                    }
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": note}
                )
                continue
            batch_sigs[sig] = tool_calls
            # Per-path read budget across tools: `read_file` and `cat` return
            # the same information, so a path already read (unchanged) is not
            # re-executed. This is what stops read/cat ping-pong.
            read_path = ""
            if tc.name == "read_file":
                read_path = _norm_path(str(args.get("path", "")))
            elif tc.name == "run_command":
                read_path = _norm_path(_read_target(str(args.get("command", ""))))
            if read_path:
                rrec = read_budget.get(read_path)
                if (
                    rrec
                    and rrec["version"] == state.state_version
                    and rrec["count"] >= MAX_READS_PER_PATH
                ):
                    note = (
                        f"ALREADY READ: {read_path} (read {rrec['count']}x, workspace "
                        f"unchanged since). Cached digest: {rrec['digest']}. Do not "
                        "read it again — use the content you already have, edit a "
                        "file, or run a diagnostic."
                    )
                    _emit(
                        {
                            "tool": tc.name,
                            "args": _summarise_args(args),
                            "ok": True,
                            "blocked": True,
                            "read_budget": True,
                            "step": tool_calls,
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": note}
                    )
                    continue
            # Post-diagnosis read budget (CI): once the failure is understood,
            # more reading is not progress. Two read-only calls, then the loop
            # demands an edit or a command that reproduces the failure.
            is_read_call = (
                tc.name in _READ_ONLY_TOOLS
                or bool(read_path)
                or (
                    tc.name == "run_command"
                    and bool(
                        _EXPLORATORY_COMMAND.match(str(args.get("command", "")))
                    )
                )
            )
            if (
                tracker.skill == "fix-ci-cd"
                and tracker.phase == _phase.DIAGNOSED
                and is_read_call
                and post_diag_reads >= MAX_POST_DIAGNOSIS_READS
            ):
                msg = (
                    f"READ BUDGET EXHAUSTED: {post_diag_reads} read-only calls since "
                    "diagnosis. You already have the evidence. Your next action MUST "
                    "be an edit_file/write_file that fixes the cause, or a command "
                    "that reproduces the failure. Do not read another file."
                )
                _emit(
                    {
                        "tool": tc.name,
                        "args": _summarise_args(args),
                        "ok": False,
                        "blocked": True,
                        "read_budget": True,
                        "step": tool_calls,
                    }
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": msg}
                )
                state.last_failure = msg
                seen[sig] = {
                    "fails": 1,
                    "version": state.state_version,
                    "step": tool_calls,
                    "result": msg,
                    "ok": False,
                }
                continue
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
                    _emit(
                        {
                            "tool": tc.name,
                            "args": _summarise_args(args),
                            "ok": True,
                            "cached": True,
                            "step": tool_calls,
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": note}
                    )
                    continue
                if rec["fails"] >= (
                    MAX_IDENTICAL_COMMAND_FAILURES
                    if tc.name == "run_command"
                    else MAX_IDENTICAL_FAILURES
                ):
                    # Third identical failure: block and force a strategy change.
                    msg = (
                        "Previous action failed and repeating it is not allowed. "
                        "Choose a different diagnostic or implementation strategy: "
                        "inspect the directory structure, try another path, or run a "
                        "different command. Do not issue this exact call again."
                    )
                    _emit(
                        {
                            "tool": tc.name,
                            "args": _summarise_args(args),
                            "ok": False,
                            "blocked": True,
                            "step": tool_calls,
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": msg}
                    )
                    state.last_failure = msg
                    continue
            result = _compact_tool_result(tc.name, _execute(workspace, tc.name, args))
            meta = _parse_command_result(result) if tc.name == "run_command" else {}
            # exit_code is authoritative (null = timeout). A non-zero command
            # is a failed command, not a successful action: it must never count
            # as progress or investigation evidence.
            failed_command = bool(meta) and (
                meta.get("timed_out") is True
                or (
                    meta.get("exit_code") is not None
                    and meta.get("exit_code") != 0
                )
            )
            ok = not result.startswith("ERROR") and not failed_command
            was_diagnosed = tracker.phase == _phase.DIAGNOSED
            failure_directive = ""
            if ok:
                # Only a workspace mutation invalidates cached reads. Bumping
                # on every success (as this used to) meant any intervening
                # call - even `git_status` - discarded the whole read cache,
                # so alternating read_file/cat re-executed forever.
                mutated = tc.name in ("write_file", "edit_file") or (
                    tc.name == "run_command"
                    and _mutates_workspace(str(args.get("command", "")))
                )
                if mutated:
                    state.state_version += 1
                state.last_successful_action = tc.name
                if tc.name in ("write_file", "edit_file"):
                    p = str(args.get("path", ""))
                    if p and p not in state.changed_files:
                        state.changed_files.append(p)
                if tc.name == "run_command":
                    state.commands_run += 1
                # Phase 1 state machine bookkeeping (runtime, not prompt-only).
                # Phase 4.5: pass args so depth (distinct paths, workflow reads)
                # is tracked, not just call counts.
                tracker.record(tc.name, True, args)
                if was_diagnosed and is_read_call:
                    post_diag_reads += 1
                if tc.name in ("write_file", "edit_file"):
                    tracker.mark_implementing()
                if tc.name in ("run_command", "git_diff") and state.changed_files:
                    tracker.mark_validating()
                # Progress accounting: distinct evidence only. Status calls
                # (git_status/git_diff) never count as progress.
                if mutated:
                    turn_progress = True
                elif tc.name == "read_file":
                    p = _norm_path(str(args.get("path", "")))
                    if p and p not in path_seen:
                        path_seen.add(p)
                        turn_progress = True
                elif tc.name == "list_directory":
                    p = _norm_path(str(args.get("path", ".") or "."))
                    if p not in dir_seen:
                        dir_seen.add(p)
                        turn_progress = True
                elif tc.name == "search_code":
                    pat = str(args.get("pattern", ""))
                    if pat and pat not in search_seen:
                        search_seen.add(pat)
                        turn_progress = True
                elif tc.name == "run_command":
                    if sig not in cmd_seen:
                        cmd_seen.add(sig)
                        turn_progress = True
                if read_path:
                    rrec = read_budget.get(read_path)
                    if not rrec or rrec["version"] != state.state_version:
                        rrec = {
                            "count": 0,
                            "version": state.state_version,
                            "digest": "",
                        }
                    rrec["count"] += 1
                    rrec["digest"] = _read_digest(result)
                    read_budget[read_path] = rrec
            else:
                state.last_failure = result[:500]
                if failed_command:
                    # Reproduction is real diagnostic evidence, but a failed
                    # command is never progress toward a fix.
                    tracker.mark_reproduced()
                    if sig not in cmd_seen:
                        cmd_seen.add(sig)
                        turn_progress = True
                    failure_directive = _command_failure_directive(meta, result)
            seen[sig] = {
                "fails": (rec["fails"] + 1) if rec and not ok else (0 if ok else 1),
                "version": state.state_version,
                "step": tool_calls,
                "result": result,
                "ok": ok,
            }
            if len(seen) > MAX_TRACKED_SIGNATURES:
                seen.pop(next(iter(seen)))
            ev = {
                "tool": tc.name,
                "args": _summarise_args(args),
                "ok": ok,
                "step": tool_calls,
                "phase": tracker.phase,
                "skill": tracker.skill,
            }
            ev.update(meta)
            _emit(ev)
            state.recent_signatures.append(sig)
            del state.recent_signatures[:-50]
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if failure_directive:
                messages.append({"role": "user", "content": failure_directive})
                _emit(
                    {
                        "tool": "command_failed",
                        "args": {
                            "command": str(args.get("command", ""))[:300],
                            "exit_code": meta.get("exit_code"),
                            "timed_out": bool(meta.get("timed_out")),
                        },
                        "ok": False,
                        "failed_command": True,
                        "step": tool_calls,
                    }
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
        # A turn that produced no new evidence is a stall. Two stalls force a
        # convergence directive; three end the run with a legible reason
        # instead of burning the remaining budget on the same reads.
        if turn_progress:
            stalled_turns = 0
        else:
            stalled_turns += 1
            if stalled_turns == STALL_NUDGE_AT:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "STOP INVESTIGATING. This turn produced no new evidence: "
                            "you repeated information already in this conversation. "
                            "Your next message MUST contain an edit_file/write_file "
                            "that fixes the failure, or a single command that "
                            "reproduces it. Do not read the same file again."
                        ),
                    }
                )
                _emit(
                    {
                        "tool": "stall_nudge",
                        "args": {"turns": stalled_turns},
                        "ok": True,
                        "step": tool_calls,
                    }
                )
            if stalled_turns >= MAX_STALLED_TURNS:
                return LoopResult(
                    False,
                    (
                        f"blocked: no progress after {stalled_turns} turns "
                        f"({tool_calls} calls, {file_changing_calls} edits). The "
                        "agent repeated information it already had instead of "
                        "changing code."
                    ),
                    iterations,
                    tool_calls,
                    events,
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
            elif name == "run_command" and not ev.get("cached"):
                cmd = str(args.get("command", ""))[:100]
                commands.append(f"`{cmd}` -> exit {ev.get('exit_code', '?')}")
            if not ok and not ev.get("blocked") and not ev.get("cached"):
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


def _first_error_line(text: str) -> str:
    """First actionable error line from a command result (bounded)."""
    probes = (
        "error",
        "failed",
        "traceback",
        "exception",
        "would reformat",
        "not found",
        "no matching distribution",
        "denied",
    )
    fallback = ""
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(
            (
                "exit_code:",
                "stdout:",
                "stderr:",
                "cwd:",
                "duration_ms:",
                "timed_out:",
            )
        ):
            continue
        if not fallback:
            fallback = line
        if any(probe in line.lower() for probe in probes):
            return line[:300]
    return fallback[:300]


def _command_failure_directive(meta: dict, result: str) -> str:
    """Hard directive after a non-zero command: change strategy, do not retry."""
    code = meta.get("exit_code")
    status = "timed out" if meta.get("timed_out") else f"exit {code}"
    detail = _first_error_line(result)
    return (
        f"COMMAND FAILED ({status})"
        + (f": {detail}" if detail else "")
        + ". This is failure evidence, not progress toward a fix. The next "
        "action MUST be an edit_file/write_file that fixes the cause, or a "
        "different command that isolates it. Do NOT repeat this command."
    )


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
    if name == "nudge_no_changes":
        return "Prompted agent to continue (no changes yet)"
    if name == "validation":
        return str(args.get("note", "Validation step"))
    return name.replace("_", " ")
