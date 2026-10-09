"""Phase 5 M0: sandbox backend abstraction + fail-closed dispatch.

The `SandboxBackend` ABC is the single cut point between FixHub's agent/tools
and the execution substrate. `host` preserves the current subprocess behavior
byte-for-byte (dev/test). `firecracker` routes through real microVMs (M2-M4)
and NEVER falls back to host execution: any unavailability raises
SandboxBlockedError so callers fail to BLOCKED/FAILED.

Guest APIs never accept secret parameters by design (see firecracker.py):
tokens/keys stay on the trusted host; only tokenless file trees cross vsock.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ExecResult:
    """Transport-neutral exec result. Mirrors sandbox.CommandResult fields."""

    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool = False
    duration_ms: int = 0
    timed_out: bool = False
    cwd: str = "."
    returncode: int | None = None

    def __post_init__(self) -> None:
        if self.returncode is None:
            self.returncode = self.exit_code if self.exit_code is not None else 124


class SandboxUnavailable(RuntimeError):
    """Backend cannot execute right now. Callers map to BLOCKED, never retry on host."""


class SandboxBackend(ABC):
    """Execution substrate for one task workspace."""

    name: str = "abstract"

    @abstractmethod
    def run_command(
        self,
        workspace: str,
        command: str,
        timeout_s: int | None = None,
        cwd: str = ".",
    ) -> ExecResult:
        raise NotImplementedError

    def destroy(self, workspace: str) -> None:
        """Best-effort per-workspace resource release. Never raises."""
        return None


def active_backend_name() -> str:
    """Resolve the configured backend name (host | firecracker)."""
    try:
        from app.config import settings as _settings

        name = str(getattr(_settings, "SANDBOX_BACKEND", "host") or "host")
    except Exception:
        return "host"
    return name.strip().lower() or "host"


def get_backend(name: str | None = None) -> SandboxBackend:
    """Return the backend instance. Unknown names fail closed (never default to host)."""
    from app.sandbox import sandbox as _host

    backend_name = (name or active_backend_name()).strip().lower()
    if backend_name == "host":
        return _host.HostBackend()
    if backend_name == "firecracker":
        from app.sandbox import firecracker as _fc

        return _fc.FirecrackerBackend()
    # Unknown backend string: fail closed, do NOT silently use host.
    raise _host.SandboxBlockedError(f"unknown SANDBOX_BACKEND: {backend_name!r}")


def run_command(
    workspace: str,
    command: str,
    timeout_s: int | None = None,
    cwd: str = ".",
) -> ExecResult:
    """Dispatch to the active backend. No host fallback on failure."""
    return get_backend().run_command(
        workspace, command, timeout_s=timeout_s, cwd=cwd
    )
