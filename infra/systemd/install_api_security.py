#!/usr/bin/env python3
"""Install the deployed API security drop-in (AUTH_COOKIE_SECURE=1).

The base `fixhub-api.service` unit is host-managed and not tracked, so the
repository ships a systemd drop-in instead of overwriting it. The base unit
also reads `/home/ubuntu/Fixhub/backend/.env` via `EnvironmentFile=`, and
systemd gives `EnvironmentFile=` precedence over `Environment=`. The drop-in
therefore appends a second, later `EnvironmentFile=` pointing at
`/etc/fixhub/api-security.env`; systemd reads it after the base file, so the
deployed value wins without editing the host-managed `.env`.

This installer is idempotent and fail-closed: it writes the root-owned env
file and the tracked drop-in, reloads systemd, checks the merged
`EnvironmentFile=` order, restarts only `fixhub-api`, and verifies that the
RUNNING process actually has AUTH_COOKIE_SECURE=1 and answers `/health`
before reporting success. It retries the brief window where a restarted
process has not exec'd or started listening yet; a readable wrong value fails
immediately.

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
DEFAULT_SECURITY_ENV = Path("/etc/fixhub/api-security.env")
DROPIN_NAME = "10-security.conf"
SOURCE = Path(__file__).with_name("fixhub-api.service.d-10-security.conf")
ENV_KEY = "AUTH_COOKIE_SECURE"
ENV_VALUE = "1"
SECURITY_ENV_TEXT = f"{ENV_KEY}={ENV_VALUE}\n"


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


def install_security_env(path: Path = DEFAULT_SECURITY_ENV) -> Path:
    """Write the root-owned file read after the unit's own EnvironmentFile=.

    systemd reads multiple `EnvironmentFile=` entries in order and lets the
    later file win. This file is deliberately tiny and contains no secrets; it
    exists so the deployed value overrides `backend/.env` without editing that
    host-managed file. Safe to re-run.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_text(encoding="utf-8") != SECURITY_ENV_TEXT:
        path.write_text(SECURITY_ENV_TEXT, encoding="utf-8")
    os.chmod(path, 0o600)
    if os.name == "posix" and os.geteuid() == 0:
        try:
            os.chown(path, 0, 0)
        except OSError as exc:
            raise InstallerError(
                f"could not set root ownership on {path}: {exc}"
            ) from exc
    return path


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
    if not any(
        line.strip().startswith("EnvironmentFile=")
        for line in text.splitlines()
    ):
        raise InstallerError(
            f"{source} does not append an EnvironmentFile=; without it the "
            "base unit's EnvironmentFile= would override "
            f"{ENV_KEY}={ENV_VALUE}"
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
    security_env: Path = DEFAULT_SECURITY_ENV,
    runner=run,
) -> Path:
    """Install the env file and drop-in, then reload systemd (no restart)."""
    if not unit_exists(unit, runner=runner):
        raise InstallerError(
            f"{unit}.service not found; install the base unit first"
        )
    install_security_env(security_env)
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


def read_process_env(
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
) -> dict[str, str] | None:
    """Return the environment the process was exec'd with, or None if unreadable.

    `/proc/<pid>/environ` only reflects the unit environment after execve(2).
    Immediately after `systemctl restart`, systemd has spawned the child but it
    may not have exec'd yet; its `/proc` entry then still shows the service
    manager's environment. Callers must retry instead of treating that snapshot
    as a wrong value.
    """
    environ = Path(proc_root) / str(pid) / "environ"
    try:
        raw = environ.read_bytes()
    except OSError:
        return None
    env: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry or b"=" not in entry:
            continue
        key, _, value = entry.partition(b"=")
        env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def process_env_has(
    pid: int,
    key: str = ENV_KEY,
    expected: str = ENV_VALUE,
    *,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Check the environment the running process was actually started with."""
    env = read_process_env(pid, proc_root=proc_root)
    return env is not None and env.get(key) == expected


def health_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(getattr(resp, "status", 0)) == 200
    except Exception:
        return False


def _precedence_hint(
    unit: str = DEFAULT_UNIT,
    security_env: Path = DEFAULT_SECURITY_ENV,
    *,
    runner=run,
) -> str:
    """Explain why the deployed value is still losing, using the merged unit."""
    files = environment_files(unit, runner=runner)
    # Unit files always use POSIX paths, even when the installer is exercised
    # from a Windows development checkout.
    ours = Path(security_env).as_posix()
    if ours not in files:
        return (
            f"{unit}.service does not list EnvironmentFile={ours}; the "
            "security drop-in may not be installed "
            f"(check `systemctl cat {unit}.service`)."
        )
    if files[-1] != ours:
        later = ", ".join(files[files.index(ours) + 1 :])
        return (
            f"EnvironmentFile={ours} is read before {later}; systemd lets the "
            "later file win. Rename this drop-in so it sorts after the other "
            f"drop-in (check `systemctl cat {unit}.service`)."
        )
    return (
        f"EnvironmentFile={ours} is read last, but the running process still "
        "has the wrong value. Run `systemctl daemon-reload` and restart the "
        f"service (check `systemctl show {unit}.service -p EnvironmentFiles` "
        "and `/proc/<MainPID>/environ`)."
    )


def _restart_hint(unit: str = DEFAULT_UNIT) -> str:
    return (
        f"Check `systemctl status {unit}` and "
        f"`journalctl -u {unit} -n 100 --no-pager`."
    )


def verify(
    unit: str = DEFAULT_UNIT,
    health_url: str = DEFAULT_HEALTH_URL,
    *,
    runner=run,
    proc_root: Path = Path("/proc"),
    security_env: Path = DEFAULT_SECURITY_ENV,
    health=health_ok,
    attempts: int = 60,
    interval: float = 0.5,
    sleeper=time.sleep,
) -> tuple[bool, str]:
    """Prove the live service has the setting; never infer it from the file.

    `systemctl restart` returns once systemd has spawned the process, which is
    before `/proc/<pid>/environ` is guaranteed to show the unit environment and
    before the app is ready to answer `/health`. Retry both instead of failing
    on a transient snapshot. A readable, wrong value fails immediately: retrying
    cannot change the environment of a process that has already exec'd.
    """
    attempts = max(1, attempts)
    last_problem = f"{unit}.service is not running (MainPID=0)"
    kind = "not-running"
    for attempt in range(attempts):
        pid = main_pid(unit, runner=runner)
        if pid <= 0:
            last_problem = f"{unit}.service is not running (MainPID=0)"
            kind = "not-running"
        else:
            env = read_process_env(pid, proc_root=proc_root)
            if env is None:
                last_problem = (
                    f"{unit}.service (pid {pid}) environment is not readable "
                    "(the process may still be starting or has exited)"
                )
                kind = "unreadable"
            else:
                actual = env.get(ENV_KEY)
                if actual is None:
                    last_problem = (
                        f"{unit}.service (pid {pid}) does not have "
                        f"{ENV_KEY} in its environment"
                    )
                    kind = "missing"
                elif actual != ENV_VALUE:
                    hint = _precedence_hint(unit, security_env, runner=runner)
                    return False, (
                        f"{unit}.service (pid {pid}) has "
                        f"{ENV_KEY}={actual}, expected {ENV_VALUE}. {hint}"
                    )
                elif health(health_url):
                    return True, (
                        f"{unit}.service is active with {ENV_KEY}={ENV_VALUE}; "
                        f"{health_url} responded 200"
                    )
                else:
                    last_problem = (
                        "process environment is correct but the health check "
                        f"failed: {health_url}"
                    )
                    kind = "health"
        if attempt + 1 < attempts:
            sleeper(interval)
    if kind == "missing":
        hint = _precedence_hint(unit, security_env, runner=runner)
        return False, f"{last_problem}. {hint}"
    return False, f"{last_problem}. {_restart_hint(unit)}"


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
        dest = apply_config(
            unit=args.unit,
            systemd_dir=Path(args.systemd_dir),
        )
        print(
            f"[install-api-security] installed {DEFAULT_SECURITY_ENV} "
            f"and {dest}"
        )
        files = environment_files(args.unit)
        security = DEFAULT_SECURITY_ENV.as_posix()
        if files:
            print(
                "[install-api-security] EnvironmentFile order: "
                + " -> ".join(files)
            )
        if security not in files:
            print(
                "[install-api-security] ERROR: "
                f"{args.unit}.service does not list "
                f"EnvironmentFile={security} after daemon-reload; "
                f"check `systemctl cat {args.unit}.service`",
                file=sys.stderr,
            )
            return 1
        if files[-1] != security:
            print(
                "[install-api-security] ERROR: "
                f"EnvironmentFile={security} is not read last "
                f"({', '.join(files)}); a later EnvironmentFile= would "
                "override it. Rename this drop-in so it sorts after the other "
                "drop-ins.",
                file=sys.stderr,
            )
            return 1
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
    except (InstallerError, OSError) as exc:
        print(f"[install-api-security] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
