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
    rules = "\n".join(_net._nft_rules(tap="ftap1", proxy_port=8443, iface="vh1"))
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


def test_cgroup_args_v1_real_quota_not_shares(monkeypatch):
    """cgroup v1 must enforce a real CPU quota (cfs), never cpu.shares."""
    from app.sandbox import firecracker as _fc

    monkeypatch.setattr(_fc, "cgroup_version", lambda: "1")
    version, args = _fc.jailer_cgroup_args(vcpu=2, mem_mib=1024, pids_max=256)
    assert version == "1"
    blob = " ".join(args)
    assert "cpu.cfs_quota_us=200000" in blob
    assert "cpu.cfs_period_us=100000" in blob
    assert "cpu.shares" not in blob
    assert f"memory.limit_in_bytes={1024 * 1024 * 1024}" in blob
    assert "pids.max=256" in blob


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


def test_nft_rules_match_veth_host_not_tap():
    """Host-namespace rules must match the veth-host device (visible here).

    Matching the TAP name would silently match nothing: the TAP lives in
    the task netns. Chain names stay per-task (tap-derived) so task B
    never flushes task A's chains.
    """
    import pytest as _pytest

    from app.sandbox import net as _net

    rules = "\n".join(_net._nft_rules(tap="ft7", proxy_port=8443, iface="vh7"))
    assert 'iifname "vh7"' in rules
    assert '"ft7"' not in rules
    assert "pre_ft7" in rules and "out_ft7" in rules
    with _pytest.raises(_net.NetworkIsolationError):
        _net._nft_rules(tap="ft7", proxy_port=8443)


def test_nft_rules_proxy_only_no_open_443():
    """No bare `tcp dport 443 accept`: everything funnels to the proxy."""
    from app.sandbox import net as _net

    rules = "\n".join(_net._nft_rules(tap="ft9", proxy_port=8443, iface="vh9"))
    assert "dnat to 10.200.0.1:8443" in rules
    assert "tcp dport 443 drop" in rules
    # The forward chain is default-deny FOR THIS TASK ONLY via an explicit
    # interface-scoped catch-all — never a base-chain drop policy (which
    # would drop unrelated host-forwarded traffic on the shared hook).
    assert "policy drop" not in dict(_net._chain_specs())["out"]
    assert "policy drop" not in dict(_net._chain_specs())["in"]
    assert rules.splitlines()[-1].endswith('iifname "vh9" drop')
    for line in rules.splitlines():
        if "tcp dport 443 accept" in line:
            raise AssertionError(f"open-443 bypass in nft rules: {line}")
    # Per-task chains (multi-tenant safe).
    assert "pre_ft9" in rules and "out_ft9" in rules


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


def _dead_pid() -> int:
    """A valid-range PID that is (practically) never allocated.

    Stays inside the plausible PID range so liveness probing is a single
    instant `os.kill` check — no child processes spawned, no absurd
    out-of-range PIDs. Tests always pair it with a very stale heartbeat
    (and, where relevant, lease state), mirroring production's layered
    check: even a recycled PID could not cause a wrongful reap without a
    live lease AND a fresh heartbeat.
    """
    return 2**21 - 1  # 2097151: valid range, never allocated on test hosts


def _write_test_owner(base, tid, *, host=None, pid=None, age_s=2000):
    """Plant a durable owner record for a jail dir (crashed-worker sim)."""
    import json as _json
    import time as _time

    from app.sandbox import firecracker as _fc

    owner = {
        "task_id": tid,
        "host": host if host is not None else _fc._local_host(),
        "worker": "w-test",
        "pid": pid if pid is not None else _dead_pid(),
        "pid_start": "",
        "user": "",
        "heartbeat": _time.time() - age_s,
    }
    path = base / "firecracker" / f"task-{tid}" / "owner.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(owner), encoding="utf-8")
    return owner


def test_destroy_orphans_reaps_stale_jail_dir(monkeypatch, tmp_path):
    """destroy_orphans() reaps jail dirs with provably dead owners only."""
    from app.sandbox import firecracker as _fc

    base = tmp_path / "jails"
    stale = base / "firecracker" / "task-424242" / "root"
    stale.mkdir(parents=True)
    (stale / "overlay.ext4").write_bytes(b"x")
    monkeypatch.setattr(
        "app.sandbox.firecracker._cfg",
        lambda name, default="": str(base) if name == "FC_CHROOT_BASE" else default,
    )
    # Crashed worker: dead pid + very stale heartbeat + no live DB lease.
    _write_test_owner(base, 424242, age_s=2000)
    monkeypatch.setattr(_fc, "_db_lease_live", lambda tid: False)
    assert 424242 not in _fc._REG
    cleaned = _fc.destroy_orphans()
    assert cleaned >= 1
    assert not (base / "firecracker" / "task-424242").exists()


def test_destroy_orphans_needs_owner_file(monkeypatch, tmp_path):
    """A jail dir with NO owner record is never reaped (mid-provision safe)."""
    from app.sandbox import firecracker as _fc

    base = tmp_path / "jails"
    pending = base / "firecracker" / "task-424249" / "root"
    pending.mkdir(parents=True)
    monkeypatch.setattr(
        "app.sandbox.firecracker._cfg",
        lambda name, default="": str(base) if name == "FC_CHROOT_BASE" else default,
    )
    monkeypatch.setattr(_fc, "_db_lease_live", lambda tid: False)
    assert 424249 not in _fc._REG
    assert _fc.destroy_orphans() == 0
    assert (base / "firecracker" / "task-424249").exists()


def test_destroy_orphans_spares_live_foreign_owner(monkeypatch, tmp_path):
    """A live VM owned by another worker process survives a foreign sweep."""
    import os as _os

    from app.sandbox import firecracker as _fc

    base = tmp_path / "jails"
    live = base / "firecracker" / "task-424250" / "root"
    live.mkdir(parents=True)
    monkeypatch.setattr(
        "app.sandbox.firecracker._cfg",
        lambda name, default="": str(base) if name == "FC_CHROOT_BASE" else default,
    )
    # Owner is THIS test process with a fresh heartbeat: alive by definition.
    _write_test_owner(base, 424250, pid=_os.getpid(), age_s=0)
    monkeypatch.setattr(_fc, "_db_lease_live", lambda tid: False)
    assert 424250 not in _fc._REG  # absent locally, yet owned elsewhere
    assert _fc.destroy_orphans() == 0
    assert (base / "firecracker" / "task-424250").exists()


def test_destroy_orphans_spares_foreign_host(monkeypatch, tmp_path):
    """Owner records from another host are never reaped here."""
    from app.sandbox import firecracker as _fc

    base = tmp_path / "jails"
    other = base / "firecracker" / "task-424251" / "root"
    other.mkdir(parents=True)
    monkeypatch.setattr(
        "app.sandbox.firecracker._cfg",
        lambda name, default="": str(base) if name == "FC_CHROOT_BASE" else default,
    )
    _write_test_owner(base, 424251, host="some-other-host", age_s=99999)
    monkeypatch.setattr(_fc, "_db_lease_live", lambda tid: False)
    assert _fc.destroy_orphans() == 0
    assert (base / "firecracker" / "task-424251").exists()


def test_destroy_orphans_live_lease_protects_vm(monkeypatch, tmp_path):
    """A RUNNING task with a live lease is never reaped, even when the
    owner pid is dead and the heartbeat is stale (lease always wins)."""
    from app.sandbox import firecracker as _fc

    base = tmp_path / "jails"
    leased = base / "firecracker" / "task-424252" / "root"
    leased.mkdir(parents=True)
    monkeypatch.setattr(
        "app.sandbox.firecracker._cfg",
        lambda name, default="": str(base) if name == "FC_CHROOT_BASE" else default,
    )
    _write_test_owner(base, 424252, age_s=99999)
    monkeypatch.setattr(_fc, "_db_lease_live", lambda tid: True)
    assert _fc.destroy_orphans() == 0
    assert (base / "firecracker" / "task-424252").exists()


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


def _try_symlink(target, link):
    """Create a symlink; pytest.skip when the platform refuses (Windows)."""
    import pytest as _pytest

    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        _pytest.skip(f"symlinks unavailable on this host: {exc}")


def test_collect_transfer_never_follows_symlinks(tmp_path):
    """Host->guest enumeration must not expose outside files via repo links."""
    from app.sandbox import firecracker as _fc

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "good.py").write_text("print('ok')\n", encoding="utf-8")
    (ws / ".env").write_text("LLM_API_KEY=topsecret\n", encoding="utf-8")
    sentinel = tmp_path / "HOST_ONLY_SENTINEL"
    sentinel.write_text("host-secret-xyz\n", encoding="utf-8")
    _try_symlink(str(sentinel), str(ws / "innocent.txt"))
    sub = ws / "sub"
    sub.mkdir()
    _try_symlink(str(tmp_path), str(sub / "dirlink"))

    collected = dict(_fc._collect_transfer_files(str(ws)))
    assert "good.py" in collected
    assert ".env" not in collected
    assert "innocent.txt" not in collected
    assert not any("dirlink" in rel for rel in collected)
    for rel, full in collected.items():
        assert "host-secret-xyz" not in _fc._read_host_file_nofollow(full)


def test_read_nofollow_refuses_symlink(tmp_path):
    from app.sandbox import firecracker as _fc

    ws = tmp_path / "ws"
    ws.mkdir()
    real = ws / "real.txt"
    real.write_text("data\n", encoding="utf-8")
    assert _fc._read_host_file_nofollow(str(real)) == "data\n"
    sentinel = tmp_path / "SENTINEL"
    sentinel.write_text("s3cr3t\n", encoding="utf-8")
    link = ws / "link.txt"
    _try_symlink(str(sentinel), str(link))
    try:
        _fc._read_host_file_nofollow(str(link))
    except OSError:
        return
    raise AssertionError("O_NOFOLLOW read must refuse symlinks")


def test_safe_host_dest_rejects_escapes(tmp_path):
    import pytest as _pytest

    from app.sandbox import firecracker as _fc

    ws = tmp_path / "ws"
    ws.mkdir()
    for bad in ("", "../evil", "/abs", "a/../../b", "C:\\win", "a\x00b"):
        with _pytest.raises(ValueError):
            _fc._safe_host_dest(str(ws), bad)
    # Symlinked parent inside the workspace.
    outside = tmp_path / "outside"
    outside.mkdir()
    _try_symlink(str(outside), str(ws / "sub"))
    with _pytest.raises(ValueError):
        _fc._safe_host_dest(str(ws), "sub/evil.txt")
    # Symlink at the destination itself.
    sentinel = outside / "SENTINEL"
    sentinel.write_text("keep\n", encoding="utf-8")
    _try_symlink(str(sentinel), str(ws / "dest.txt"))
    with _pytest.raises(ValueError):
        _fc._safe_host_dest(str(ws), "dest.txt")
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_write_host_result_atomic_and_safe(tmp_path):
    import pytest as _pytest

    from app.sandbox import firecracker as _fc

    ws = tmp_path / "ws"
    ws.mkdir()
    dest = _fc.write_host_result(str(ws), "pkg/app.py", "print(1)\n")
    assert open(dest, encoding="utf-8").read() == "print(1)\n"
    assert not os.path.exists(dest + ".fixhub-tmp")
    # Sensitive names blocked even when the path is otherwise safe.
    with _pytest.raises(ValueError):
        _fc.write_host_result(str(ws), ".env", "x=1\n")
    # Guest path escaping through a host symlink is refused; sentinel intact.
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "SENTINEL"
    sentinel.write_text("keep\n", encoding="utf-8")
    _try_symlink(str(sentinel), str(ws / "evil.txt"))
    with _pytest.raises(ValueError):
        _fc.write_host_result(str(ws), "evil.txt", "pwned\n")
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_guest_addrs_per_task_unique_and_aligned():
    from app.sandbox import net as _net

    a = _net.guest_addrs(91021)
    b = _net.guest_addrs(91022)
    assert a["prefix"] == 30
    assert a["vm"] != b["vm"] and a["gw"] != b["gw"] and a["net"] != b["net"]
    for tid in (1, 7, 91001, 100001, 16383, 16384):
        addrs = _net.guest_addrs(tid)
        assert addrs["net"].startswith("10.201.")
        assert addrs["gw"].rsplit(".", 1)[0] == addrs["net"].rsplit(".", 1)[0]
        last = int(addrs["net"].rsplit(".", 1)[1])
        assert last % 4 == 0  # /30 alignment
        assert addrs["gw"].endswith(f".{last + 1}")
        assert addrs["vm"].endswith(f".{last + 2}")
    nets = {_net.guest_addrs(i)["net"] for i in range(16384)}
    assert len(nets) == 16384  # full subnet space, no collisions


def test_ifname_full_id_no_truncation():
    import pytest as _pytest

    from app.sandbox import net as _net

    assert _net.tap_name(1) == "ft1"
    assert _net.tap_name(100001) == "ft100001"  # must differ, never collide
    assert _net.veth_names(1) == ("vh1", "vg1")
    assert _net.veth_names(100001) != _net.veth_names(1)
    with _pytest.raises(_net.NetworkIsolationError):
        _net.tap_name(10**15)  # >15 chars: refuse, never truncate


def test_nft_redirect_mark_before_drops():
    """Prerouting marks+DNATs; the INPUT chain (the hook DNATed-to-local
    packets actually traverse) accepts marked proxy traffic before the
    10/8 drop; unmarked direct-to-proxy traffic still drops."""
    from app.sandbox import net as _net

    pre_fwd = _net._nft_rules(tap="ft9", proxy_port=8443, iface="vh9")
    blob = "\n".join(pre_fwd)
    assert "meta mark set 0x1" in blob and "dnat to 10.200.0.1:8443" in blob
    # Forward chain carries NO local-destination accepts (dead rules would
    # be mistaken for enforcement — DNATed packets never traverse forward).
    for line in pre_fwd:
        if "accept" in line:
            assert "10.200.0.1" not in line, line
    rules = _net._nft_input_rules(tap="ft9", proxy_port=8443, iface="vh9")
    blob = "\n".join(rules)
    marked = next(i for i, r in enumerate(rules) if "meta mark 0x1" in r and "accept" in r)
    drop10 = next(i for i, r in enumerate(rules) if "ip daddr 10.0.0.0/8 drop" in r)
    assert marked < drop10
    # No unmarked proxy-port accept anywhere (direct-connect bypass closed).
    for line in rules:
        if "tcp dport 8443 accept" in line:
            assert "meta mark" in line, line
    # Catch-all drop is last: no other host-local service is reachable.
    assert rules[-1].endswith('iifname "vh9" drop')
    # Every input rule is interface-qualified (other hosts/tasks unaffected).
    for line in rules:
        assert 'iifname "vh9"' in line, line
    # Input chain spec carries no drop policy (that would hit all host input).
    assert "policy drop" not in dict(_net._chain_specs())["in"]


def test_teardown_targets_scoped_to_task():
    from app.sandbox import net as _net

    t = _net._teardown_targets(91021)
    assert t["netns"] == "fixhub-t91021"
    assert t["vh"] == "vh91021"
    assert t["net"].startswith("10.201.")
    assert "lo" not in (t["vh"], t["tap"])
    t2 = _net._teardown_targets(91022)
    assert t["chains"] != t2["chains"]
    assert t["net"] != t2["net"]


def test_atomic_write_stops_at_workspace_root(tmp_path):
    """Symlinked ancestors ABOVE the workspace must not refuse writes, but
    symlinked parents INSIDE still do."""
    from app.sandbox import firecracker as _fc

    real = tmp_path / "real"
    real.mkdir()
    ws = real / "ws"
    ws.mkdir()
    linkdir = tmp_path / "linkdir"
    _try_symlink(str(real), str(linkdir))
    if not os.path.islink(str(linkdir)):
        return  # symlink platform check already skipped inside _try_symlink
    ws_via_link = linkdir / "ws"
    dest = _fc.write_host_result(str(ws_via_link), "pkg/app.py", "print(1)\n")
    assert open(dest, encoding="utf-8").read() == "print(1)\n"


def test_guest_status_warns_on_truncated_sync(tmp_path, monkeypatch):
    """A capped reconcile must present a visible warning, never silent."""
    from app.sandbox import firecracker as _fc

    ws = tmp_path / "task-424260"
    ws.mkdir()
    monkeypatch.setattr(
        _fc, "sync_guest_to_host",
        lambda task_id, workspace: {"files": 2000, "bytes": 1, "truncated": True},
    )
    monkeypatch.setattr(
        _fc, "_run_host_git", lambda workspace, *args, cap=8000: "exit_code: 0\n"
    )
    out = _fc.guest_status_and_diff(str(ws), what="status")
    assert "warning" in out.lower() and "partial" in out.lower()
    monkeypatch.setattr(
        _fc, "sync_guest_to_host",
        lambda task_id, workspace: {"files": 1, "bytes": 9, "truncated": False},
    )
    assert "warning" not in _fc.guest_status_and_diff(str(ws), what="status").lower()


def test_cgroup_expected_files_shapes():
    from app.sandbox import firecracker as _fc

    v2 = _fc._expected_cgroup_files(version="2", vcpu=2, mem_mib=1024, pids_max=256)
    assert v2 == {
        "cpu.max": "200000 100000",
        "memory.max": str(1024 * 1024 * 1024),
        "pids.max": "256",
    }
    v1 = _fc._expected_cgroup_files(version="1", vcpu=2, mem_mib=1024, pids_max=256)
    assert v1["cpu.cfs_quota_us"] == "200000"
    assert v1["cpu.cfs_period_us"] == "100000"
    assert "cpu.shares" not in v1
    assert v1["memory.limit_in_bytes"] == str(1024 * 1024 * 1024)


def test_cgroup_base_discovery():
    from app.sandbox import firecracker as _fc

    mounts = [
        ("cgroup", "/sys/fs/cgroup/cpu", "rw,cpu"),
        ("cgroup", "/sys/fs/cgroup/memory", "rw,memory"),
        ("cgroup2", "/sys/fs/cgroup", "rw,nsdelegate"),
    ]
    assert _fc._cgroup_base_dir(
        version="2", controller_file="cpu.max", mounts=mounts
    ) == "/sys/fs/cgroup"
    assert _fc._cgroup_base_dir(
        version="1", controller_file="cpu.cfs_quota_us", mounts=mounts
    ) == "/sys/fs/cgroup/cpu"
    assert _fc._cgroup_base_dir(
        version="1", controller_file="memory.limit_in_bytes", mounts=mounts
    ) == "/sys/fs/cgroup/memory"
    try:
        _fc._cgroup_base_dir(version="1", controller_file="nope.x", mounts=mounts)
    except RuntimeError:
        return
    raise AssertionError("undiscoverable controller must raise")


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


# --- Gate 0: concurrent-VM topology regression (mocked ip/nft, no root) ---


def _fake_completed(stdout="", stderr="", returncode=0):
    import subprocess as _sp

    return _sp.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_link_addrs_per_task_unique_and_separate():
    """Link /31 pool: unique per task, /31-aligned, disjoint from guest pool."""
    from app.sandbox import net as _net

    a, b = _net.link_addrs(92001), _net.link_addrs(92002)
    assert a["prefix"] == 31 and b["prefix"] == 31
    assert a["net"] != b["net"] and a["host"] != b["host"] and a["ns"] != b["ns"]
    assert a["net"].startswith("10.202.") and b["net"].startswith("10.202.")
    for tid in (1, 7, 92001, 100001, 32767, 32768):
        link = _net.link_addrs(tid)
        guest = _net.guest_addrs(tid)
        assert link["net"].startswith("10.202.")
        assert not link["net"].startswith("10.201.")
        assert not guest["net"].startswith("10.202.")
        last = int(link["net"].rsplit(".", 1)[1])
        assert last % 2 == 0  # /31 alignment (even base)
        assert link["host"].endswith(f".{last}")
        assert link["ns"].endswith(f".{last + 1}")
        assert link["host"] != _net.HOST_SVC_IP and link["ns"] != _net.HOST_SVC_IP
    nets = {_net.link_addrs(i)["net"] for i in range(32768)}
    assert len(nets) == 32768  # full link space, no collisions
    # Guest-space wrap implies link-space difference and vice versa: the
    # guest collision guard therefore covers link collisions too.
    assert _net.guest_addrs(7)["net"] == _net.guest_addrs(7 + 16384)["net"]
    assert _net.link_addrs(7)["net"] != _net.link_addrs(7 + 16384)["net"]
    assert _net.guest_addrs(9)["net"] == _net.guest_addrs(9 + 32768)["net"]
    assert _net.link_addrs(9)["net"] == _net.link_addrs(9 + 32768)["net"]


def test_concurrent_vms_distinct_topology_and_chains():
    """Two simultaneous VMs: distinct guest/link subnets, per-task chains/hooks.

    Regression for the duplicate-HOST_SVC_IP bug (_ensure_veth assigning
    10.200.0.1/24 to every veth): each veth pair holds only its own /31
    link addresses, the service address lives once on loopback, and
    sibling chains never overlap.
    """
    from app.sandbox import net as _net

    a_id, b_id = 92001, 92002
    aa, ab = _net.guest_addrs(a_id), _net.guest_addrs(b_id)
    assert aa["net"] != ab["net"] and aa["vm"] != ab["vm"] and aa["gw"] != ab["gw"]
    assert aa["net"].startswith("10.201.") and ab["net"].startswith("10.201.")
    la, lb = _net.link_addrs(a_id), _net.link_addrs(b_id)
    assert la["net"] != lb["net"] and la["host"] != lb["host"] and la["ns"] != lb["ns"]

    # Interface + chain identity is per-task (no truncation collisions).
    assert _net.veth_names(a_id) != _net.veth_names(b_id)
    assert _net.tap_name(a_id) != _net.tap_name(b_id)
    ta, tb = _net._teardown_targets(a_id), _net._teardown_targets(b_id)
    assert ta["chains"] != tb["chains"] and ta["net"] != tb["net"]
    assert ta["vh"] != tb["vh"] and ta["tap"] != tb["tap"]
    assert len(ta["chains"]) == 3  # prerouting + forward + input
    assert any(c.startswith("in_") for c in ta["chains"])
    # Teardown never touches shared state (loopback) or a sibling's objects.
    for key in ("netns", "tap", "vh", "net", "vm_ip"):
        assert tb[key] not in (ta["netns"], ta["tap"], ta["vh"], ta["net"], ta["vm_ip"]) or key in (
            "net", "vm_ip",
        )
    assert "lo" not in (ta["vh"], ta["tap"], tb["vh"], tb["tap"])

    # Forward rules for A match only A's veth-host device and carry the
    # full forbidden set + DNS bypass drops + IPv6 drops (no local-dst
    # accepts: DNATed packets traverse input, never forward).
    for tid, vh in ((a_id, ta["vh"]), (b_id, tb["vh"])):
        rules = _net._nft_rules(tap=_net.tap_name(tid), proxy_port=8443, iface=vh)
        blob = "\n".join(rules)
        other_vh = tb["vh"] if tid == a_id else ta["vh"]
        assert f'iifname "{vh}"' in blob
        assert f'iifname "{other_vh}"' not in blob
        for cidr in _net.FORBIDDEN_CIDRS:
            assert f"ip daddr {cidr} drop" in blob, cidr
        assert "udp dport 53 drop" in blob and "tcp dport 53 drop" in blob
        assert "ip6 daddr ::/0 drop" in blob
        for line in rules:
            if "accept" in line:
                assert "10.200.0.1" not in line, line
    # Input rules carry the proxy/DNS reachability shape: marked accept to
    # the stub exists, and no unmarked direct-to-proxy accept exists.
    for tid, vh in ((a_id, ta["vh"]), (b_id, tb["vh"])):
        in_rules = _net._nft_input_rules(
            tap=_net.tap_name(tid), proxy_port=8443, iface=vh
        )
        in_blob = "\n".join(in_rules)
        assert f"ip daddr {_net.HOST_SVC_IP} tcp dport 8443 accept" in in_blob
        assert "meta mark 0x1" in in_blob
        assert f"ip daddr {_net.HOST_SVC_IP} accept" in in_blob  # stub DNS
        assert in_rules[-1].endswith(f'iifname "{vh}" drop')  # catch-all last


def _fake_topology_world(tid):
    """Healthy mocked world for _verify_topology (addressed /31 design)."""
    from app.sandbox import net as _net

    addrs = _net.guest_addrs(tid)
    link = _net.link_addrs(tid)
    vh, vg = _net.veth_names(tid)
    tap, netns = _net.tap_name(tid), _net.netns_name(tid)

    def _fake_run(*args, **kwargs):
        cmd = list(args)
        if cmd[:3] == ["ip", "addr", "show"] and "lo" in cmd:
            return _fake_completed(stdout=f"inet {_net.HOST_SVC_IP}/32 scope host lo\n")
        if cmd[:4] == ["ip", "-o", "addr", "show"]:
            return _fake_completed(
                stdout=f"2: {vh} inet {link['host']}/{link['prefix']} brd x scope global\n"
            )
        if cmd[:3] == ["ip", "neigh", "show"]:
            return _fake_completed(
                stdout=f"{addrs['vm']} lladdr aa:bb:cc:dd:ee:01 PERMANENT\n"
            )
        if cmd[:3] == ["ip", "route", "show"]:
            return _fake_completed(
                stdout=f"{addrs['net']}/{addrs['prefix']} dev {vh} scope link\n"
            )
        if cmd[0] == "sysctl" or "sysctl" in cmd:
            return _fake_completed(stdout="1\n")
        return _fake_completed(stdout="")

    def _fake_run_ns(netns_arg, *ip_args, **kwargs):
        args = list(ip_args)
        if args[:2] == ["addr", "show"] and tap in args:
            return _fake_completed(stdout=f"inet {addrs['gw']}/30 scope global {tap}\n")
        if args[:3] == ["-o", "addr", "show"]:
            return _fake_completed(
                stdout=f"3: {vg} inet {link['ns']}/{link['prefix']} scope global\n"
            )
        if args[:2] == ["neigh", "show"]:
            queried = args[2] if len(args) > 2 else ""
            return _fake_completed(
                stdout=f"{queried} lladdr aa:bb:cc:dd:ee:ff PERMANENT\n"
            )
        if args[:2] == ["route", "show"] and len(args) == 2:
            return _fake_completed(
                stdout=(
                    f"{_net.HOST_SVC_IP}/32 dev {vg} scope link\n"
                    f"default via {link['host']} dev {vg}\n"
                )
            )
        return _fake_completed(stdout="")

    return addrs, link, vh, vg, tap, netns, _fake_run, _fake_run_ns


def test_verify_accepts_addressed_topology(monkeypatch):
    """Healthy /31 world (exact link addrs + intended default) verifies."""
    from app.sandbox import net as _net

    tid = 92001
    addrs, link, vh, vg, tap, netns, _fake_run, _fake_run_ns = _fake_topology_world(tid)
    monkeypatch.setattr(_net, "_run", _fake_run)
    monkeypatch.setattr(_net, "_run_ns", _fake_run_ns)
    _net._verify_topology(netns, tap, vh, vg, addrs, link)


def test_verify_rejects_duplicate_link_address(monkeypatch):
    """_verify_topology must refuse a veth carrying the service address
    (old duplicate-IP shape) or any extra address."""
    import pytest as _pytest

    from app.sandbox import net as _net

    tid = 92001
    addrs, link, vh, vg, tap, netns, _fake_run, _fake_run_ns = _fake_topology_world(tid)

    def _bad_run(*args, **kwargs):
        cmd = list(args)
        if cmd[:4] == ["ip", "-o", "addr", "show"]:
            # Regression shape: service address duplicated on the veth.
            return _fake_completed(
                stdout=(
                    f"2: {vh} inet {link['host']}/{link['prefix']} scope global\n"
                    f"2: {vh} inet {_net.HOST_SVC_IP}/24 brd x scope global\n"
                )
            )
        return _fake_run(*args, **kwargs)

    monkeypatch.setattr(_net, "_run", _bad_run)
    monkeypatch.setattr(_net, "_run_ns", _fake_run_ns)
    with _pytest.raises(_net.NetworkIsolationError, match="extra addresses"):
        _net._verify_topology(netns, tap, vh, vg, addrs, link)


def test_verify_rejects_wrong_default_route(monkeypatch):
    """A missing or foreign default route in the netns refuses boot."""
    import pytest as _pytest

    from app.sandbox import net as _net

    tid = 92001
    addrs, link, vh, vg, tap, netns, _fake_run, _fake_run_ns = _fake_topology_world(tid)

    def _noroute_run_ns(netns_arg, *ip_args, **kwargs):
        args = list(ip_args)
        if args[:2] == ["route", "show"] and len(args) == 2:
            return _fake_completed(
                stdout=f"{_net.HOST_SVC_IP}/32 dev {vg} scope link\n"
            )
        return _fake_run_ns(netns_arg, *ip_args, **kwargs)

    monkeypatch.setattr(_net, "_run", _fake_run)
    monkeypatch.setattr(_net, "_run_ns", _noroute_run_ns)
    with _pytest.raises(_net.NetworkIsolationError, match="default route"):
        _net._verify_topology(netns, tap, vh, vg, addrs, link)


def test_ensure_veth_emits_addressed_routes(monkeypatch):
    """_ensure_veth assigns exactly the /31 pair + via-routes + default."""
    from app.sandbox import net as _net

    tid = 92001
    addrs, link = _net.guest_addrs(tid), _net.link_addrs(tid)
    vh, vg = _net.veth_names(tid)
    netns = _net.netns_name(tid)
    calls: list[tuple[str, ...]] = []

    def _fake_run(*args, **kwargs):
        calls.append(("root",) + tuple(args))
        cmd = list(args)
        if cmd[:4] == ["ip", "-o", "link", "show"]:
            return _fake_completed(stdout=f"2: {vh} link/ether aa:bb:cc:dd:ee:ff\n")
        if cmd[0] == "sysctl":
            return _fake_completed(stdout="")
        return _fake_completed(stdout="")

    def _fake_run_ns(ns, *ip_args, **kwargs):
        calls.append(("ns",) + tuple(ip_args))
        args = list(ip_args)
        if args[:3] == ["-o", "link", "show"]:
            return _fake_completed(stdout=f"3: {vg} link/ether aa:bb:cc:dd:ee:01\n")
        if args[:2] == ["route", "show"]:
            return _fake_completed(
                stdout=f"default via {link['host']} dev {vg}\n"
            )
        return _fake_completed(stdout="")

    monkeypatch.setattr(_net, "_run", _fake_run)
    monkeypatch.setattr(_net, "_run_ns", _fake_run_ns)
    _net._ensure_veth(netns, vh, vg, addrs, link)
    flat = [" ".join(c) for c in calls]
    # Addressed ends (replace heals stale layouts), static routes, default.
    assert any(f"addr replace {link['host']}/{link['prefix']} dev {vh}" in s for s in flat)
    assert any(f"addr replace {link['ns']}/{link['prefix']} dev {vg}" in s for s in flat)
    # Service route is link-scoped (dev-form): the static HOST_SVC_IP neigh
    # entry is its actual resolution. A `via` form would resolve the next
    # hop instead and leave that entry decorative — refuse that shape.
    assert any(
        f"route replace {_net.HOST_SVC_IP}/32 dev {vg}" in s for s in flat
    )
    assert not any(
        f"route replace {_net.HOST_SVC_IP}/32 via" in s for s in flat
    ), "service route must not use a via next hop"
    # Static neigh for the service IP AND for the default's real next hop.
    assert any(
        f"neigh replace {_net.HOST_SVC_IP} lladdr" in s and "permanent" in s
        for s in flat
    )
    assert any(
        f"neigh replace {link['host']} lladdr" in s and "permanent" in s
        for s in flat
    )
    assert any(f"route replace default via {link['host']} dev {vg}" in s for s in flat)
    # No unqualified service address may ever land on a veth device.
    for s in flat:
        if f"addr replace {_net.HOST_SVC_IP}" in s or f"addr add {_net.HOST_SVC_IP}" in s:
            raise AssertionError(f"service address on veth: {s}")


def test_loopback_check_requires_exact_32(monkeypatch):
    """Substring 10.200.0.1 must not satisfy the loopback check (10.200.0.10)."""
    import pytest as _pytest

    from app.sandbox import net as _net

    calls: list[list[str]] = []

    def _fake_run(*args, **kwargs):
        calls.append(list(args))
        if list(args) == ["ip", "addr", "show", "dev", "lo"]:
            # Longer address sharing the prefix: must NOT count.
            return _fake_completed(stdout="inet 10.200.0.10/32 scope global lo\n")
        return _fake_completed(stdout="", stderr="cannot find device", returncode=1)

    monkeypatch.setattr(_net, "_run", _fake_run)
    with _pytest.raises(_net.NetworkIsolationError):
        _net._ensure_loopback()
    # It attempted the exact /32 add after rejecting the imposter.
    assert any("10.200.0.1/32" in " ".join(c) for c in calls)


def test_subnet_collision_fails_closed(monkeypatch):
    """Two live tasks mapping to the same /30 refuse the second boot."""
    import pytest as _pytest

    from app.sandbox import net as _net

    tid_a = 7
    # Same subnet by construction (modulo the 16384-subnet space).
    tid_b = tid_a + _net.GUEST_SUBNETS
    assert _net.guest_addrs(tid_a)["net"] == _net.guest_addrs(tid_b)["net"]
    monkeypatch.setattr(
        _net, "_live_task_nets", lambda exclude=None: {tid_a: _net.guest_addrs(tid_a)["net"]}
    )
    with _pytest.raises(_net.NetworkIsolationError, match="already owned"):
        _net._assert_subnet_free(tid_b, _net.guest_addrs(tid_b)["net"])
    # The owner itself is never blocked by its own netns.
    monkeypatch.setattr(_net, "_live_task_nets", lambda exclude=None: {})
    _net._assert_subnet_free(tid_a, _net.guest_addrs(tid_a)["net"])


def test_owner_reclaimable_db_unknown_requires_runtime_cap(monkeypatch):
    """DB-unknown reaps only with dead pid AND heartbeat past the VM cap."""
    import time as _time

    from app.sandbox import firecracker as _fc

    owner = {
        "task_id": 424260,
        "host": _fc._local_host(),
        "worker": "w",
        "pid": _dead_pid(),
        "pid_start": "",
        "user": "",
        "heartbeat": _time.time() - 1000,  # past stale(900), below cap(1500)
    }
    monkeypatch.setattr(_fc, "_db_lease_live", lambda tid: None)
    assert _fc._owner_reclaimable(dict(owner), stale_s=900) is False
    owner["heartbeat"] = _time.time() - 2000  # past the runtime cap
    assert _fc._owner_reclaimable(dict(owner), stale_s=900) is True
    # Live lease always protects, even with dead pid + ancient heartbeat.
    monkeypatch.setattr(_fc, "_db_lease_live", lambda tid: True)
    assert _fc._owner_reclaimable(dict(owner), stale_s=900) is False
    # Definite not-running lease + dead pid + stale heartbeat reaps.
    monkeypatch.setattr(_fc, "_db_lease_live", lambda tid: False)
    assert _fc._owner_reclaimable(dict(owner), stale_s=900) is True
    # Clock skew (future heartbeat) never reaps.
    owner["heartbeat"] = _time.time() + 60
    assert _fc._owner_reclaimable(dict(owner), stale_s=900) is False


def test_cgroup_verify_reads_back_limits(tmp_path, monkeypatch):
    """Post-spawn cgroup verification accepts exact limits, rejects drift."""
    import pytest as _pytest

    from app.sandbox import firecracker as _fc

    base = tmp_path / "cgroup"
    base.mkdir()
    monkeypatch.setattr(
        _fc, "_cgroup_base_dir", lambda version, controller_file: str(base)
    )
    jail = base / "fixhub" / "task-7"
    jail.mkdir(parents=True)
    expected = _fc._expected_cgroup_files(version="2", vcpu=2, mem_mib=1024, pids_max=256)
    for name, want in expected.items():
        (jail / name).write_text(want, encoding="utf-8")
    _fc._verify_cgroup_applied(jail_id="task-7", version="2", vcpu=2, mem_mib=1024, pids_max=256)
    (jail / "memory.max").write_text("1", encoding="utf-8")
    with _pytest.raises(RuntimeError, match="not enforced"):
        _fc._verify_cgroup_applied(
            jail_id="task-7", version="2", vcpu=2, mem_mib=1024, pids_max=256
        )


def test_host_git_no_shell_and_truncates(tmp_path, monkeypatch):
    """Host git runs argv-style (no shell) and truncates long output visibly."""
    import subprocess as _sp

    from app.sandbox import firecracker as _fc

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        assert isinstance(cmd, list) and cmd[0] == "git"
        assert "shell" not in kwargs
        big = "x" * 9000
        return _sp.CompletedProcess(args=cmd, returncode=0, stdout=big, stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    out = _fc._run_host_git(str(tmp_path), "status", "--porcelain=v1", "-uall")
    assert out.startswith("exit_code: 0")
    assert "truncated" in out
    assert seen["cmd"][:2] == ["git", "status"]
    assert seen["kwargs"].get("cwd") == str(tmp_path)


def test_write_host_result_blocks_colon_sensitive(tmp_path):
    """ADS-style 'sensitive:stream' names are refused via the prefix check."""
    import pytest as _pytest

    from app.sandbox import firecracker as _fc

    ws = tmp_path / "ws"
    ws.mkdir()
    with _pytest.raises(ValueError):
        _fc.write_host_result(str(ws), ".env:stream", "x=1\n")
    dest = _fc.write_host_result(str(ws), "notes.txt", "hi\n")
    assert open(dest, encoding="utf-8").read() == "hi\n"


def test_atomic_write_detects_post_write_escape(tmp_path, monkeypatch):
    """A parent swapped to escape after rename is detected and removed."""
    import os as _os

    import pytest as _pytest

    from app.sandbox import firecracker as _fc

    ws = tmp_path / "ws"
    ws.mkdir()
    real_realpath = _os.path.realpath

    def _fake_realpath(p):
        if str(p).endswith("evil.txt"):
            return "/elsewhere/evil.txt"
        return real_realpath(p)

    monkeypatch.setattr(_os.path, "realpath", _fake_realpath)
    with _pytest.raises(ValueError, match="escaped workspace"):
        _fc.write_host_result(str(ws), "evil.txt", "pwned\n")
    assert not (ws / "evil.txt").exists()


# --- Gate 0: scoped enforcement without a global forward drop ---


def test_forward_chain_scoped_no_global_policy():
    """Per-task forward enforcement must not break unrelated host traffic.

    A base-chain `policy drop` applies to the HOOK, so one task would drop
    every forwarded packet on the host. Instead: no policy on either filter
    chain, every rule interface-qualified, explicit catch-all last.
    """
    from app.sandbox import net as _net

    specs = dict(_net._chain_specs())
    assert "policy drop" not in specs["out"]
    assert "policy drop" not in specs["in"]
    assert "hook forward" in specs["out"] and "hook input" in specs["in"]
    fwd = _net._nft_rules(tap="ft9", proxy_port=8443, iface="vh9")
    inp = _net._nft_input_rules(tap="ft9", proxy_port=8443, iface="vh9")
    for line in fwd + inp:
        assert 'iifname "vh9"' in line, line  # unmatched traffic unaffected
    assert fwd[-1].endswith('iifname "vh9" drop')  # per-task default-deny
    assert inp[-1].endswith('iifname "vh9" drop')
    # Sibling isolation: neither task's rules match the other's device.
    sib = _net._nft_rules(tap="ft91022", proxy_port=8443, iface="vh91022")
    assert 'iifname "vh9"' not in "\n".join(sib)
    sib_in = _net._nft_input_rules(tap="ft91022", proxy_port=8443, iface="vh91022")
    assert 'iifname "vh9"' not in "\n".join(sib_in)


def test_established_first_is_load_bearing():
    """Established accepts precede the 10/8 drop on both hooks — replies to
    the guest (dst 10.201/16, inside 10/8) would otherwise die. Drops still
    gate every NEW flow; cross-lifetime stale entries are flushed (see
    _flush_task_conntrack), not papered over by reordering."""
    from app.sandbox import net as _net

    fwd = _net._nft_rules(tap="ft9", proxy_port=8443, iface="vh9")
    est = next(i for i, r in enumerate(fwd) if "established,related accept" in r)
    drop10 = next(i for i, r in enumerate(fwd) if "ip daddr 10.0.0.0/8 drop" in r)
    assert est < drop10
    inp = _net._nft_input_rules(tap="ft9", proxy_port=8443, iface="vh9")
    est_in = next(i for i, r in enumerate(inp) if "established,related accept" in r)
    drop10_in = next(i for i, r in enumerate(inp) if "ip daddr 10.0.0.0/8 drop" in r)
    assert est_in < drop10_in
    # The guest's own subnet is inside the dropped range — proving the
    # ordering matters (replies must clear before the drop).
    import ipaddress as _ip

    assert _ip.ip_address(_net.guest_addrs(9)["vm"]) in _ip.ip_network("10.0.0.0/8")


def test_conntrack_flush_best_effort(monkeypatch):
    """Stale-entry hygiene: both orig directions flushed; tool absence never
    blocks boot (drops still gate NEW flows)."""
    from app.sandbox import net as _net

    calls: list[tuple[str, ...]] = []

    def _fake_run(*args, **kwargs):
        calls.append(tuple(args))
        return _fake_completed(stdout="")

    monkeypatch.setattr(_net, "_run", _fake_run)
    _net._flush_task_conntrack("10.201.0.2")
    flat = [" ".join(c) for c in calls]
    assert "conntrack -D --orig-src 10.201.0.2" in flat
    assert "conntrack -D --orig-dst 10.201.0.2" in flat

    def _boom(*args, **kwargs):
        raise OSError("conntrack missing")

    monkeypatch.setattr(_net, "_run", _boom)
    _net._flush_task_conntrack("10.201.0.2")  # must not raise


def test_destroy_flushes_conntrack_and_all_chains(monkeypatch):
    """Teardown flushes the guest IP first, then removes all three chains."""
    from app.sandbox import net as _net

    tid = 92001
    vm_ip = _net.guest_addrs(tid)["vm"]
    runs: list[tuple[str, ...]] = []
    nft_cmds: list[tuple[str, ...]] = []

    def _fake_run(*args, **kwargs):
        runs.append(tuple(args))
        return _fake_completed(stdout="")

    def _fake_nft(*args):
        nft_cmds.append(tuple(args))
        return _fake_completed(stdout="")

    monkeypatch.setattr(_net, "_run", _fake_run)
    monkeypatch.setattr(_net, "_run_ns", lambda *a, **k: _fake_completed(stdout=""))
    monkeypatch.setattr(_net, "_nft", _fake_nft)
    _net.destroy_isolation(tid)  # never raises by contract
    flat = [" ".join(c) for c in runs]
    assert f"conntrack -D --orig-src {vm_ip}" in flat
    deleted = {" ".join(c) for c in nft_cmds if c[:2] == ("delete", "chain")}
    tap = _net.tap_name(tid)
    pre, out, in_chain = _net._chain_names(tap)
    for chain in (pre, out, in_chain):
        assert any(chain in d for d in deleted), chain


def test_verify_requires_link_peer_neigh(monkeypatch):
    """A missing permanent entry for the default's next hop refuses boot
    (no silent fallback to dynamic resolution)."""
    import pytest as _pytest

    from app.sandbox import net as _net

    tid = 92001
    addrs, link, vh, vg, tap, netns, _fake_run, _fake_run_ns = _fake_topology_world(tid)

    def _no_peer_ns(netns_arg, *ip_args, **kwargs):
        args = list(ip_args)
        if args[:2] == ["neigh", "show"] and len(args) > 2 and args[2] == link["host"]:
            return _fake_completed(stdout="")  # no PERMANENT entry
        return _fake_run_ns(netns_arg, *ip_args, **kwargs)

    monkeypatch.setattr(_net, "_run", _fake_run)
    monkeypatch.setattr(_net, "_run_ns", _no_peer_ns)
    with _pytest.raises(_net.NetworkIsolationError, match="link peer"):
        _net._verify_topology(netns, tap, vh, vg, addrs, link)
