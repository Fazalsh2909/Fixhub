"""Deployed API security configuration: secure auth cookie + systemd delivery.

These tests cover the failure mode where a staging/production API refuses to
boot because the deployed process never received AUTH_COOKIE_SECURE=1:

- the guard still refuses an insecure deployed configuration (never bypassed),
- the cookie's Secure attribute follows the setting at runtime,
- local development stays permissive,
- the tracked systemd drop-in and installer actually deliver the setting.
"""

from __future__ import annotations

import importlib.util
import shlex
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
DROPIN = REPO_ROOT / "infra" / "systemd" / "fixhub-api.service.d-10-security.conf"
INSTALLER = REPO_ROOT / "infra" / "systemd" / "install_api_security.py"

GOOD_PW = "test-password-123"


def _installer():
    spec = importlib.util.spec_from_file_location("install_api_security", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Runner:
    """Deterministic stand-in for systemctl, recording the command order."""

    def __init__(
        self,
        *,
        unit_present: bool = True,
        active_after: int = 0,
        main_pid: str = "4242",
    ) -> None:
        self.unit_present = unit_present
        self.active_after = active_after
        self.main_pid = main_pid
        self.calls: list[list[str]] = []
        self._active_checks = 0

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if cmd[:2] == ["systemctl", "cat"]:
            rc = 0 if self.unit_present else 1
            out = "[Service]\n" if self.unit_present else ""
            return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
        if cmd[:2] == ["systemctl", "is-active"]:
            self._active_checks += 1
            state = "active" if self._active_checks > self.active_after else "activating"
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{state}\n", stderr="")
        if cmd[:3] == ["systemctl", "show", "-p"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{self.main_pid}\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _service_environment(path: Path) -> dict[str, str]:
    """Parse Environment= lines from the [Service] section (systemd syntax)."""
    env: dict[str, str] = {}
    in_service = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_service = line[1:-1].strip().lower() == "service"
            continue
        if in_service and line.lower().startswith("environment="):
            for token in shlex.split(line.split("=", 1)[1]):
                key, _, value = token.partition("=")
                env[key] = value
    return env


def _cookie_flags(response) -> set[str]:
    header = response.headers.get("set-cookie", "")
    return {part.strip().lower() for part in header.split(";")[1:]}


# --- startup guard: still fails closed ----------------------------------------


def test_staging_refuses_insecure_cookie(monkeypatch):
    from app import main as _main

    monkeypatch.setattr(settings, "ENV", "staging")
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", 0)
    monkeypatch.setattr(settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "x")
    with pytest.raises(RuntimeError, match="insecure auth cookie"):
        _main._enforce_production_guards()


def test_staging_accepts_secure_cookie(monkeypatch):
    """The guard passes once the deployed setting is actually present."""
    from app import config as _cfg
    from app import main as _main
    from app.sandbox import images as _images

    monkeypatch.setattr(settings, "ENV", "staging")
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", 1)
    monkeypatch.setattr(settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "x")
    monkeypatch.setattr(settings, "SANDBOX_BACKEND", "firecracker")
    # Isolate the cookie guard from unrelated ladder/usage/sandbox checks.
    monkeypatch.setattr(_cfg, "validate_usage_limits", lambda _s: None)
    monkeypatch.setattr(_cfg, "validate_timeout_ladder", lambda _s: None)
    monkeypatch.setattr(
        _images, "verify_artifacts", lambda: {"ok": True, "missing": []}
    )
    _main._enforce_production_guards()  # must not raise


def test_prod_accepts_secure_cookie(monkeypatch):
    """Production passes the same guard once the setting is present."""
    from app import config as _cfg
    from app import main as _main
    from app.sandbox import images as _images

    monkeypatch.setattr(settings, "ENV", "prod")
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", 1)
    monkeypatch.setattr(settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "x")
    monkeypatch.setattr(settings, "SANDBOX_BACKEND", "firecracker")
    monkeypatch.setattr(_cfg, "validate_usage_limits", lambda _s: None)
    monkeypatch.setattr(_cfg, "validate_timeout_ladder", lambda _s: None)
    monkeypatch.setattr(
        _images, "verify_artifacts", lambda: {"ok": True, "missing": []}
    )
    _main._enforce_production_guards()  # must not raise


def test_dev_allows_insecure_cookie(monkeypatch):
    from app import main as _main

    monkeypatch.setattr(settings, "ENV", "dev")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", 0)
    _main._enforce_production_guards()  # must not raise


# --- cookie behavior: Secure follows the same setting -------------------------


def test_login_cookie_secure_flag_follows_setting(db, monkeypatch):
    from tests.conftest import make_user

    make_user(db, email="cookie@example.com", password=GOOD_PW)
    client = TestClient(app)
    payload = {"email": "cookie@example.com", "password": GOOD_PW}

    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", 1)
    r = client.post("/api/auth/login", json=payload)
    assert r.status_code == 200, r.text
    flags = _cookie_flags(r)
    assert "secure" in flags
    assert "httponly" in flags  # cookie protections are not weakened

    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", 0)
    r = client.post("/api/auth/login", json=payload)
    assert r.status_code == 200, r.text
    flags = _cookie_flags(r)
    assert "secure" not in flags
    assert "httponly" in flags


# --- tracked deployment configuration -----------------------------------------


def test_tracked_dropin_sets_secure_cookie():
    env = _service_environment(DROPIN)
    assert env.get("AUTH_COOKIE_SECURE") == "1"


def test_installer_applies_dropin_and_reloads(tmp_path):
    mod = _installer()
    runner = _Runner()

    dest = mod.apply_config(unit="fixhub-api", systemd_dir=tmp_path, runner=runner)

    assert dest == tmp_path / "fixhub-api.service.d" / "10-security.conf"
    text = dest.read_text(encoding="utf-8")
    assert "Environment=AUTH_COOKIE_SECURE=1" in text
    assert ["systemctl", "daemon-reload"] in runner.calls

    # Idempotent: a second run leaves the same installed configuration.
    again = mod.apply_config(unit="fixhub-api", systemd_dir=tmp_path, runner=runner)
    assert again.read_text(encoding="utf-8") == text


def test_installer_refuses_without_base_unit(tmp_path):
    mod = _installer()
    with pytest.raises(mod.InstallerError, match="not found"):
        mod.apply_config(
            unit="fixhub-api",
            systemd_dir=tmp_path,
            runner=_Runner(unit_present=False),
        )


def test_restart_is_controlled_and_waits_for_active():
    mod = _installer()
    runner = _Runner(active_after=1)

    assert (
        mod.restart_and_wait(unit="fixhub-api", runner=runner, sleeper=lambda _s: None)
        is True
    )
    assert runner.calls[0] == ["systemctl", "restart", "fixhub-api.service"]
    assert any(call[:2] == ["systemctl", "is-active"] for call in runner.calls)

    stuck = _Runner(active_after=99)
    assert (
        mod.restart_and_wait(
            unit="fixhub-api", runner=stuck, sleeper=lambda _s: None, attempts=2
        )
        is False
    )


def test_process_env_has_reads_running_environment(tmp_path):
    mod = _installer()
    environ = tmp_path / "4242" / "environ"
    environ.parent.mkdir()
    environ.write_bytes(b"ENV=staging\0AUTH_COOKIE_SECURE=1\0")

    assert mod.process_env_has(4242, proc_root=tmp_path) is True
    assert mod.process_env_has(4242, expected="0", proc_root=tmp_path) is False
    assert mod.process_env_has(9999, proc_root=tmp_path) is False


def test_verify_fails_closed_without_live_env(tmp_path):
    mod = _installer()
    proc = tmp_path / "proc"
    environ = proc / "4242" / "environ"
    environ.parent.mkdir(parents=True)
    environ.write_bytes(b"ENV=staging\0AUTH_COOKIE_SECURE=0\0")
    runner = _Runner()

    ok, detail = mod.verify(
        "fixhub-api",
        "http://127.0.0.1:8000/health",
        runner=runner,
        proc_root=proc,
        health=lambda _url: True,
    )
    assert ok is False
    assert "EnvironmentFile" in detail  # actionable hint, not a silent success

    environ.write_bytes(b"ENV=staging\0AUTH_COOKIE_SECURE=1\0")
    ok, detail = mod.verify(
        "fixhub-api",
        "http://127.0.0.1:8000/health",
        runner=runner,
        proc_root=proc,
        health=lambda _url: True,
    )
    assert ok is True
    assert "responded 200" in detail

    ok, detail = mod.verify(
        "fixhub-api",
        "http://127.0.0.1:8000/health",
        runner=runner,
        proc_root=proc,
        health=lambda _url: False,
    )
    assert ok is False
    assert "health check" in detail
