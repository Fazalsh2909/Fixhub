"""Per-turn context management: budget the transcript, remind of git state.

Two cheap equivalents of neural-code's history.fit/strip, applied to the
provider-native message list every turn before the LLM call:

- strip: shrink tool outputs older than KEEP_FULL_TURNS turns (the recent
  evidence stays verbatim, old output becomes a prefix + marker).
- fit: while over budget, drop the oldest assistant+tool exchange as one unit,
  so a tool result never loses the assistant message that asked for it.
  System + latest user message are never dropped.

Neither touches the DB — TaskEvents keep the full record. This only shapes
what the next LLM call sees.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

KEEP_FULL_TURNS = 4
STRIPPED_CHARS = 800


def estimate_chars(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += len(str(m.get("content") or ""))
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            total += len(str(fn.get("arguments") or ""))
    return total


def _is_tool_result(m: dict) -> bool:
    return m.get("role") == "tool"


def _opens_tool_exchange(m: dict) -> bool:
    return bool(m.get("tool_calls"))


def strip_old_outputs(
    messages: list[dict], keep_full_turns: int = KEEP_FULL_TURNS
) -> list[dict]:
    """Copy of messages with stale tool outputs truncated. Never mutates input."""
    # Find the last N tool-result indices — those stay verbatim.
    tool_idx = [i for i, m in enumerate(messages) if _is_tool_result(m)]
    keep = set(tool_idx[-keep_full_turns:]) if tool_idx else set()
    out = []
    for i, m in enumerate(messages):
        if i in keep or not _is_tool_result(m):
            out.append(m)
            continue
        content = str(m.get("content") or "")
        if len(content) > STRIPPED_CHARS:
            m = dict(m)
            m["content"] = content[:STRIPPED_CHARS] + "\n…[older tool output truncated]"
        out.append(m)
    return out


def fit(messages: list[dict], budget_chars: int) -> list[dict]:
    """Drop oldest tool exchanges until under budget. System + last user kept."""
    messages = [dict(m) for m in messages]
    while estimate_chars(messages) > budget_chars:
        # Oldest assistant message that opened a tool exchange (skip index 0).
        drop_at = next(
            (i for i in range(1, len(messages)) if _opens_tool_exchange(messages[i])),
            None,
        )
        if drop_at is None:
            break
        # Remove it plus the tool results that answer it (up to next non-tool).
        end = drop_at + 1
        while end < len(messages) - 1 and _is_tool_result(messages[end]):
            end += 1
        # Never eat the final user message.
        if end >= len(messages) and messages[-1].get("role") == "user":
            end = len(messages) - 1
        if end <= drop_at:
            break
        del messages[drop_at:end]
        if len(messages) <= 2:
            break
    return messages


def git_reminder(workdir: Path) -> str:
    """Branch + changed files, for per-turn injection. Empty when not a repo."""
    try:
        branch = subprocess.run(
            ["git", "-C", str(workdir), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if not branch:
            return ""
        status = subprocess.run(
            ["git", "-C", str(workdir), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        changed_lines = [line for line in status.splitlines() if line.strip()][:10]
        changed = "\n".join(changed_lines) if changed_lines else "(clean)"
        extra = (
            ""
            if len(status.splitlines()) <= 10
            else f"\n…+{len(status.splitlines()) - 10} more"
        )
        return f"Git: branch={branch}\nChanged files:\n{changed}{extra}"
    except Exception:
        return ""
