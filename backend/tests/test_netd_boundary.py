"""Privilege-boundary unit tests: fixhub-netd delegation (no root/KVM needed).

Covers operation validation, authorization boundaries, invalid identifiers,
task isolation, and failure handling for the netd client + daemon + the
delegating call sites in net.py / firecracker.py / egress.py.

Nothing here claims packet filtering: rule GENERATION is covered by
test_sandbox_backend.py; real filtering proof requires the KVM host suite
(test_sandbox_firecracker.py with FIXHUB_FIRECRACKER_TEST=1).
"""

import importlib.util
import json
import os
import socket as _socket_mod
import sys
import types

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# The daemon is Unix-first (pwd/grp for uid resolution). Stub them on
# platforms without them so the pure validation/dispatch logic is testable.
try:
    import pwd  # noqa: F401
except ImportError:  # pragma: no cover - Windows laptop
    # Faithful stub: real pwd raises KeyError for unknown ids. Complete with
    # getpwuid/getgrgid so unrelated stdlib consumers (tarfile) keep working
    # when this module is collected in the same pytest process.
    _pwd = types.ModuleType("pwd")

    def _missing(*_a, **_k):
        raise KeyError("no such user")

    _pwd.getpwnam = _missing
    _pwd.getpwuid = _missing
    try:
        import grp as _real_grp  # noqa: F401
    except ImportError:
        _pwd.getgrgid = _missing
        _pwd.getgrnam = _missing
    sys.modules["pwd"] = _pwd
try:
    import grp  # noqa: F401
except ImportError:  # pragma: no cover - Windows laptop
    _grp = types.ModuleType("grp")

    def _missing_grp(*_a, **_k):
        raise KeyError("no such group")

    _grp.getgrnam = _missing_grp
    _grp.getgrgid = _missing_grp
    sys.modules["grp"] = _grp

from app.sandbox import egress as _egress  # noqa: E402
from app.sandbox import firecracker as _fc  # noqa: E402
from app.sandbox import net as _net  # noqa: E402
from app.sandbox import netd_client as _client  # noqa: E402

_DAEMON_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.abspath(_client.__file__)),
        "..",
        "..",
        "..",
        "infra",
        "firecracker",
        "fixhub-netd.py",
    )
)


def _load_daemon():
    spec = importlib.util.spec_from_file_location("fixhub_netd", _DAEMON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    _daemon = _load_daemon()
    _DAEMON_LOAD_ERROR = ""
except Exception as exc:  # pragma: no cover - surfaces as a test failure below
    _daemon = None
    _DAEMON_LOAD_ERROR = str(exc)


def test_daemon_module_loads():
    assert _daemon is not None, f"fixhub-netd.py must import cleanly: {_DAEMON_LOAD_ERROR}"


def test_daemon_run_server_refuses_without_unix_sockets(monkeypatch, tmp_path):
    monkeypatch.delattr(_socket_mod, "AF_UNIX", raising=False)
    with pytest.raises(RuntimeError, match="refusing start"):
        _daemon.run_server(
            str(tmp_path / "x.sock"), allowed_uid=1001, backend_dir=str(tmp_path)
        )


_NEEDS_UDS = pytest.mark.skipif(
    not hasattr(_socket_mod, "AF_UNIX"),
    reason="Unix sockets unavailable on this platform",
)


# ------------------------------------------------- task id validation ----
@pytest.mark.parametrize(
    "bad", [0, -1, -100, 2**31, 10**15, "0", "-5", "abc", "", "  ",
            "1; rm -rf /", "10.200.0.1", "0x10", "1.5", None, True, False,
            1.5, b"7", ["7"], {"id": 7}, "fixhub-t7", "７"],
)
def test_validate_task_id_rejects(bad):
    with pytest.raises(_client.NetdError):
        _client.validate_task_id(bad)


@pytest.mark.parametrize("good", [1, 7, 91001, "7", "91001", "  42  "])
def test_validate_task_id_accepts(good):
    assert _client.validate_task_id(good) == int(str(good).strip())


def test_validate_spawn_resources_bounds():
    assert _client.validate_spawn_resources(2, 1024, 256) == (2, 1024, 256)
    for bad in (
        (0, 1024, 256), (17, 1024, 256), (2, 64, 256), (2, 99999, 256),
        (2, 1024, 0), ("2", 1024, 256), (2.0, 1024, 256), (True, 1, 1),
        (None, 1024, 256),
    ):
        with pytest.raises(_client.NetdError):
            _client.validate_spawn_resources(*bad)


# ------------------------------------------------- activation rule ----
def test_delegation_requires_explicit_socket(monkeypatch):
    monkeypatch.delenv("FC_NETD_SOCKET", raising=False)
    monkeypatch.delenv("FC_NETD_DIRECT", raising=False)
    assert _client.delegation_active() is False


def test_delegation_opt_out_wins(monkeypatch):
    monkeypatch.setenv("FC_NETD_SOCKET", "/run/fixhub-netd.sock")
    monkeypatch.setenv("FC_NETD_DIRECT", "1")
    assert _client.delegation_active() is False


def test_delegation_never_as_root(monkeypatch):
    monkeypatch.setenv("FC_NETD_SOCKET", "/run/fixhub-netd.sock")
    monkeypatch.delenv("FC_NETD_DIRECT", raising=False)
    monkeypatch.setattr(_client.os, "geteuid", lambda: 0, raising=False)
    assert _client.delegation_active() is False


def test_delegation_active_when_configured_unprivileged(monkeypatch):
    monkeypatch.setenv("FC_NETD_SOCKET", "/run/fixhub-netd.sock")
    monkeypatch.delenv("FC_NETD_DIRECT", raising=False)
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(_client.os, "geteuid", lambda: 1000, raising=False)
    assert _client.delegation_active() is True


def test_firecracker_delegation_mirrors_client(monkeypatch):
    monkeypatch.delenv("FC_NETD_SOCKET", raising=False)
    assert _fc._delegated() is False
    monkeypatch.setenv("FC_NETD_SOCKET", "/run/fixhub-netd.sock")
    monkeypatch.delenv("FC_NETD_DIRECT", raising=False)
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(_client.os, "geteuid", lambda: 1000, raising=False)
    assert _fc._delegated() is True


# ------------------------------------------------- wire schema ----
def test_client_rejects_unknown_op():
    with pytest.raises(_client.NetdError):
        _client._call("exec")


def test_client_rejects_forbidden_fields():
    with pytest.raises(_client.NetdError):
        _client._call("setup", {"task_id": 7, "iface": "lo"})
    with pytest.raises(_client.NetdError):
        _client._call("setup", {"task_id": 7, "argv": ["ip", "link"]})
    with pytest.raises(_client.NetdError):
        _client._call("setup", {"task_id": 7, "path": "/usr/local/bin/jailer"})


def _code_without_strings(path):
    """Source with every string literal blanked (prose must not trip checks)."""
    import ast as _ast

    text = open(path, encoding="utf-8").read()
    tree = _ast.parse(text)
    spans = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Constant) and isinstance(node.value, str):
            spans.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
    lines = text.splitlines(keepends=True)
    line_offsets = [0]
    for line in lines:
        line_offsets.append(line_offsets[-1] + len(line))

    def _off(lineno, col):
        return line_offsets[lineno - 1] + col

    chars = list(text)
    for start_line, start_col, end_line, end_col in spans:
        for i in range(_off(start_line, start_col), _off(end_line, end_col)):
            if chars[i] not in ("\n",):
                chars[i] = " "
    return "".join(chars)


def test_client_has_no_command_runner():
    import app.sandbox.netd_client as _mod

    code = _code_without_strings(_mod.__file__)
    assert "shell=True" not in code
    assert "subprocess" not in code
    assert "Popen" not in code


def test_daemon_has_no_shell():
    assert _daemon is not None, _DAEMON_LOAD_ERROR
    code = _code_without_strings(_DAEMON_PATH)
    assert "shell=True" not in code


# ------------------------------------------------- daemon auth ----
@pytest.mark.parametrize("op", sorted(_client.OPS))
def test_daemon_rejects_unauthorized_peer_for_every_op(op):
    req = {"op": op, "task_id": 7}
    if op == "spawn-jailer":
        req.update({"vcpu": 2, "mem_mib": 1024, "pids_max": 256})
    resp = _daemon.handle_request(req, peer_uid=99999, allowed_uid=1001)
    assert resp["ok"] is False
    assert "unauthorized" in resp["error"]


def test_daemon_rejects_unknown_peer_and_missing_auth():
    assert _daemon.handle_request(
        {"op": "probe"}, peer_uid=None, allowed_uid=1001
    )["ok"] is False
    assert _daemon.handle_request(
        {"op": "probe"}, peer_uid=1001, allowed_uid=None
    )["ok"] is False


def test_daemon_rejects_unknown_op_and_forbidden_fields():
    assert _daemon.handle_request(
        {"op": "exec", "task_id": 7}, peer_uid=1001, allowed_uid=1001
    )["ok"] is False
    for evil in (
        {"op": "setup", "task_id": 7, "iface": "lo"},
        {"op": "setup", "task_id": 7, "ip": "10.200.0.1"},
        {"op": "teardown", "task_id": 7, "chains": ["pre_x"]},
        {"op": "setup", "task_id": 7, "argv": ["nft", "flush", "ruleset"]},
        {"op": "spawn-jailer", "task_id": 7, "vcpu": 2, "mem_mib": 1024,
         "pids_max": 256, "exec-file": "/tmp/evil"},
        "just a string",
        ["setup"],
        None,
    ):
        resp = _daemon.handle_request(evil, peer_uid=1001, allowed_uid=1001)
        assert resp["ok"] is False, evil


@pytest.mark.parametrize(
    "bad", [0, -3, "abc", "", None, True, "7;id", 2**31],
)
def test_daemon_rejects_invalid_task_ids(bad):
    resp = _daemon.handle_request(
        {"op": "setup", "task_id": bad}, peer_uid=1001, allowed_uid=1001
    )
    assert resp["ok"] is False


def test_daemon_probe_proves_authorization():
    resp = _daemon.handle_request({"op": "probe"}, peer_uid=1001, allowed_uid=1001)
    assert resp["ok"] is True
    assert resp["data"]["uid"] == 1001
    assert set(resp["data"]["ops"]) == set(_client.OPS)


# ------------------------------------------------- server-side derivation ----
class _StubNet:
    def __init__(self):
        self.calls = []

    def ensure_isolation(self, task_id):
        self.calls.append(("setup", task_id))
        return {"netns": f"fixhub-t{task_id}", "tap": f"ft{task_id}",
                "nft_applied": True, "proxy": {"proxy_port": 8443}}

    def destroy_isolation(self, task_id):
        self.calls.append(("teardown", task_id))


class _StubEgress:
    @staticmethod
    def _probe_available():
        return True, "stub"


def test_daemon_derives_everything_from_task_id():
    stub = _StubNet()
    # Even a well-formed request carries no names/addrs/chains: the daemon
    # passes the bare task id to the canonical implementation.
    resp = _daemon.handle_request(
        {"op": "setup", "task_id": 7}, peer_uid=1001, allowed_uid=1001,
        netmod=stub, egressmod=_StubEgress(),
    )
    assert resp["ok"] is True
    assert stub.calls == [("setup", 7)]
    assert resp["data"]["netns"] == "fixhub-t7"


def test_daemon_setup_fails_closed_when_egress_down():
    class _Down:
        @staticmethod
        def _probe_available():
            return False, "proxy unreachable"

    stub = _StubNet()
    resp = _daemon.handle_request(
        {"op": "setup", "task_id": 7}, peer_uid=1001, allowed_uid=1001,
        netmod=stub, egressmod=_Down(),
    )
    assert resp["ok"] is False
    assert "egress" in resp["error"]
    assert stub.calls == []  # network setup never starts without egress


def test_daemon_teardown_never_raises():
    class _Boom:
        def destroy_isolation(self, task_id):
            raise RuntimeError("boom")

    resp = _daemon.handle_request(
        {"op": "teardown", "task_id": 7}, peer_uid=1001, allowed_uid=1001,
        netmod=_Boom(),
    )
    assert resp["ok"] is True


def test_daemon_never_raises_on_handler_crash():
    class _Boom:
        def ensure_isolation(self, task_id):
            raise ValueError("unserializable-\udc80? no: plain boom")

    resp = _daemon.handle_request(
        {"op": "setup", "task_id": 7}, peer_uid=1001, allowed_uid=1001,
        netmod=_Boom(), egressmod=_StubEgress(),
    )
    assert resp["ok"] is False
    json.dumps(resp)  # response itself must stay serializable


# ------------------------------------------------- task isolation ----
def test_teardown_inventory_never_covers_shared_state():
    t7 = _net._teardown_targets(7)
    t8 = _net._teardown_targets(8)
    assert t7["netns"] != t8["netns"]
    assert t7["vh"] != t8["vh"] and t7["tap"] != t8["tap"]
    assert t7["chains"] != t8["chains"] and t7["net"] != t8["net"]
    blob = json.dumps(t7)
    assert t7["vh"] != "lo" and t7["tap"] != "lo"
    assert _net.HOST_SVC_IP not in blob  # loopback service addr never torn down
    assert "fixhub_vm" not in blob  # table itself (shared) never addressed


def test_per_task_derivation_matches_firecracker_contract(monkeypatch):
    # Daemon and backend must agree on jail identity (drift = wrong-VM kill).
    assert _daemon.jail_id_for(91021) == _fc.jail_id_for(91021) == "task-91021"
    assert _daemon.vm_uid_gid(91021) == (100000 + 91021 % 50000,) * 2
    monkeypatch.setattr(
        "app.sandbox.firecracker._cfg",
        lambda name, default="": (
            "/srv/firecracker/jails" if name == "FC_CHROOT_BASE" else default
        ),
    )
    assert _daemon.chroot_dir_for(91021, "/srv/firecracker/jails") == \
        _fc.chroot_dir_for(91021)


def test_daemon_cgroup_builder_matches_backend():
    import unittest.mock as _mock

    with _mock.patch.object(_fc, "cgroup_version", lambda: "2"):
        _v2, _a2 = _fc.jailer_cgroup_args(vcpu=2, mem_mib=1024, pids_max=256)
    with _mock.patch.object(_fc, "cgroup_version", lambda: "1"):
        _v1, _a1 = _fc.jailer_cgroup_args(vcpu=2, mem_mib=1024, pids_max=256)
    d2 = _daemon.build_cgroup_args(version="2", vcpu=2, mem_mib=1024, pids_max=256)
    d1 = _daemon.build_cgroup_args(version="1", vcpu=2, mem_mib=1024, pids_max=256)
    assert d2 == (_v2, _a2)
    assert d1 == (_v1, _a1)
    with pytest.raises(RuntimeError):
        _daemon.build_cgroup_args(version="", vcpu=2, mem_mib=1024, pids_max=256)


def test_daemon_defaults_match_app_config():
    from app.config import settings as _settings

    for key, want in _daemon._DEFAULTS.items():
        got = str(getattr(_settings, key, ""))
        if "VCPU" in key or "MEM" in key or "PIDS" in key:
            assert str(int(got)) == str(int(want)), key
        else:
            assert got == want, key


# ------------------------------------------------- failure handling ----
def test_setup_unreachable_daemon_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("FC_NETD_SOCKET", str(tmp_path / "no-such.sock"))
    monkeypatch.delenv("FC_NETD_DIRECT", raising=False)
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(_client.os, "geteuid", lambda: 1000, raising=False)
    assert _client.delegation_active() is True
    with pytest.raises(_client.NetdError, match="fixhub-netd"):
        _client.setup(7)
    # And the net layer maps it to fail-closed isolation refusal (no fallback).
    with pytest.raises(_net.NetworkIsolationError):
        _net.ensure_isolation(7)


def test_setup_daemon_error_fails_closed_no_fallback(monkeypatch):
    monkeypatch.setenv("FC_NETD_SOCKET", "/run/fixhub-netd.sock")
    monkeypatch.delenv("FC_NETD_DIRECT", raising=False)
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(_client.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        _client, "setup", lambda tid: (_ for _ in ()).throw(_client.NetdError("down"))
    )
    with pytest.raises(_net.NetworkIsolationError, match="down"):
        _net.ensure_isolation(7)


def test_setup_incomplete_topology_refuses_boot(monkeypatch):
    monkeypatch.setenv("FC_NETD_SOCKET", "/run/fixhub-netd.sock")
    monkeypatch.delenv("FC_NETD_DIRECT", raising=False)
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(_client.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        _client, "setup",
        lambda tid: {"netns": "", "tap": "", "nft_applied": True},
    )
    with pytest.raises(_net.NetworkIsolationError, match="incomplete"):
        _net.ensure_isolation(7)
    monkeypatch.setattr(
        _client, "setup",
        lambda tid: {"netns": "fixhub-t7", "tap": "ft7", "nft_applied": False},
    )
    with pytest.raises(_net.NetworkIsolationError, match="nft"):
        _net.ensure_isolation(7)


def test_setup_success_shape(monkeypatch):
    monkeypatch.setenv("FC_NETD_SOCKET", "/run/fixhub-netd.sock")
    monkeypatch.delenv("FC_NETD_DIRECT", raising=False)
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(_client.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        _client, "setup",
        lambda tid: {"netns": "fixhub-t7", "tap": "ft7", "nft_applied": True,
                     "proxy": {"proxy_port": 8443}},
    )
    info = _net.ensure_isolation(7)
    assert info == {"netns": "fixhub-t7", "tap": "ft7", "nft_applied": True,
                    "proxy": {"proxy_port": 8443}}


def test_destroy_delegation_never_raises(monkeypatch):
    monkeypatch.setenv("FC_NETD_SOCKET", "/run/fixhub-netd.sock")
    monkeypatch.delenv("FC_NETD_DIRECT", raising=False)
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(_client.os, "geteuid", lambda: 1000, raising=False)

    def _boom(_tid):
        raise _client.NetdError("daemon gone")

    monkeypatch.setattr(_client, "teardown", _boom)
    _net.destroy_isolation(7)  # never raises by contract


@_NEEDS_UDS
def test_malformed_daemon_response_is_netd_error(monkeypatch, tmp_path):
    import socket as _socket

    path = str(tmp_path / "fake.sock")
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def _serve():
        conn, _ = srv.accept()
        try:
            conn.recv(65536)
            conn.sendall(b"not-json\n")
        finally:
            conn.close()
            srv.close()

    import threading as _threading

    _threading.Thread(target=_serve, daemon=True).start()
    monkeypatch.setenv("FC_NETD_SOCKET", path)
    with pytest.raises(_client.NetdError, match="unreadable"):
        _client.probe()


@_NEEDS_UDS
def test_daemon_error_response_is_netd_error(monkeypatch, tmp_path):
    import socket as _socket

    path = str(tmp_path / "fake2.sock")
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def _serve():
        conn, _ = srv.accept()
        try:
            conn.recv(65536)
            conn.sendall(b'{"ok": false, "error": "nope"}\n')
        finally:
            conn.close()
            srv.close()

    import threading as _threading

    _threading.Thread(target=_serve, daemon=True).start()
    monkeypatch.setenv("FC_NETD_SOCKET", path)
    with pytest.raises(_client.NetdError, match="nope"):
        _client.probe()


# ------------------------------------------------- egress posture ----
def test_egress_ensure_available_names_service_when_down(monkeypatch):
    monkeypatch.delenv("FC_EGRESS_AUTOSPAWN", raising=False)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(_egress, "_probe_available", lambda: (False, "proxy down"))
    with pytest.raises(RuntimeError, match="fixhub-egress"):
        _egress.ensure_available()


def test_egress_autospawn_opt_in_only(monkeypatch):
    assert _egress._autospawn_allowed() is False
    monkeypatch.setenv("FC_EGRESS_AUTOSPAWN", "1")
    assert _egress._autospawn_allowed() is True
