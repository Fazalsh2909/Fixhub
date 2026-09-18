"""Explore subagent: search that happens somewhere else.

Four rules (same shape as the pattern's source):

1. starts from an empty history — no memory of the lead agent's transcript.
2. holds read-only tools only — list/read/search. No run, no edits, no plan
   writes, no spawning further subagents. Structural: those tools are simply
   not offered, so it cannot call them.
3. runs the same tool loop as the main agent — nothing special here.
4. only its final message comes back — everything else is discarded, so the
   lead transcript pays one answer instead of dozens of tool results.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..llm.base import LLMProvider

# Structural guarantee: a guest never holds these, whatever the prompt says.
WITHHELD = {
    "task",
    "write_todos",
    "run_command",
    "run_test",
    "edit_file",
    "create_file",
}

SYSTEM_PROMPT = """You are an exploration subagent. You were given one question by a lead agent and you answer it. That is the whole job.

You cannot see the conversation that spawned you, and the lead agent cannot see anything you do here. Only your final message crosses back, so it has to stand on its own.

You are read-only: investigate with list_files, read_file, search_code. Never try to edit or run anything.

How to work:
- Search in batches. Several greps in one turn beats one grep per turn.
- Stop as soon as you can answer. Do not keep looking to be thorough.

Keep it short — findings only: file paths with line numbers, names, values. Say plainly what you could not find; a gap is useful, a guess is not."""


def read_only_specs() -> list:
    from ..tools.registry import tool_specs

    return [s for s in tool_specs() if s.name not in WITHHELD]


def explore(workdir: Path, question: str, llm: LLMProvider, max_turns: int = 6) -> str:
    """Run a fresh agent on one question. Return only its final answer."""
    from ..llm.openrouter import ProviderError
    from ..agent.orchestrator import _execute_tool

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT + f"\n\nWorkdir: {workdir}"},
        {"role": "user", "content": (question or "")[:2000]},
    ]
    specs = read_only_specs()
    report: str | None = None
    for i in range(max(1, max_turns)):
        try:
            resp = llm.tool_call(messages, specs)
        except ProviderError as e:
            note = f"(subagent provider error: {e}. Partial findings follow.)"
            return f"{note}\n\n{report}" if report else f"{note} Nothing gathered."
        valid = [c for c in resp.tool_calls if isinstance(c, dict) and c.get("name")]
        native: list[dict] = []
        for j, call in enumerate(valid):
            raw = call.get("arguments", "{}") or "{}"
            arg_str = raw if isinstance(raw, str) else json.dumps(raw)
            native.append(
                {
                    "id": f"sub-{i}-{j}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": arg_str},
                }
            )
        messages.append(
            {"role": "assistant", "content": resp.text or "", "tool_calls": native}
        )
        report = resp.text or report
        if not native:
            return report or "(the subagent came back with nothing)"
        for call, nat in zip(valid, native):
            try:
                args = json.loads(nat["function"]["arguments"] or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            if call["name"] in WITHHELD:
                out = {"ok": False, "output": "tool not available to subagent"}
            else:
                out = _execute_tool(workdir, call["name"], args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": nat["id"],
                    "content": str(out)[:2000],
                }
            )
    if report:
        return f"(stopped after {max_turns} turns, partial.)\n\n{report}"
    return f"(stopped after {max_turns} turns with nothing to report.)"
