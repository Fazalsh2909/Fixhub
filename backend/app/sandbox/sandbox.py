"""Isolated command execution. Fail-closed: no sandbox -> BLOCKED, never host fallback.

Guarantees:
- every command runs with a fixed cwd INSIDE the task workspace
- wall timeout + output cap
- env scrubbed (no LLM/GitHub secrets inherited by child processes)
- registered secrets redacted from all outputs (never leak to model context)
- destructive-pattern denylist
- path traversal blocked at the resolver (see agent/paths.py)
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass

from app.agent.paths import resolve as _resolve_cwd
from app.config import settings

# Conservative denylist: block obviously destructive / exfiltration patterns.
_DENY = [
    r"\brm\s+-rf\s+/(?:\s|$)",
    r"\bmkfs\b",
    r"\bdd\s+",
    r"\bshutdown\b",
    r"\breboot\b",
    r":\(\)\s*\{\s*:\|\:&\s*\}\s*;:",
    r"\bcurl\b.*\|\s*(?:sh|bash)",
    r"\bwget\b.*\|\s*(?:sh|bash)",
    r"\bssh\b",
    r"\bscp\b",
    r">\s*/dev/sd",
]

_SCRUB_KEYS = ("LLM_API_KEY", "GITHUB_APP_PRIVATE_KEY", "GITHUB_TOKEN", "GH_TOKEN")


@dataclass
class CommandResult:
    # exit_code is None ONLY on timeout (authoritative success/failure signal).
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool = False
    duration_ms: int = 0
    timed_out: bool = False
    cwd: str = "."
    # Backward-compat alias (existing callers/tests use .returncode).
    returncode: int | None = None

    def __post_init__(self) -> None:
        if self.returncode is None:
            self.returncode = self.exit_code if self.exit_code is not None else 124


class SandboxBlockedError(RuntimeError):
    pass


# Secret redaction registry: values registered here (e.g. per-task GitHub
# installation tokens) are scrubbed from every sandbox output so command
# results like `git remote -v` can never leak them into model context.
_REDACTED: list[str] = []


def register_secret(value: str | None) -> None:
    if value and len(value) >= 8 and value not in _REDACTED:
        _REDACTED.append(value)
        del _REDACTED[:-20]


def redact(text: str) -> str:
    for secret in _REDACTED:
        if secret and secret in text:
            text = text.replace(secret, "[REDACTED]")
    return text


def _check_allowed(command: str) -> None:
    for pat in _DENY:
        if re.search(pat, command):
            raise SandboxBlockedError(f"command blocked by sandbox policy: {pat}")


def _scrubbed_env() -> dict:
    env = dict(os.environ)
    for k in _SCRUB_KEYS:
        env.pop(k, None)
    return env


def run_command(
    workspace: str,
    command: str,
    timeout_s: int | None = None,
    cwd: str = ".",
) -> CommandResult:
    """Run a shell command with a fixed cwd inside the workspace.

    `cwd` is repository-relative and resolved through the shared jail;
    anything outside the workspace root raises SandboxBlockedError.
    Raises SandboxBlockedError on policy hit. Never falls back to host exec.
    """
    if not workspace or not os.path.isdir(workspace):
        raise SandboxBlockedError("sandbox unavailable: workspace does not exist")
    try:
        run_dir = _resolve_cwd(workspace, (cwd or ".").strip() or ".")
    except ValueError as exc:
        raise SandboxBlockedError(str(exc))
    if not os.path.isdir(run_dir):
        raise SandboxBlockedError(f"cwd is not a directory: {cwd}")
    _check_allowed(command)
    timeout = timeout_s or settings.COMMAND_TIMEOUT_S
    started = time.monotonic()
    group_kwargs: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
        else {"start_new_session": True}
    )
    # NOTE: OSError from Popen (e.g. bad cwd race) propagates like before so
    # the tool layer reports ERROR instead of crashing the loop.
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=run_dir,
        env=_scrubbed_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **group_kwargs,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the whole process TREE: orphaned grandchildren inherit the
        # pipes, and reaping output would otherwise block until THEY exit
        # (Windows pipe-inheritance deadlock — timeouts became unenforceable).
        _kill_tree(proc.pid)
        out, err = proc.communicate()
        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandResult(
            exit_code=None,
            stdout=redact(_cap(out or "")),
            stderr=redact(_cap(err or "")) + "\n[TIMEOUT]",
            truncated=True,
            duration_ms=duration_ms,
            timed_out=True,
            cwd=cwd,
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    cap = settings.TOOL_OUTPUT_MAX_BYTES
    truncated = len(out or "") + len(err or "") > cap
    return CommandResult(
        exit_code=proc.returncode,
        stdout=redact(_cap(out or "")),
        stderr=redact(_cap(err or "")),
        truncated=truncated,
        duration_ms=duration_ms,
        timed_out=False,
        cwd=cwd,
    )


def _kill_tree(pid: int | None) -> None:
    """Best-effort kill of a timed-out process TREE. Never raises."""
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        pass


def _cap(text: str) -> str:
    cap = settings.TOOL_OUTPUT_MAX_BYTES
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n...[truncated {len(text) - cap} bytes]..."
