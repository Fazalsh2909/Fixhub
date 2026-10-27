#!/usr/bin/env python3
"""Install the deployed API security drop-in (AUTH_COOKIE_SECURE=1).

The base `fixhub-api.service` unit is host-managed and not tracked, so the
repository ships a systemd drop-in instead of overwriting it. This installer
is idempotent and fail-closed: it writes the tracked drop-in, reloads systemd,
restarts only `fixhub-api`, and verifies that the RUNNING process actually has
AUTH_COOKIE_SECURE=1 before reporting success.

It never edits the base unit, the worker, the database, migrations, or any
Firecracker/networking configuration.

Run as root on the host:

    sudo python3 infra/systemd/install_api_security.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_UNIT = "fixhub-api"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/health"
DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")
DROPIN_NAME = "10-security.conf"
SOURCE = Path(__file__).with_name("fixhub-api.service.d-10-security.conf")
ENV_KEY = "AUTH_COOKIE_SECURE"
ENV_VALUE = "1"


class InstallerError(RuntimeError):
    """A deployment step failed; the caller must not claim success."""


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run one system command without raising; callers inspect returncode."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def unit_exists(unit: str = DEFAULT_UNIT, *, runner=run) -> bool:
    return runner(["systemctl", "cat", f"{unit}.service"]).returncode == 0


def environment_files(unit: str = DEFAULT_UNIT, *, runner=run) -> list[str]:
    """Return the unit's EnvironmentFile= paths (systemd gives them precedence)."""
    out = runner(["systemctl", "cat", f"{unit}.service"])
    if out.returncode != 0:
        return []
    files: list[str] = []
    for raw in (out.stdout or "").splitlines():
        line = raw.strip()
        if not line.startswith("EnvironmentFile="):
            continue
        value = line.split("=", 1)[1].strip().lstrip("-")
        if value:
            files.append(value)
    return files


def install_dropin(
    *,
    source: Path = SOURCE,
    unit: str = DEFAULT_UNIT,
    systemd_dir: Path = DEFAULT_SYSTEMD_DIR,
) -> Path:
    """Write the tracked drop-in into <unit>.service.d/. Safe to re-run."""
    source = Path(source)
    if not source.is_file():
        raise InstallerError(f"tracked drop-in not found: {source}")
    text = source.read_text(encoding="utf-8")
    if f"Environment={ENV_KEY}={ENV_VALUE}" not in text:
        raise InstallerError(
            f"{source} does not set Environment={ENV_KEY}={ENV_VALUE}"
        )
    target_dir = Path(systemd_dir) / f"{unit}.service.d"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / DROPIN_NAME
    dest.write_text(text, encoding="utf-8")
    os.chmod(dest, 0o644)
    return dest


def apply_config(
    unit: str = DEFAULT_UNIT,
    *,
    source: Path = SOURCE,
    systemd_dir: Path = DEFAULT_SYSTEMD_DIR,
    runner=run,
) -> Path:
    """Install the drop-in and reload systemd (no restart)."""
    if not unit_exists(unit, runner=runner):
        raise InstallerError(
            f"{unit}.service not found; install the base unit first"
        )
    dest = install_dropin(source=source, unit=unit, systemd_dir=systemd_dir)
    reloaded = runner(["systemctl", "daemon-reload"])
    if reloaded.returncode != 0:
        detail = (reloaded.stderr or "").strip()[:300]
        raise InstallerError(f"systemctl daemon-reload failed: {detail}")
    return dest


def restart_and_wait(
    unit: str = DEFAULT_UNIT,
    *,
    runner=run,
    sleeper=time.sleep,
    attempts: int = 30,
    interval: float = 0.5,
) -> bool:
    """Restart the API and wait for systemd to report it active."""
    restarted = runner(["systemctl", "restart", f"{unit}.service"])
    if restarted.returncode != 0:
        detail = (restarted.stderr or "").strip()[:300]
        raise InstallerError(f"systemctl restart failed: {detail}")
    for _ in range(max(1, attempts)):
        active = runner(["systemctl", "is-active", f"{unit}.service"])
        if (active.stdout or "").strip() == "active":
            return True
        sleeper(interval)
    return False


def main_pid(unit: str = DEFAULT_UNIT, *, runner=run) -> int:
    out = runner(
        ["systemctl", "show", "-p", "MainPID", "--value", f"{unit}.service"]
    )
    try:
        return int((out.stdout or "").strip() or "0")
    except ValueError:
        return 0


def process_env_has(
    pid: int,
    key: str = ENV_KEY,
    expected: str = ENV_VALUE,
    *,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Check the environment the running process was actually started with."""
    environ = Path(proc_root) / str(pid) / "environ"
    try:
        raw = environ.read_bytes()
    except OSError:
        return False
    return f"{key}={expected}".encode() in raw.split(b"\0")


def health_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(getattr(resp, "status", 0)) == 200
    except Exception:
        return False


def verify(
    unit: str = DEFAULT_UNIT,
    health_url: str = DEFAULT_HEALTH_URL,
    *,
    runner=run,
    proc_root: Path = Path("/proc"),
    health=health_ok,
) -> tuple[bool, str]:
    """Prove the live service has the setting; never infer it from the file."""
    pid = main_pid(unit, runner=runner)
    if pid <= 0:
        return False, f"{unit}.service is not running (MainPID=0)"
    if not process_env_has(pid, proc_root=proc_root):
        return False, (
            f"{unit}.service (pid {pid}) does not have "
            f"{ENV_KEY}={ENV_VALUE} in its environment. If the base unit uses "
            "EnvironmentFile=, systemd gives that file precedence over the "
            "drop-in; set AUTH_COOKIE_SECURE=1 there too "
            f"(check `systemctl cat {unit}.service`)."
        )
    if not health(health_url):
        return False, (
            "process environment is correct but the health check failed: "
            f"{health_url}"
        )
    return True, (
        f"{unit}.service is active with {ENV_KEY}={ENV_VALUE}; "
        f"{health_url} responded 200"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install the tracked AUTH_COOKIE_SECURE=1 drop-in for the "
            "deployed FixHub API, reload systemd, restart it, and verify."
        )
    )
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--systemd-dir", default=str(DEFAULT_SYSTEMD_DIR))
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="install + daemon-reload only; skip restart and live verification",
    )
    args = parser.parse_args(argv)

    if os.name == "posix" and os.geteuid() != 0:
        print(
            "[install-api-security] ERROR: must run as root "
            "(systemd unit installation)",
            file=sys.stderr,
        )
        return 1

    try:
        files = environment_files(args.unit)
        if files:
            print(
                "[install-api-security] NOTE: "
                f"{args.unit}.service uses EnvironmentFile= "
                f"({', '.join(files)}); systemd gives those files precedence "
                "over Environment=. Live verification will catch a conflict."
            )
        dest = apply_config(
            unit=args.unit,
            systemd_dir=Path(args.systemd_dir),
        )
        print(f"[install-api-security] installed {dest}")
        if args.no_restart:
            print(
                "[install-api-security] daemon-reload done; restart skipped. "
                f"Run: systemctl restart {args.unit}"
            )
            return 0
        if not restart_and_wait(args.unit):
            print(
                f"[install-api-security] ERROR: {args.unit}.service did not "
                "become active; check "
                f"`systemctl status {args.unit}` and "
                f"`journalctl -u {args.unit} -n 100 --no-pager`",
                file=sys.stderr,
            )
            return 1
        ok, detail = verify(args.unit, args.health_url)
        if not ok:
            print(f"[install-api-security] ERROR: {detail}", file=sys.stderr)
            return 1
        print(f"[install-api-security] OK: {detail}")
        return 0
    except InstallerError as exc:
        print(f"[install-api-security] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
