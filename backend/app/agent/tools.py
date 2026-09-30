"""Smallest useful tool set: 8 tools, all jailed to the task workspace.

- paths are repo-relative; absolute paths and `..` escapes are rejected
- sensitive files (.env, keys, secrets) are never returned
- outputs are bounded (TOOL_OUTPUT_MAX_BYTES); large results truncate with notice
"""
from __future__ import annotations

import os
import subprocess

from app.agent.paths import is_sensitive as _is_sensitive
from app.agent.paths import resolve as _resolve
from app.config import settings
from app.sandbox import sandbox as _sandbox


def _cap(text: str) -> str:
    """Bound output length AND redact registered secrets (no leakage to model)."""
    text = _sandbox.redact(text)
    cap = settings.TOOL_OUTPUT_MAX_BYTES
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n...[truncated {len(text) - cap} bytes]..."


def list_directory(workspace: str, path: str = ".") -> str:
    full = _resolve(workspace, path)
    if not os.path.isdir(full):
        return f"ERROR: not a directory: {path}"
    try:
        entries = sorted(os.listdir(full))
    except OSError as exc:
        return f"ERROR: {exc}"
    lines = []
    for e in entries[:500]:
        p = os.path.join(full, e)
        lines.append(f"{e}/" if os.path.isdir(p) else e)
    note = "" if len(entries) <= 500 else f"\n...[{len(entries) - 500} more entries truncated]"
    return "\n".join(lines) + note if lines else "(empty directory)"


def read_file(workspace: str, path: str, offset: int = 1, limit: int = 200) -> str:
    if _is_sensitive(path):
        return "ERROR: access to sensitive file is blocked"
    full = _resolve(workspace, path)
    if not os.path.isfile(full):
        return f"ERROR: file not found: {path}"
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return f"ERROR: {exc}"
    total = len(lines)
    start = max(1, offset)
    chunk = lines[start - 1 : start - 1 + limit]
    body = "".join(chunk)
    header = f"--- {path} lines {start}-{start + len(chunk) - 1} of {total} ---\n"
    return _cap(header + body)


def search_code(workspace: str, pattern: str, include: str = "") -> str:
    """Regex search via `rg` (fallback: grep). Returns path:line snippets, bounded."""
    root = os.path.abspath(workspace)
    rg = shutil_which("rg")
    try:
        if rg:
            cmd = [rg, "--no-heading", "--line-number", "--max-count", "40", pattern, root]
            if include:
                cmd += ["-g", include]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            out = proc.stdout
        else:
            cmd = ["grep", "-rn", "--exclude-dir=.git", "-m", "40", pattern, root]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            out = proc.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"ERROR: search failed: {exc}"
    if not out.strip():
        return "(no matches)"
    # relativise + drop sensitive hits
    lines = []
    for ln in out.splitlines()[:120]:
        rel = ln.replace(root + os.sep, "", 1) if ln.startswith(root) else ln
        if _is_sensitive(rel.split(":")[0]):
            continue
        lines.append(rel)
    return _cap("\n".join(lines) or "(no matches)")


def shutil_which(name: str) -> str | None:
    import shutil as _sh

    return _sh.which(name)


def write_file(workspace: str, path: str, content: str) -> str:
    if _is_sensitive(path):
        return "ERROR: writing to sensitive file is blocked"
    full = _resolve(workspace, path)
    try:
        os.makedirs(os.path.dirname(full) or full, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        return f"ERROR: {exc}"
    return f"WROTE {path} ({len(content)} bytes)"


def edit_file(workspace: str, path: str, old: str, new: str) -> str:
    if _is_sensitive(path):
        return "ERROR: editing sensitive file is blocked"
    full = _resolve(workspace, path)
    if not os.path.isfile(full):
        return f"ERROR: file not found: {path}"
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
    except OSError as exc:
        return f"ERROR: {exc}"
    if old not in body:
        return "ERROR: oldString not found"
    if body.count(old) > 1:
        return "ERROR: oldString matches multiple locations; provide more context"
    body = body.replace(old, new, 1)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(body)
    return f"EDITED {path}"


def _format_command_result(res) -> str:
    """Stable machine-readable rendering. Contract (parsed by agent/loop.py):

    exit_code: <int|null>   (null ONLY on timeout; authoritative success signal)
    cwd: <repo-relative dir the command ran in>
    duration_ms: <int>
    timed_out: <true|false>
    --- stdout --- / --- stderr --- sections, bounded.
    """
    code = "null" if res.exit_code is None else str(res.exit_code)
    timed = "true" if res.timed_out else "false"
    tail = "\n[output truncated]" if res.truncated else ""
    return (
        f"exit_code: {code}\n"
        f"cwd: {res.cwd}\n"
        f"duration_ms: {res.duration_ms}\n"
        f"timed_out: {timed}\n"
        f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}{tail}"
    )


def run_command(workspace: str, command: str, cwd: str = ".") -> str:
    """Run a shell command with a fixed repository-relative working directory.

    No `cd` needed and never `pwd`: pass e.g. cwd="apps/api". Absolute and
    escaping cwds are rejected; the command runs nowhere else.
    """
    try:
        res = _sandbox.run_command(workspace, command, cwd=cwd)
    except _sandbox.SandboxBlockedError as exc:
        return f"ERROR: blocked: {exc}"
    return _format_command_result(res)


def _git_direct(workspace: str, *args: str, cap: int = 8000) -> str:
    """Run git without shell pipes (Windows-safe) and truncate in Python."""
    import subprocess as _sp

    try:
        proc = _sp.run(
            ["git", *args], cwd=workspace, capture_output=True, text=True, timeout=30,
            env={k: v for k, v in __import__("os").environ.items()},
        )
    except (OSError, _sp.TimeoutExpired) as exc:
        return f"ERROR: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > cap:
        out = out[:cap] + f"\n...[truncated {len(out) - cap} bytes]..."
    return f"exit_code: {proc.returncode}\n{out}"


def git_status(workspace: str) -> str:
    return _git_direct(workspace, "status", "--porcelain=v1", "-uall")


def git_diff(workspace: str) -> str:
    # bounded diff: stat + capped unified diff
    stat = _git_direct(workspace, "diff", "--stat", cap=4000)
    diff = _git_direct(workspace, "diff", cap=16000)
    return f"{stat}\n{diff}"


# OpenAI function-calling schemas for the loop.
# Every path/cwd is repository-relative. Absolute paths, /tmp paths, runner
# paths, and workspace-root prefixes are rejected by the tool layer.
_REL = " Repository-relative path, e.g. \"apps/api/app/main.py\". Never absolute, never /tmp or runner paths, never prepend the workspace root."

TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {"name": "list_directory", "description": "List files in a directory." + _REL, "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}, "required": []}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a chunk of a file." + _REL, "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "default": 1}, "limit": {"type": "integer", "default": 200}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "search_code", "description": "Regex-search code under the repository root.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "include": {"type": "string", "default": ""}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create/overwrite a file." + _REL, "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Exact-string edit of a file." + _REL, "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run a shell command with a fixed repository-relative working directory (no cd needed, never pwd). Returns structured exit_code/stdout/stderr/cwd/duration_ms/timed_out; exit_code is authoritative.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string", "default": "."}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "git_status", "description": "git status porcelain of the repository.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "git_diff", "description": "Bounded git diff of the repository.", "parameters": {"type": "object", "properties": {}, "required": []}}},
]

DISPATCH = {
    "list_directory": list_directory,
    "read_file": read_file,
    "search_code": search_code,
    "write_file": write_file,
    "edit_file": edit_file,
    "run_command": run_command,
    "git_status": git_status,
    "git_diff": git_diff,
}
