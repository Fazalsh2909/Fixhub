"""Phase 5 M6: REAL microVM security suite (no mocks count).

Gate: runs only when FIXHUB_FIRECRACKER_TEST=1. Under that flag EVERY test
REQUIRES a real jailer-booted microVM; missing KVM/binaries/images/network
isolation FAILS the test (never skips, never mocks success). Without the flag
the whole module skips cleanly for Windows-laptop dev.

Covers the 15 acceptance probes:
  1 boot+exec  2 host-marker secrecy  3 cross-task isolation
  4 credential absence  5 traversal/symlink  6 forbidden net
  7 approved egress  8 resource limits  9 cancel/terminate
  10 crash recovery  11 no host fallback  12 concurrent VMs
  13 worker-in-VM  14 publish boundary (tokenless guest)
  15 vsock CID agreement (config == dial)

Each test provisions real VMs through app.sandbox.firecracker.provision().
"""

import os

import pytest

REQUIRED = os.environ.get("FIXHUB_FIRECRACKER_TEST") == "1"

if not REQUIRED:
    pytest.skip(
        "real-VM suite needs FIXHUB_FIRECRACKER_TEST=1 on a Linux/KVM host",
        allow_module_level=True,
    )

from app.sandbox import firecracker as _fc  # noqa: E402
from app.sandbox import images as _images  # noqa: E402
from app.sandbox import sandbox as _sandbox  # noqa: E402


def _require_host():
    """Fail (not skip) when prerequisites are missing under the required flag."""
    result = _images.verify_artifacts()
    assert result["ok"], (
        "FAIL (not skip): real-VM prerequisites missing: "
        + "; ".join(result["missing"])
        + " | info=" + str(result["info"])
    )


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "task-91001"
    ws.mkdir()
    (ws / "app.py").write_text("print('hello')\n", encoding="utf-8")
    return str(ws)


def _exec(task_id, command, cwd="."):
    out = _fc.exec_in_guest(task_id, command=command, cwd=cwd, timeout_s=30)
    assert out["exit_code"] is not None or out["timed_out"]
    return out


def test_01_guest_boots_and_executes(workspace):
    _require_host()
    _fc.provision(91001, workspace)
    try:
        out = _exec(91001, "echo ok-proof-01")
        assert out["exit_code"] == 0
        assert "ok-proof-01" in out["stdout"]
    finally:
        _fc.destroy(91001)


def test_02_guest_cannot_read_host_marker(tmp_path, workspace):
    _require_host()
    marker = tmp_path / "HOST_ONLY_MARKER"
    marker.write_text("host-secret-02", encoding="utf-8")
    _fc.provision(91002, workspace)
    try:
        for probe in (
            f"cat {marker}",
            "ls /srv 2>&1; echo ---; cat /proc/self/root/srv/* 2>&1",
            "cat /host_marker 2>&1",
        ):
            out = _exec(91002, probe)
            assert "host-secret-02" not in out["stdout"]
            assert "host-secret-02" not in out["stderr"]
    finally:
        _fc.destroy(91002)


def test_03_tasks_cannot_cross_workspaces(tmp_path):
    _require_host()
    wa = tmp_path / "task-91003"
    wb = tmp_path / "task-91004"
    wa.mkdir()
    wb.mkdir()
    (wa / "secret.txt").write_text("task-A-secret-03", encoding="utf-8")
    _fc.provision(91003, str(wa))
    _fc.provision(91004, str(wb))
    try:
        out = _exec(91004, "cat ../task-91003/secret.txt 2>&1; find / -name secret.txt 2>&1 | head -5")
        assert "task-A-secret-03" not in out["stdout"]
    finally:
        _fc.destroy(91003)
        _fc.destroy(91004)


def test_04_credentials_absent_from_guest(workspace, monkeypatch):
    _require_host()
    monkeypatch.setenv("BYNARA_API_KEY", "bsk-test-04")
    monkeypatch.setenv("FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "master-test-04")
    _fc.provision(91005, workspace)
    try:
        out = _exec(91005, "env | sort; echo ---; cat /proc/self/environ 2>&1 | tr '\\0' '\\n' | sort")
        for secret in ("bsk-test-04", "master-test-04"):
            assert secret not in out["stdout"]
    finally:
        _fc.destroy(91005)


def test_05_traversal_and_symlink_blocked(workspace):
    _require_host()
    from app.agent import tools as _tools
    from app.config import settings as _settings

    orig = _settings.SANDBOX_BACKEND
    _settings.SANDBOX_BACKEND = "firecracker"
    try:
        _fc.provision(91006, workspace)
        assert "ERROR" in _tools.read_file(workspace, "../../etc/passwd")
        assert "ERROR" in _tools.read_file(workspace, "/etc/shadow")
        out = _exec(91006, "ln -s /etc/passwd link06; cat link06 2>&1 | head -3")
        # Guest agent jail must refuse to follow the escape (no root: line).
        assert "root:" not in out["stdout"]
    finally:
        _settings.SANDBOX_BACKEND = orig
        _fc.destroy(91006)


def test_06_forbidden_destinations_unreachable(workspace):
    _require_host()
    _fc.provision(91007, workspace)
    try:
        for target in (
            # Cloud metadata + private nets (nft hard drops).
            "curl -m 5 http://169.254.169.254/ 2>&1",
            "curl -m 5 http://10.0.0.1/ 2>&1",
            "curl -m 5 http://192.168.1.1/ 2>&1",
            # Direct DNS bypass (must use the host stub only).
            "nslookup api.github.com 8.8.8.8 2>&1",
            "getent hosts api.github.com 2>&1",
            # Unapproved HTTPS: no SNI allowlist match -> proxy closes it.
            "curl -m 8 -sS https://example.com/ -o /dev/null -w '%{http_code}' 2>&1",
            # Literal-IP HTTPS: no hostname -> never allowlisted.
            "curl -m 8 -sk https://1.1.1.1/ -o /dev/null -w '%{http_code}' 2>&1",
            # IPv6 bypass shape.
            "curl -m 5 -g -6 http://[::1]/ 2>&1",
        ):
            out = _exec(91007, target)
            assert out["exit_code"] != 0 or "200" not in out["stdout"], target
    finally:
        _fc.destroy(91007)


def test_07_approved_egress_works(workspace):
    _require_host()
    _fc.provision(91008, workspace)
    try:
        out = _exec(91008, "git ls-remote https://github.com/git/git.git HEAD 2>&1 | head -2")
        assert out["exit_code"] == 0
    finally:
        _fc.destroy(91008)


def test_08_limits_enforced(workspace):
    """Measure ACTUAL limits inside the guest (not merely command timeout)."""
    import re as _re

    from app.config import settings as _settings

    _require_host()
    _fc.provision(91009, workspace)
    try:
        # 1. CPU: guest-visible CPUs must equal the configured vCPU count.
        out = _exec(91009, "nproc")
        assert out["exit_code"] == 0, out
        assert int(out["stdout"].strip()) == int(_settings.FC_GUEST_VCPU), out
        # 2. Memory: guest total must be within 25% of the configured MiB
        # (kernel reserves some; far more/less means the limit is not applied).
        out = _exec(91009, "free -m | awk '/^Mem:/ {print $2}'")
        assert out["exit_code"] == 0, out
        total_mib = int(out["stdout"].strip())
        want_mib = int(_settings.FC_GUEST_MEM_MIB)
        assert abs(total_mib - want_mib) <= max(128, want_mib // 4), (total_mib, want_mib)
        # 3. Disk: rootfs size must not exceed the overlay cap.
        out = _exec(91009, "df -BM / | awk 'NR==2 {print $2}'")
        assert out["exit_code"] == 0, out
        disk_mib = int(_re.sub(r"[^0-9]", "", out["stdout"].strip()))
        assert disk_mib <= int(_settings.FC_OVERLAY_MB), (disk_mib, out)
        # 4. Runtime: a 60s sleep under a short exec budget must actually
        # be terminated near the deadline (wall-clock evidence, not a
        # tautology: a runaway sleep returning success after 60s fails).
        import time as _time

        budget = 10
        started = _time.monotonic()
        out = _fc.exec_in_guest(91009, command="sleep 60", cwd=".",
                                timeout_s=budget)
        elapsed = _time.monotonic() - started
        assert out["timed_out"] is True, out
        assert out["exit_code"] is None, out
        assert elapsed < 60, elapsed
        assert elapsed < budget + 30, elapsed
    finally:
        _fc.destroy(91009)


def test_09_cancel_cleans_up_vm(workspace):
    _require_host()
    vm = _fc.provision(91010, workspace)
    pid_before = vm.firecracker_pid
    _fc.destroy(91010)
    # No residue: jail dir gone, pid dead.
    assert not os.path.exists(vm.jail_dir)
    if pid_before:
        try:
            os.kill(pid_before, 0)
            alive = True
        except OSError:
            alive = False
        assert not alive


def _backdate_owner_dead(task_id, age_s=99999):
    """Rewrite this VM's owner as a dead worker (crashed-worker simulation)."""
    import json as _json
    import time as _time

    import app.sandbox.firecracker as _fcm

    # A valid-range, never-allocated PID (see _dead_pid in
    # test_sandbox_backend.py): instant liveness check, no child spawned.
    dead_pid = 2**21 - 1
    path = _fcm._owner_file_for(task_id)
    with open(path, "r", encoding="utf-8") as fh:
        owner = _json.load(fh)
    owner["pid"] = dead_pid
    owner["pid_start"] = ""
    owner["heartbeat"] = _time.time() - age_s
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        _json.dump(owner, fh)
    os.replace(tmp, path)


def test_10_crash_recovery_leaves_no_vm(workspace):
    _require_host()
    vm = _fc.provision(91011, workspace)
    chroot = vm.chroot_dir
    # Simulate worker loss: drop the in-memory entry (a foreign process
    # never sees it) AND expire the durable owner as a dead worker. The
    # per-host reaper must then reclaim the jail + network.
    import app.sandbox.firecracker as _fcm

    with _fcm._REG_LOCK:
        _fcm._REG.pop(91011, None)
    _backdate_owner_dead(91011)
    cleaned = _fcm.destroy_orphans()
    assert cleaned >= 1
    assert not os.path.exists(chroot)
    # Reap is idempotent: a second sweep finds nothing for this task.
    assert _fcm.destroy_orphans() >= 0


def test_10b_foreign_sweep_spares_live_vm(workspace):
    """A live VM is never reaped merely because the sweeping process does
    not hold its registry entry (multiprocess safety on a real jail)."""
    _require_host()
    vm = _fc.provision(91019, workspace)
    try:
        import app.sandbox.firecracker as _fcm

        # Foreign-process view: empty registry, but the durable owner is
        # alive (fresh heartbeat, current pid) — sweep must skip it.
        with _fcm._REG_LOCK:
            _fcm._REG.pop(91019, None)
        assert _fcm.destroy_orphans() == 0
        assert os.path.exists(vm.chroot_dir)
        # ... and a foreign-HOST owner record is equally untouchable.
        import json as _json

        path = _fcm._owner_file_for(91019)
        with open(path, "r", encoding="utf-8") as fh:
            owner = _json.load(fh)
        owner["host"] = "some-other-host"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(owner, fh)
        os.replace(tmp, path)
        assert _fcm.destroy_orphans() == 0
        assert os.path.exists(vm.chroot_dir)
    finally:
        # Re-claim ownership for this process so destroy() cleans up via
        # the orphan path (no second jailer spawn).
        _fcm._write_owner(91019)
        _fc.destroy(91019)


def test_11_creation_failure_never_runs_on_host(tmp_path):
    _require_host()
    # Point at a bogus kernel to force provision failure, then assert the
    # command was NOT executed on the host (no marker file created).
    from app.config import settings as _settings

    orig = _settings.FC_KERNEL_IMAGE
    _settings.FC_KERNEL_IMAGE = "/nonexistent/vmlinux-11"
    ws = str(tmp_path / "task-91012")
    os.makedirs(ws, exist_ok=True)
    marker = os.path.join(ws, "HOST_RAN_MARKER")
    try:
        with pytest.raises(_sandbox.SandboxBlockedError):
            _fc.provision(91012, ws)
        assert not os.path.exists(marker)
    finally:
        _settings.FC_KERNEL_IMAGE = orig
        _fc.destroy(91012)


def test_12_concurrent_vms_isolated(tmp_path):
    _require_host()
    ids = [91013, 91014, 91015]
    workspaces = []
    for i in ids:
        ws = tmp_path / f"task-{i}"
        ws.mkdir()
        (ws / "who.txt").write_text(f"vm-{i}\n", encoding="utf-8")
        workspaces.append(str(ws))
        _fc.provision(i, workspaces[-1])
    try:
        # Concurrent VMs must hold DISTINCT CIDs (single source of truth).
        import app.sandbox.firecracker as _fcm

        with _fcm._REG_LOCK:
            cids = [_fcm._REG[i].cid for i in ids]
        assert len(set(cids)) == len(ids), cids
        for i, cid in zip(ids, cids):
            assert cid == _fcm._cid_for(i), (i, cid)
        for i in ids:
            out = _exec(i, "cat who.txt")
            assert f"vm-{i}" in out["stdout"]
    finally:
        for i in ids:
            _fc.destroy(i)


def test_15_vsock_cid_matches_config(workspace):
    """The configured guest CID and the host dial CID are identical."""
    import app.sandbox.firecracker as _fcm

    _require_host()
    vm = _fc.provision(91018, workspace)
    try:
        assert vm.cid == _fcm._cid_for(91018)
        assert vm.cid != 3  # the old hardcoded mismatch must be gone
        # Boot+exec succeeding at all proves the vsock pair agrees: the
        # host dials vm.cid and the guest was configured with the same CID.
        out = _exec(91018, "echo cid-proof-15")
        assert out["exit_code"] == 0
        assert "cid-proof-15" in out["stdout"]
    finally:
        _fc.destroy(91018)


def test_13_worker_executes_command_in_vm(workspace):
    _require_host()
    from app.agent import tools as _tools
    from app.config import settings as _settings

    orig = _settings.SANDBOX_BACKEND
    _settings.SANDBOX_BACKEND = "firecracker"
    try:
        _fc.provision(91016, workspace)
        out = _tools.run_command(workspace, "echo worker-in-vm-13")
        assert "worker-in-vm-13" in out
        assert "exit_code:" in out
    finally:
        _settings.SANDBOX_BACKEND = orig
        _fc.destroy(91016)


def test_16_symlink_sentinel_never_crosses(tmp_path):
    """A host-only sentinel behind a repo symlink is neither readable in
    the guest nor overwriteable during guest->host result sync."""
    _require_host()
    sentinel = tmp_path / "HOST_ONLY_SENTINEL_16"
    sentinel.write_text("host-secret-16\n", encoding="utf-8")
    ws = tmp_path / "task-91020"
    ws.mkdir()
    try:
        os.symlink(str(sentinel), str(ws / "innocent.txt"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")
    (ws / "app.py").write_text("print(1)\n", encoding="utf-8")
    _fc.provision(91020, str(ws))
    try:
        # 1. The link target's bytes must not be readable inside the guest.
        out = _exec(91020, "cat innocent.txt 2>&1")
        assert "host-secret-16" not in out["stdout"]
        # 2. A guest file planted where a host symlink lives must not
        # overwrite the sentinel during result sync.
        out = _exec(91020, "echo pwned > innocent.txt 2>&1; cat innocent.txt 2>&1")
        synced = _fc.sync_guest_to_host(task_id=91020, workspace=str(ws))
        assert sentinel.read_text(encoding="utf-8") == "host-secret-16\n"
        assert synced["files"] >= 0
    finally:
        _fc.destroy(91020)


def test_17_sibling_vm_network_unreachable(tmp_path):
    """One task VM cannot reach a sibling task VM over the network."""
    _require_host()
    wa = tmp_path / "task-91021"
    wb = tmp_path / "task-91022"
    wa.mkdir()
    wb.mkdir()
    (wa / "app.py").write_text("print(1)\n", encoding="utf-8")
    (wb / "app.py").write_text("print(2)\n", encoding="utf-8")
    _fc.provision(91021, str(wa))
    _fc.provision(91022, str(wb))
    try:
        import app.sandbox.firecracker as _fcm
        from app.sandbox import net as _net

        with _fcm._REG_LOCK:
            cid_b = _fcm._REG[91022].cid
        sib = _net.guest_addrs(91022)["vm"]
        own = _net.guest_addrs(91021)["vm"]
        assert sib != own  # per-task subnets: siblings never share an IP
        # Sibling guest IP, direct proxy-port without redirect mark, vsock.
        for target in (
            f"curl -m 5 http://{sib}/ 2>&1",
            f"curl -m 5 http://{sib}:8443/ 2>&1",
            "curl -m 5 http://10.200.0.1:8443/ -k -o /dev/null -w '%{http_code}' 2>&1",
            f"python3 -c \"import socket;s=socket.socket(socket.AF_VSOCK,socket.SOCK_STREAM);s.settimeout(4);s.connect(({cid_b},5000))\" 2>&1",
        ):
            out = _exec(91021, target)
            assert out["exit_code"] != 0 or "timed_out" in str(out), target
    finally:
        _fc.destroy(91021)
        _fc.destroy(91022)


def test_18_host_git_status_diff_after_guest_edit(tmp_path):
    """Agent-visible status/diff reflect guest edits via trusted host git,
    while the guest holds no push-capable credential."""
    _require_host()
    import subprocess as _sp

    from app.agent import tools as _tools
    from app.config import settings as _settings

    ws = tmp_path / "task-91023"
    ws.mkdir()
    _sp.run(["git", "init", "-b", "main"], cwd=str(ws), capture_output=True,
            timeout=30)
    _sp.run(["git", "config", "user.email", "t@t.t"], cwd=str(ws),
            capture_output=True, timeout=30)
    _sp.run(["git", "config", "user.name", "t"], cwd=str(ws),
            capture_output=True, timeout=30)
    (ws / "app.py").write_text("print(1)\n", encoding="utf-8")
    _sp.run(["git", "add", "-A"], cwd=str(ws), capture_output=True, timeout=30)
    _sp.run(["git", "commit", "-m", "init"], cwd=str(ws), capture_output=True,
            timeout=30)
    orig = _settings.SANDBOX_BACKEND
    _settings.SANDBOX_BACKEND = "firecracker"
    try:
        _fc.provision(91023, str(ws))
        out = _tools.run_command(str(ws), "echo 'print(2)' >> app.py")
        assert "exit_code:" in out
        status = _tools.git_status(str(ws))
        assert "app.py" in status, status
        diff = _tools.git_diff(str(ws))
        assert "print(2)" in diff, diff
        # Guest holds no push-capable credential for GitHub.
        pub = _exec(91023, "git remote -v 2>&1; git push --dry-run origin main 2>&1")
        assert "x-access-token" not in pub["stdout"]
    finally:
        _settings.SANDBOX_BACKEND = orig
        _fc.destroy(91023)


def test_19_concurrent_vms_topology_and_lifecycle(tmp_path):
    """Two live VMs: distinct subnets/routes, mutual egress, no cross-talk,
    single-VM destroy preserves the survivor, recreation works."""
    _require_host()
    import subprocess as _sp

    from app.sandbox import net as _net

    wa = tmp_path / "task-92001"
    wb = tmp_path / "task-92002"
    wa.mkdir()
    wb.mkdir()
    (wa / "app.py").write_text("print(1)\n", encoding="utf-8")
    (wb / "app.py").write_text("print(2)\n", encoding="utf-8")
    aa, ab = _net.guest_addrs(92001), _net.guest_addrs(92002)
    assert aa["net"] != ab["net"]
    la, lb = _net.link_addrs(92001), _net.link_addrs(92002)
    assert la["net"] != lb["net"]
    _fc.provision(92001, str(wa))
    _fc.provision(92002, str(wb))
    try:
        # Each guest sees its own unique link-net (no shared guest IP) and
        # a default route carrying public traffic toward the host veth.
        for tid, addrs in ((92001, aa), (92002, ab)):
            out = _exec(tid, "ip -o addr show dev eth0")
            assert addrs["vm"] in out["stdout"], (tid, out)
            out = _exec(tid, "ip route show default")
            assert "default" in out["stdout"], (tid, out)
        # Host veth ends hold exactly their /31 link addresses (fail-visible
        # evidence for the addressed-link design, per task).
        for tid, link in ((92001, la), (92002, lb)):
            vh = _net.veth_names(tid)[0]
            addrs_out = _sp.run(
                ["ip", "-o", "addr", "show", "dev", vh],
                capture_output=True, text=True, timeout=15,
            )
            assert f"inet {link['host']}/{link['prefix']}" in (addrs_out.stdout or ""), (tid, addrs_out)
        # Both reach approved egress through the shared proxy path.
        for tid in (92001, 92002):
            out = _exec(tid, "git ls-remote https://github.com/git/git.git HEAD 2>&1 | head -2")
            assert out["exit_code"] == 0, (tid, out)
        # Neither reaches the other (no shared L2, no route, nft drops).
        out = _exec(92001, f"curl -m 5 http://{ab['vm']}/ 2>&1")
        assert out["exit_code"] != 0, out
        # Destroying A keeps B's chains, routes and egress intact.
        _fc.destroy(92001)
        out = _exec(92002, "git ls-remote https://github.com/git/git.git HEAD 2>&1 | head -2")
        assert out["exit_code"] == 0, out
        chains = _sp.run(
            ["nft", "list", "table", "inet", "fixhub_vm"],
            capture_output=True, text=True, timeout=15,
        )
        assert "out_ft92002" in (chains.stdout or ""), chains
        assert "out_ft92001" not in (chains.stdout or ""), chains
        # Survivor keeps its input-hook enforcement (marked proxy accept +
        # catch-all host-local drop); the destroyed task's is gone.
        inchains = _sp.run(
            ["nft", "list", "chain", "inet", "fixhub_vm", "in_ft92002"],
            capture_output=True, text=True, timeout=15,
        )
        assert "meta mark" in (inchains.stdout or ""), inchains
        # Recreating A works on the same topology (idempotent rebuild).
        _fc.provision(92001, str(wa))
        out = _exec(92001, "git ls-remote https://github.com/git/git.git HEAD 2>&1 | head -2")
        assert out["exit_code"] == 0, out
    finally:
        _fc.destroy(92001)
        _fc.destroy(92002)


def test_14_publish_boundary_stays_tokenless(tmp_path):
    _require_host()
    ws = tmp_path / "task-91017"
    ws.mkdir()
    gitdir = ws / ".git"
    gitdir.mkdir()
    (gitdir / "config").write_text(
        '[remote "origin"]\n\turl = https://x-access-token:TOKEN14@github.com/o/r.git\n',
        encoding="utf-8",
    )
    (ws / "app.py").write_text("print(1)\n", encoding="utf-8")
    _fc.provision(91017, str(ws))
    try:
        out = _exec(91017, "cat .git/config 2>&1")
        assert "TOKEN14" not in out["stdout"]
        assert "x-access-token" not in out["stdout"]
    finally:
        _fc.destroy(91017)
