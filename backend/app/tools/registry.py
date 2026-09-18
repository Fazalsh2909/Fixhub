"""Controlled agent tools. Allow-listed shell; no host secrets; timeouts always."""

from __future__ import annotations

from pathlib import Path

from ..llm.base import ToolSpec
from ..sandbox.docker_runner import run_in_sandbox

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
    allow = ", ".join(ALLOWED_PREFIXES)
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
            f"Run an allow-listed command in sandbox workdir. Allowed prefixes: {allow}. "
            'Examples: {"cmd": "python -m pytest -q"}, {"cmd": "ruff check ."}. '
            "Anything else is rejected — do not guess other commands.",
            {"type": "object", "properties": {"cmd": {"type": "string"}}},
        ),
        ToolSpec(
            "run_test",
            f"Alias for run_command with test focus. Same allow-list: {allow}. "
            'Example: {"target": "pytest -q"} or {"cmd": "python -m pytest tests/ -x -q"}.',
            {"type": "object", "properties": {"target": {"type": "string"}}},
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
    ]


def _resolve(workdir: Path, rel: str) -> Path | None:
    """Resolve rel strictly inside workdir. None = escape attempt."""
    try:
        target = (workdir / rel).resolve()
        target.relative_to(workdir.resolve())
        return target
    except (ValueError, OSError):
        return None


def edit_file(workdir: Path, path: str, old_string: str, new_string: str) -> dict:
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
    return {"ok": True, "output": f"edited {path}"}


def create_file(workdir: Path, path: str, content: str) -> dict:
    target = _resolve(workdir, path)
    if target is None:
        return {"ok": False, "output": "path escapes workdir"}
    if target.exists():
        return {"ok": False, "output": "file exists (use edit_file)"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "output": f"created {path}"}
    except OSError as e:
        return {"ok": False, "output": f"write failed: {e}"}


def run_command(workdir: Path, cmd: str, timeout: int = 180) -> dict:
    if any(d in cmd for d in DENIED_SUBSTRINGS):
        return {"ok": False, "output": "denied by tool policy"}
    if not cmd.startswith(ALLOWED_PREFIXES):
        return {"ok": False, "output": f"command not allow-listed: {cmd[:80]}"}
    # Agent commands must execute inside the isolated sandbox, never on the API host.
    return run_in_sandbox(workdir, cmd, timeout=timeout, require_isolation=True)
