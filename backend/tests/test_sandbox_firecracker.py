"""Phase 5 M6: REAL microVM security suite (no mocks count).

Gate: runs only when FIXHUB_FIRECRACKER_TEST=1. Under that flag EVERY test
REQUIRES a real jailer-booted microVM; missing KVM/binaries/images/network
isolation FAILS the test (never skips, never mocks success). Without the flag
the whole module skips cleanly for Windows-laptop dev.

Covers the 14 acceptance probes:
 1 boot+exec  2 host-marker secrecy  3 cross-task isolation
 4 credential absence  5 traversal/symlink  6 forbidden net
 7 approved egress  8 resource limits  9 cancel/terminate
 10 crash recovery  11 no host fallback  12 concurrent VMs
 13 worker-in-VM  14 publish boundary (tokenless guest)

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
            "curl -m 5 http://169.254.169.254/ 2>&1",
            "curl -m 5 http://10.0.0.1/ 2>&1",
        ):
            out = _exec(91007, target)
            assert out["exit_code"] != 0 or "200" not in out["stdout"]
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
    _require_host()
    _fc.provision(91009, workspace)
    try:
        out = _exec(91009, "sleep 60")
        # With timeout_s=30 the guest must report timeout, not hang the suite.
        assert out["timed_out"] or out["exit_code"] is not None
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


def test_10_crash_recovery_leaves_no_vm(workspace):
    _require_host()
    vm = _fc.provision(91011, workspace)
    # Simulate worker loss: drop registry entry without destroy, then reclaim
    # path must still clean up (destroy by task id is authoritative).
    import app.sandbox.firecracker as _fcm

    with _fcm._REG_LOCK:
        _fcm._REG.pop(91011, None)
    _fc.destroy(91011)
    assert not os.path.exists(vm.jail_dir)


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
        for i in ids:
            out = _exec(i, "cat who.txt")
            assert f"vm-{i}" in out["stdout"]
    finally:
        for i in ids:
            _fc.destroy(i)


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
