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
    rules = "\n".join(_net._nft_rules(tap="ftap1", proxy_port=8443))
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


# --- Gate 0: boot-path correctness (no KVM needed; pure layout/policy) ---


def test_jail_layout_official_shape(monkeypatch, tmp_path):
    """Jail root/api-socket/pidfile follow the official jailer layout."""
    from app.sandbox import firecracker as _fc

    monkeypatch.setattr(_fc._images, "firecracker_binary", lambda: "/usr/local/bin/firecracker")
    base = str(tmp_path / "jails")
    monkeypatch.setattr(
        "app.sandbox.firecracker._cfg",
        lambda name, default="": base if name == "FC_CHROOT_BASE" else default,
    )
    chroot = _fc.chroot_dir_for(7)
    assert chroot == os.path.join(base, "firecracker", "task-7", "root")
    assert _fc.api_socket_for(chroot) == os.path.join(chroot, "api.socket")
    # PID file is <exec>.pid inside the jail root (official, all modes).
    assert _fc.pid_file_for(chroot) == os.path.join(chroot, "firecracker.pid")
    assert _fc.jail_id_for(7) == "task-7"


def test_cid_single_source_of_truth():
    """Guest CID is deterministic per task and never the hardcoded 3."""
    from app.sandbox import firecracker as _fc

    assert _fc._cid_for(91001) == 100 + (91001 % 50000)
    assert _fc._cid_for(91001) == _fc._cid_for(91001)
    assert _fc._cid_for(91001) != 3
    cids = {_fc._cid_for(i) for i in range(91001, 91101)}
    assert len(cids) == 100  # no collisions in a realistic window
    assert all(3 <= c < (1 << 32) for c in cids)


def test_cgroup_args_fail_closed_on_unknown_layout(monkeypatch):
    from app.sandbox import firecracker as _fc

    monkeypatch.setattr(_fc, "cgroup_version", lambda: "")
    try:
        _fc.jailer_cgroup_args(vcpu=2, mem_mib=1024, pids_max=256)
    except RuntimeError:
        return
    raise AssertionError("undetectable cgroup layout must refuse boot")


def test_cgroup_args_v2_shape(monkeypatch):
    from app.sandbox import firecracker as _fc

    monkeypatch.setattr(_fc, "cgroup_version", lambda: "2")
    version, args = _fc.jailer_cgroup_args(vcpu=2, mem_mib=1024, pids_max=256)
    assert version == "2"
    blob = " ".join(args)
    assert "cpu.max=200000 100000" in blob
    assert f"memory.max={1024 * 1024 * 1024}" in blob
    assert "pids.max=256" in blob
    # The old bogus form must be gone.
    assert "cpus=" not in blob


def test_overlay_cap_rejects_oversize_base(monkeypatch, tmp_path):
    from app.sandbox import firecracker as _fc

    big = tmp_path / "base.ext4"
    big.write_bytes(b"\x00" * (2 * 1024 * 1024))
    monkeypatch.setattr(_fc._images, "rootfs_image", lambda: str(big))
    monkeypatch.setattr(
        "app.sandbox.firecracker._cfg",
        lambda name, default="": "1" if name == "FC_OVERLAY_MB" else default,
    )
    try:
        _fc._prepare_overlay(str(tmp_path / "overlay.ext4"))
    except RuntimeError as exc:
        assert "exceeds overlay cap" in str(exc)
        return
    raise AssertionError("oversize base rootfs must refuse boot")


def test_nft_rules_proxy_only_no_open_443():
    """No bare `tcp dport 443 accept`: everything funnels to the proxy."""
    from app.sandbox import net as _net

    rules = "\n".join(_net._nft_rules(tap="ftap9", proxy_port=8443))
    assert "dnat to 10.200.0.1:8443" in rules
    assert "tcp dport 443 drop" in rules
    # The forward chain itself is default-deny.
    assert "policy drop" in dict(_net._chain_specs())["out"]
    for line in rules.splitlines():
        if "tcp dport 443 accept" in line:
            raise AssertionError(f"open-443 bypass in nft rules: {line}")
    # Per-task chains (multi-tenant safe).
    assert "pre_ftap9" in rules and "out_ftap9" in rules


def test_egress_allowlist_matching():
    from app.sandbox import egress as _egress

    allow = ["api.github.com", "pypi.org"]
    assert _egress.host_allowed("api.github.com", allow) is True
    assert _egress.host_allowed("sub.api.github.com", allow) is True
    assert _egress.host_allowed("evilapi.github.com", allow) is False
    assert _egress.host_allowed("github.com", allow) is False
    assert _egress.host_allowed("1.1.1.1", allow) is False
    assert _egress.host_allowed("", allow) is False
    assert _egress.host_allowed("api.github.com.", allow) is True


def test_sni_parser_rejects_garbage():
    from app.sandbox import egress as _egress

    assert _egress.sni_from_clienthello(b"") == ""
    assert _egress.sni_from_clienthello(b"GET / HTTP/1.0\r\n\r\n") == ""
    assert _egress.sni_from_clienthello(b"\x16\x03\x01\x00\x02\xff\xff") == ""


def test_dns_stub_refuses_non_allowlisted():
    import struct as _struct

    from app.sandbox import egress as _egress

    def _query(name):
        q = _struct.pack(">HHHHHH", 0x4242, 0x0100, 1, 0, 0, 0)
        q += b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split("."))
        return q + b"\x00" + _struct.pack(">HH", 1, 1)

    resp = _egress._dns_response(_query("evil.example.com"), ["api.github.com"])
    assert resp is not None
    rcode = _struct.unpack(">H", resp[2:4])[0] & 0x000F
    assert rcode == 5  # REFUSED
    assert _egress._dns_response(b"\x00\x01", ["api.github.com"]) is None


def test_dns_stub_answers_allowlisted(monkeypatch):
    import socket as _socket
    import struct as _struct

    from app.sandbox import egress as _egress

    monkeypatch.setattr(_socket, "gethostbyname", lambda name: "1.2.3.4")
    q = _struct.pack(">HHHHHH", 0x1111, 0x0100, 1, 0, 0, 0)
    q += b"\x03api\x06github\x03com\x00" + _struct.pack(">HH", 1, 1)
    resp = _egress._dns_response(q, ["api.github.com"])
    assert resp is not None
    rcode = _struct.unpack(">H", resp[2:4])[0] & 0x000F
    assert rcode == 0
    assert _socket.inet_aton("1.2.3.4") in resp


def test_destroy_orphans_reaps_stale_jail_dir(monkeypatch, tmp_path):
    """destroy_orphans() actually removes jail dirs with no live owner."""
    from app.sandbox import firecracker as _fc

    base = tmp_path / "jails"
    stale = base / "firecracker" / "task-424242" / "root"
    stale.mkdir(parents=True)
    (stale / "overlay.ext4").write_bytes(b"x")
    monkeypatch.setattr(
        "app.sandbox.firecracker._cfg",
        lambda name, default="": str(base) if name == "FC_CHROOT_BASE" else default,
    )
    # Registry is empty for 424242 (worker crashed before destroy).
    assert 424242 not in _fc._REG
    cleaned = _fc.destroy_orphans()
    assert cleaned >= 1
    assert not (base / "firecracker" / "task-424242").exists()


def test_destroy_orphans_reaps_overstayed_live_vm(monkeypatch, tmp_path):
    """Overstayed registry VMs (older than the runtime cap) are destroyed."""
    import time as _time

    from app.sandbox import firecracker as _fc

    base = tmp_path / "jails"
    chroot = base / "firecracker" / "task-424243" / "root"
    chroot.mkdir(parents=True)
    monkeypatch.setattr(
        "app.sandbox.firecracker._cfg",
        lambda name, default="": str(base) if name == "FC_CHROOT_BASE" else default,
    )
    vm = _fc._VM(
        task_id=424243,
        jail_id="task-424243",
        chroot_dir=str(chroot),
        api_socket=str(chroot / "api.socket"),
        cid=_fc._cid_for(424243),
        overlay=str(chroot / "overlay.ext4"),
    )
    # Pretend the VM started long ago (runaway worker that never destroyed).
    vm.ready = True
    vm.created_mono = _time.monotonic() - 10_000
    with _fc._REG_LOCK:
        _fc._REG[424243] = vm
    try:
        cleaned = _fc.destroy_orphans(max_runtime_s=60)
        assert cleaned >= 1
        with _fc._REG_LOCK:
            assert 424243 not in _fc._REG
        assert not (base / "firecracker" / "task-424243").exists()
    finally:
        with _fc._REG_LOCK:
            _fc._REG.pop(424243, None)


def test_migrate_strictness(monkeypatch):
    """Prod migration failure stops startup; dev may fall back."""
    import sys as _sys

    from app.db import migrate as _migrate

    monkeypatch.setitem(_sys.modules, "alembic", None)
    assert _migrate.upgrade_head(strict=False) is False
    try:
        _migrate.upgrade_head(strict=True)
    except _migrate.MigrationFailed:
        return
    raise AssertionError("strict migration failure must raise MigrationFailed")
