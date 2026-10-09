"""Phase 5 M0/M3-unit: backend dispatch + protocol + token stripping.

No KVM required. Real-VM isolation proof lives in test_sandbox_firecracker.py
(gated on FIXHUB_FIRECRACKER_TEST=1); nothing here claims isolation.
"""

import os

from app.sandbox import backend as _backend
from app.sandbox import guest_agent as _guest
from app.sandbox import net as _net
from app.sandbox import sandbox as _sandbox


def test_unknown_backend_fails_closed(tmp_path):
    try:
        _backend.get_backend("does-not-exist")
    except _sandbox.SandboxBlockedError:
        return
    raise AssertionError("unknown backend must raise SandboxBlockedError")


def test_host_backend_runs_command(tmp_path):
    from app.sandbox.sandbox import HostBackend

    ws = str(tmp_path)
    (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
    res = HostBackend().run_command(ws, "echo ok")
    assert res.exit_code == 0
    assert "ok" in res.stdout


def test_dispatch_defaults_to_host(tmp_path, monkeypatch):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "SANDBOX_BACKEND", "host")
    ws = str(tmp_path)
    res = _backend.run_command(ws, "echo routed")
    assert res.exit_code == 0
    assert "routed" in res.stdout


def test_firecracker_backend_fails_closed_without_kvm(tmp_path, monkeypatch):
    """On this host (Windows or no KVM box) provision must BLOCK, never run on host."""
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "SANDBOX_BACKEND", "firecracker")
    from app.sandbox import firecracker as _fc

    ws = str(tmp_path)
    try:
        _fc.provision(424242, ws)
    except _sandbox.SandboxBlockedError:
        return
    except Exception as exc:
        # Any failure mode is acceptable as long as it is fail-closed.
        assert "fallback" not in str(exc).lower()
        return
    raise AssertionError("firecracker provision without KVM must not succeed silently")


def test_guest_frame_roundtrip():
    payload = _guest.build_request(op="exec", argv=["echo", "hi"], cwd=".")
    frame = _guest.encode_frame(payload)
    back, rest = _guest.decode_frame(frame)
    assert back["op"] == "exec"
    assert rest == b""
    # Unknown op rejected at build time (no silent default).
    try:
        _guest.build_request(op="rm_rf_everything")
    except ValueError:
        return
    raise AssertionError("unknown guest op must raise")


def test_token_stripping():
    dirty = (
        '[remote "origin"]\n'
        '\turl = https://x-access-token:ghp_SECRET123@github.com/o/r.git\n'
        '\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    )
    clean = _guest.strip_token_from_git_config(dirty)
    assert "ghp_SECRET123" not in clean
    assert "x-access-token" not in clean
    assert "github.com/o/r.git" in clean
    # Idempotent.
    assert _guest.strip_token_from_git_config(clean) == clean


def test_tokenless_tree_strips_git_config(tmp_path):
    src = tmp_path / "repo"
    (src / ".git").mkdir(parents=True)
    (src / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://x-access-token:TOKEN999@github.com/o/r.git\n',
        encoding="utf-8",
    )
    (src / "app.py").write_text("print(1)\n", encoding="utf-8")
    blob = _guest.make_tokenless_tree(str(src))
    assert b"TOKEN999" not in blob
    assert b"app.py" in blob


def test_forbidden_cidrs_parse():
    assert _net.check_forbidden() is True
    assert "169.254.169.254/32" in _net.FORBIDDEN_CIDRS


def test_nft_ruleset_default_deny():
    rules = _net._nft_ruleset(tap="ftap1", allow=["api.github.com"])
    assert "policy drop" in rules
    assert "169.254.169.254" in rules
    assert "dport 53 drop" in rules


def test_host_env_scrub_covers_phase5_secrets(monkeypatch):
    monkeypatch.setenv("BYNARA_API_KEY", "secret-bynara")
    monkeypatch.setenv("FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "secret-master")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    env = _sandbox._scrubbed_env()
    assert "BYNARA_API_KEY" not in env
    assert "FIXHUB_CREDENTIAL_ENCRYPTION_KEY" not in env
    assert "DATABASE_URL" not in env


def test_prod_refuses_host_backend(monkeypatch):
    """Production must never boot on the legacy host subprocess sandbox."""
    import pytest as _pytest

    from app import main as _main
    from app.config import settings as _s

    monkeypatch.setattr(_s, "ENV", "prod")
    monkeypatch.setattr(_s, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(_s, "AUTH_COOKIE_SECURE", 1)
    monkeypatch.setattr(_s, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "x")
    monkeypatch.setattr(_s, "SANDBOX_BACKEND", "host")
    with _pytest.raises(RuntimeError, match="SANDBOX_BACKEND"):
        _main._enforce_production_guards()


def test_images_verify_shape():
    from app.sandbox import images as _images

    result = _images.verify_artifacts()
    assert "ok" in result and "missing" in result and "info" in result
    # On a Windows laptop without artifacts this must report missing (fail-closed
    # signal), never claim ok.
    if os.name == "nt":
        assert result["ok"] is False
        assert any("kvm" in m for m in result["missing"])
