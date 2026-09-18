"""Docker sandbox runner. Per-task container, scrubbed env, limits, always cleanup.

NOT a microVM — documented limitation. Falls back to local subprocess when
docker is unavailable (same interface, flagged `sandbox: local-fallback`).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..config import settings


def docker_available() -> bool:
    return shutil.which("docker") is not None


def run_in_sandbox(
    workdir: Path,
    cmd: str,
    timeout: int | None = None,
    *,
    require_isolation: bool = False,
) -> dict:
    timeout = timeout or settings.sandbox_timeout_s
    if not docker_available():
        if require_isolation:
            return {
                "ok": False,
                "output": "Docker isolation is required for agent execution",
                "sandbox": "unavailable",
            }
        try:
            p = subprocess.run(
                cmd,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "ok": p.returncode == 0,
                "output": (p.stdout + p.stderr)[-8000:],
                "sandbox": "local-fallback",
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": "timeout", "sandbox": "local-fallback"}
    # Real path: throwaway container, no host network creds, capped resources.
    # A named pip-cache volume keeps repeat runs fast (wheels cached) while the
    # container itself stays ephemeral (--rm). Cached wheels are public packages
    # only; no secrets ever enter the container env.
    subprocess.run(
        ["docker", "volume", "create", "fixhub-pip-cache"],
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
        "none",
        "-v",
        f"{workdir}:/work",
        "-v",
        "fixhub-pip-cache:/root/.cache/pip",
        "-w",
        "/work",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        settings.sandbox_image,
        "sh",
        "-c",
        cmd,
    ]
    try:
        p = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": p.returncode == 0,
            "output": (p.stdout + p.stderr)[-8000:],
            "sandbox": "docker",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "timeout", "sandbox": "docker"}
