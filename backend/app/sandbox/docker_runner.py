"""Docker sandbox runner. Per-task container, scrubbed env, limits, always cleanup.

Execution is argv-based end to end: approved commands run via ``shell=False``
locally or as direct ``docker run … program args`` with NO ``sh -c``. There
is no shell=True anywhere in the agent command path.

NOT a microVM — documented limitation. Falls back to local subprocess when
docker is unavailable (same interface, flagged `sandbox: local-fallback`).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from ..config import settings


def docker_available() -> bool:
    """True only when the docker CLI exists AND the daemon answers.

    shutil.which alone is not enough on Windows/Mac where Docker Desktop
    can be installed but stopped — every gate would then FAIL with a
    daemon-connection traceback instead of clean BLOCKED.
    """
    if shutil.which("docker") is None:
        return False
    try:
        p = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return p.returncode == 0
    except Exception:
        return False


# Locked in-container path for per-task dependency installs. The ONLY
# --target the command policy ever allows (see tools/command_policy.py).
DEPS_DIR = "/deps"

# Local agent image with git + toolchain (infra/sandbox.Dockerfile).
# Build it with: docker build -f infra/sandbox.Dockerfile -t fixhub-sandbox:local infra/
AGENT_IMAGE = "fixhub-sandbox:local"


def preferred_agent_image(configured: str = "", fallback: str = "") -> str:
    """Image for the coding agent: explicit config wins, then the local
    sandbox image when built, else the slim fallback. Fail-open to fallback —
    a missing image must never block a run (Docker reports it clearly)."""
    if (configured or "").strip():
        return configured.strip()
    try:
        p = subprocess.run(
            ["docker", "image", "inspect", AGENT_IMAGE],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if p.returncode == 0:
            return AGENT_IMAGE
    except Exception:
        pass
    return (fallback or "").strip() or "python:3.11-slim"


def deps_volume_for_task(task_id: int) -> str:
    return f"fixhub-deps-task-{int(task_id)}"


def _volume_for_workdir(workdir: Path) -> str | None:
    """Derive the per-task/session deps volume from an isolated workspace
    path (…/tasks/task-<id>, …/sessions/session-<id>). Shared bases and
    demo dirs yield None — no volume, no behavior change."""
    try:
        parts = workdir.resolve().parts
    except OSError:
        return None
    for i, part in enumerate(parts):
        if part in ("tasks", "sessions") and i + 1 < len(parts):
            leaf = parts[i + 1]
            prefix, _, num = leaf.partition("-")
            if prefix in ("task", "session") and num.isdigit():
                return f"fixhub-deps-{prefix}-{int(num)}"
    return None


def run_argv(
    workdir: Path,
    argv: list[str],
    timeout: int | None = None,
    *,
    require_isolation: bool = True,
    deps_volume: str | None = None,
) -> dict:
    """Run an already-approved argv with no shell. Docker preferred; local
    subprocess (shell=False) only when isolation is not required.

    Containers are ephemeral (--rm), so a pip install in one gate call would
    vanish before the next gate runs. Pass deps_volume (a per-task named
    volume) to persist a `--target /deps` install across gates of the same
    task: the volume mounts at /deps and PYTHONPATH picks it up. Repo pins
    in /deps shadow the baked toolchain (PYTHONPATH precedes site-packages).
    When deps_volume is None, it is derived from the workspace path
    (tasks/task-<id> → fixhub-deps-task-<id>), so agent tool calls share the
    same dep store as verification with no caller changes.
    """
    if deps_volume is None:
        deps_volume = _volume_for_workdir(workdir)
    timeout = timeout or settings.sandbox_timeout_s
    start = time.monotonic()

    def _ms() -> int:
        return int((time.monotonic() - start) * 1000)

    if not docker_available():
        if require_isolation:
            return {
                "ok": False,
                "output": "Docker isolation is required for agent execution",
                "sandbox": "unavailable",
                "duration_ms": _ms(),
            }
        try:
            p = subprocess.run(
                argv,
                shell=False,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "ok": p.returncode == 0,
                "output": (p.stdout + p.stderr)[-8000:],
                "sandbox": "local-fallback",
                "duration_ms": _ms(),
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "output": "timeout",
                "sandbox": "local-fallback",
                "duration_ms": _ms(),
            }
        except OSError as e:
            return {
                "ok": False,
                "output": f"exec failed: {e}",
                "sandbox": "local-fallback",
                "duration_ms": _ms(),
            }
    # Real path: throwaway container, no host network creds, capped resources.
    # A named pip-cache volume keeps repeat runs fast (wheels cached) while the
    # container itself stays ephemeral (--rm). Cached wheels are public packages
    # only; no secrets ever enter the container env.
    # NOTE: argv is passed directly — no `sh -c`, so shell operators in
    # arguments are inert data, and policy forbids them anyway.
    subprocess.run(
        ["docker", "volume", "create", "fixhub-pip-cache"],
        capture_output=True,
        timeout=30,
    )
    if deps_volume:
        subprocess.run(
            ["docker", "volume", "create", deps_volume],
            capture_output=True,
            timeout=30,
        )
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--cpus",
        "2",
        "--memory",
        "2g",
        "--pids-limit",
        "256",
        "--network",
        "bridge",
        "-v",
        f"{workdir}:/work",
        "-v",
        "fixhub-pip-cache:/root/.cache/pip",
        *(["-v", f"{deps_volume}:{DEPS_DIR}"] if deps_volume else []),
        "-w",
        "/work",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        *(["--env", f"PYTHONPATH={DEPS_DIR}"] if deps_volume else []),
        settings.sandbox_image,
        *argv,
    ]
    try:
        p = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr)[-8000:]
        # Daemon died between docker_available() and docker run: report
        # unavailable (→ BLOCKED, no token burn) instead of FAIL.
        if p.returncode != 0 and (
            "failed to connect to the docker API" in out
            or "Cannot connect to the Docker daemon" in out
            or "error during container init" in out
            or "failed to create task for container" in out
            or "Is the docker daemon running" in out
        ):
            return {
                "ok": False,
                "output": out,
                "sandbox": "unavailable",
                "duration_ms": _ms(),
            }
        return {
            "ok": p.returncode == 0,
            "output": out,
            "sandbox": "docker",
            "duration_ms": _ms(),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "output": "timeout",
            "sandbox": "docker",
            "duration_ms": _ms(),
        }


def run_in_sandbox(
    workdir: Path,
    cmd: str,
    timeout: int | None = None,
    *,
    require_isolation: bool = False,
    deps_volume: str | None = None,
) -> dict:
    """String entry point (verification configs, terminal). The string is
    parsed to argv and validated by the command policy first — anything the
    policy rejects never reaches a shell (and no shell exists here at all)."""
    from ..tools.command_policy import evaluate

    argv, reason = evaluate(cmd)
    if argv is None:
        return {
            "ok": False,
            "output": f"denied by tool policy: {reason}",
            "sandbox": "policy",
            "duration_ms": 0,
        }
    return run_argv(
        workdir,
        argv,
        timeout=timeout,
        require_isolation=require_isolation,
        deps_volume=deps_volume,
    )


def remove_deps_volume(name: str) -> bool:
    """Best-effort removal of a per-task deps volume (task cleanup). Never
    raises — a missing docker daemon just leaves the volume behind."""
    try:
        p = subprocess.run(
            ["docker", "volume", "rm", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return p.returncode == 0
    except Exception:
        return False
