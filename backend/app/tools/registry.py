"""Controlled agent tools. Structured argv policy; no shell; no host secrets; timeouts always."""

from __future__ import annotations

from pathlib import Path

from ..llm.base import ToolSpec
from ..repo.sensitive import DENIED_MESSAGE, is_sensitive
from ..sandbox.docker_runner import run_argv
from .command_policy import evaluate, evaluate_structured

# Kept for the tool-spec help text (the policy module is authoritative).
ALLOWED_PREFIXES = (
    "pytest",
    "python -m pytest",
    "npm test",
    "npm run",
    "ruff",
    "mypy",
    "tsc",
    "git ",
    "ls",
    "cat",
)
DENIED_SUBSTRINGS = ("rm -rf /", "mkfs", ":(){", "curl", "wget", "/etc/passwd", ".env")


def tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "list_files",
            "List files under a dir (read-only, safe first step)",
            {"type": "object", "properties": {"dir": {"type": "string"}}},
        ),
        ToolSpec(
            "read_file",
            "Read a workdir-relative file (read-only, max 4KB shown)",
            {"type": "object", "properties": {"path": {"type": "string"}}},
        ),
        ToolSpec(
            "search_code",
            'Regex search over *.py (read-only). Example: {"pattern": "ExpiredSignatureError"}',
            {"type": "object", "properties": {"pattern": {"type": "string"}}},
        ),
        ToolSpec(
            "run_command",
            "Run one allow-listed program in the isolated sandbox workdir — no shell, "
            'no chaining. Prefer {"program": "pytest", "args": ["-q"]}. '
            'A plain {"cmd": "python -m pytest -q"} string is also accepted and '
            "parsed to argv. Allowed programs: pytest, python -m pytest, ruff "
            "check/format, mypy, tsc, npm test/run, read-only git, ls, cat. "
            "Anything else is rejected — do not guess other commands.",
            {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "program": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        ToolSpec(
            "run_test",
            "Alias for run_command with test focus. Same policy. "
            'Example: {"program": "pytest", "args": ["tests/", "-x", "-q"]} '
            'or {"target": "pytest -q"}.',
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "cmd": {"type": "string"},
                    "program": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        ToolSpec(
            "edit_file",
            "Replace old_string with new_string in a workdir-relative file. "
            "Read the file first; old_string must match exactly once. .py edits are AST-gated.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        ),
        ToolSpec(
            "create_file",
            "Create a NEW workdir-relative file with content (fails if exists — use edit_file then)",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        ),
        ToolSpec(
            "task",
            "Hand a self-contained exploration question to a read-only explorer with its own context "
            "and get back its findings. Use this to learn how the codebase works (tracing behaviour, "
            "locating implementations) so the search costs one answer instead of many tool turns. "
            "It cannot see this conversation — include every detail it needs. It never edits.",
            {
                "type": "object",
                "properties": {"description": {"type": "string"}},
                "required": ["description"],
            },
        ),
        ToolSpec(
            "write_todos",
            "Record the plan for a multi-step task. Send the WHOLE list every time. "
            "Keep exactly one task in_progress and update it as you go.",
            {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "activeForm": {"type": "string"},
                                "status": {"type": "string"},
                            },
                            "required": ["content", "activeForm", "status"],
                        },
                    }
                },
                "required": ["todos"],
            },
        ),
    ]


def _resolve(workdir: Path, rel: str) -> Path | None:
    """Resolve rel strictly inside workdir. None = escape attempt."""
    try:
        target = (workdir / rel).resolve()
        target.relative_to(workdir.resolve())
        return target
    except (ValueError, OSError):
        return None


def read_file(workdir: Path, path: str, max_chars: int = 4000) -> dict:
    """Read a workdir-relative file. Sensitive files are denied (P0-3)."""
    if is_sensitive((path or "").strip().lstrip("/")):
        return {"ok": False, "output": DENIED_MESSAGE}
    p = _resolve(workdir, path)
    return (
        {
            "ok": p.exists(),
            "output": p.read_text(errors="ignore")[:max_chars]
            if p.exists()
            else "not found",
        }
        if p
        else {"ok": False, "output": "path escapes workdir"}
    )


def edit_file(workdir: Path, path: str, old_string: str, new_string: str) -> dict:
    if is_sensitive((path or "").strip().lstrip("/")):
        return {"ok": False, "output": DENIED_MESSAGE}
    target = _resolve(workdir, path)
    if target is None or not target.is_file():
        return {"ok": False, "output": "path escapes workdir or is not a file"}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "output": f"read failed: {e}"}
    if old_string not in text:
        return {"ok": False, "output": "old_string not found (read the file first)"}
    if text.count(old_string) > 1:
        return {"ok": False, "output": "old_string is ambiguous (N>1 matches)"}
    updated = text.replace(old_string, new_string)
    if target.suffix == ".py":
        import ast

        try:
            ast.parse(updated)
        except SyntaxError as e:
            # Verification-first at the tool level: never leave a broken file behind.
            return {
                "ok": False,
                "output": f"edit rejected: result is not valid Python ({e})",
            }
    target.write_text(updated, encoding="utf-8")
    diff = _unified_diff(path, text, updated)
    return {"ok": True, "output": f"edited {path}", "diff": diff}


def create_file(workdir: Path, path: str, content: str) -> dict:
    if is_sensitive((path or "").strip().lstrip("/")):
        return {"ok": False, "output": DENIED_MESSAGE}
    target = _resolve(workdir, path)
    if target is None:
        return {"ok": False, "output": "path escapes workdir"}
    if target.exists():
        return {"ok": False, "output": "file exists (use edit_file)"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        diff = "\n".join(f"+{line}" for line in content.splitlines())[:4000]
        return {"ok": True, "output": f"created {path}", "diff": diff}
    except OSError as e:
        return {"ok": False, "output": f"write failed: {e}"}


def _unified_diff(path: str, before: str, after: str, max_chars: int = 4000) -> str:
    """Small unified diff for UI display. Display-only — never parsed."""
    import difflib

    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    return "\n".join(lines)[:max_chars]


def run_command(
    workdir: Path,
    cmd: str = "",
    timeout: int = 180,
    *,
    program: str = "",
    args: list[str] | None = None,
) -> dict:
    """Execute one policy-approved command with no shell.

    Preferred shape is ``program`` + ``args`` (structured argv). ``cmd`` is
    accepted for back-compat and parsed with shlex into argv. Anything the
    policy rejects returns ``ok: False`` — rejections are evidence, never
    exceptions, so the agent loop records them as TOOL events.
    """
    if program.strip() or args:
        argv, reason = evaluate_structured(program, args or [])
    else:
        argv, reason = evaluate(cmd or "")
    if argv is None:
        return {"ok": False, "output": f"denied by tool policy: {reason}"}
    if argv[0] == "pytest":
        # The sandbox image carries no `pytest` console script on PATH
        # (per-task deps live under /deps via PYTHONPATH, scripts excluded),
        # so bare `pytest` dies in runc with "executable file not found".
        # The module form always resolves — normalize after approval.
        argv = ["python", "-m", "pytest", *argv[1:]]
    # Agent commands must execute inside the isolated sandbox, never on the API host.
    return run_argv(workdir, argv, timeout=timeout, require_isolation=True)
