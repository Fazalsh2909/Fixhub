"""Controlled agent tools. Allow-listed shell; no host secrets; timeouts always."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..llm.base import ToolSpec

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
            "List files under a dir",
            {"type": "object", "properties": {"dir": {"type": "string"}}},
        ),
        ToolSpec(
            "read_file",
            "Read a file",
            {"type": "object", "properties": {"path": {"type": "string"}}},
        ),
        ToolSpec(
            "search_code",
            "Regex search",
            {"type": "object", "properties": {"pattern": {"type": "string"}}},
        ),
        ToolSpec(
            "run_command",
            "Run an allow-listed command in sandbox workdir",
            {"type": "object", "properties": {"cmd": {"type": "string"}}},
        ),
        ToolSpec(
            "run_test",
            "Run focused test suite",
            {"type": "object", "properties": {"target": {"type": "string"}}},
        ),
        ToolSpec(
            "edit_file",
            "Replace old_string with new_string in a workdir-relative file",
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
            "Create a new workdir-relative file with content",
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
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {"ok": p.returncode == 0, "output": (p.stdout + p.stderr)[-8000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "timeout"}
