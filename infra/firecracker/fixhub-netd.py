#!/usr/bin/env python3
"""fixhub-netd: root-owned privilege boundary for Firecracker sandbox networking.

Runs as root under systemd (``infra/systemd/fixhub-netd.service``). The
unprivileged API (``User=fixhub``, ``NoNewPrivileges=true``) talks to it over
a Unix-domain socket (``/run/fixhub-netd.sock``, ``root:fixhub`` mode 0770).

WHY A DAEMON (and not setuid / sudo / file capabilities):
``NoNewPrivileges=true`` on the API unit makes the kernel block ALL privilege
gain on exec — setuid-root helpers, sudo, and file caps are all dead ends
from that process. A separately-started root service is the only delegation
that works without weakening the API's hardening.

STRICT, TYPED INTERFACE (never a generic command runner):
* op enum: probe | setup | teardown | prepare-jail | finalize-jail |
  spawn-jailer | destroy-jail. Unknown ops are rejected.
* the client may send ONLY: op, task_id, and (for spawn-jailer) three bounded
  resource scalars. Interface names, IPs, routes, nftables chains, file
  paths, executable paths, and argv are ALWAYS derived here from task_id.
* no ``shell=True`` anywhere; subprocesses use fixed argv lists.
* every connection is authenticated with SO_PEERCRED: only the configured
  API uid (default: the ``fixhub`` user) is served; everyone else is rejected
  before any validation or execution.
* network enforcement itself is NOT reimplemented here: setup/teardown call
  the canonical ``app.sandbox.net.ensure_isolation/destroy_isolation`` (same
  code the direct path uses), so nft rules, address pools, and fail-closed
  semantics cannot drift between modes.

Stdlib only. Backend code (``app.sandbox.net``, ``app.sandbox.egress``,
``app.sandbox.netd_client`` for validation) is imported at startup from the
backend checkout; a missing import fails closed with a clear error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
try:
    import pwd
except ImportError:  # Windows/dev: uid resolution unavailable, fail closed later
    pwd = None  # type: ignore[assignment]
try:
    import grp
except ImportError:  # Windows/dev: gid resolution unavailable, fail closed later
    grp = None  # type: ignore[assignment]
import signal
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

LOG = logging.getLogger("fixhub-netd")

# Closed wire schema: these are the ONLY keys a client may send.
_ALLOWED_KEYS = frozenset({"op", "task_id", "vcpu", "mem_mib", "pids_max"})
OPS = frozenset(
    {
        "probe",
        "setup",
        "teardown",
        "prepare-jail",
        "finalize-jail",
        "spawn-jailer",
        "grant-vsock",
        "destroy-jail",
    }
)
_MAX_REQUEST_BYTES = 64 * 1024
_SPAWN_WAIT_S = 15
_CGROUP_PARENT = "fixhub"


# ---------------------------------------------------------------- config ----
# Defaults mirror app.config.Settings for the keys used here (same env names,
# so one host env configures both). A unit test asserts they stay in sync.
_DEFAULTS = {
    "FC_BINARY": "/usr/local/bin/firecracker",
    "JAILER_BINARY": "/usr/local/bin/jailer",
    "FC_KERNEL_IMAGE": "/srv/firecracker/kernel/vmlinux",
    "FC_ROOTFS_IMAGE": "/srv/firecracker/rootfs/base.ext4",
    "FC_CHROOT_BASE": "/srv/firecracker/jails",
    "FC_GUEST_VCPU": "2",
    "FC_GUEST_MEM_MIB": "1024",
    "FC_PIDS_MAX": "256",
}


def _cfg(name: str) -> str:
    value = (os.environ.get(name, "") or "").strip()
    if value:
        return value
    return _DEFAULTS.get(name, "")


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(_cfg(name) or default)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------- pure validation ----
def resolve_allowed_uid(explicit: str = "") -> int | None:
    """Uid the daemon serves. Explicit value wins, else the fixhub user."""
    text = (explicit or os.environ.get("FC_NETD_ALLOWED_UID", "") or "").strip()
    if text:
        try:
            uid = int(text, 10)
        except ValueError:
            return None
        return uid if uid >= 0 else None
    if pwd is None:
        return None
    try:
        return pwd.getpwnam("fixhub").pw_uid
    except KeyError:
        return None


def resolve_fixhub_ids() -> tuple[int, int] | None:
    """(uid, gid) of the fixhub service user, or None when unknown."""
    if pwd is None:
        return None
    try:
        entry = pwd.getpwnam("fixhub")
        return entry.pw_uid, entry.pw_gid
    except KeyError:
        return None


def validate_request(
    req: object, *, peer_uid: int | None, allowed_uid: int | None
) -> tuple[bool, str, dict]:
    """Authenticate + validate one decoded request.

    Returns (ok, error, normalized). Normalized is {op, task_id, vcpu,
    mem_mib, pids_max} with only the fields each op needs. NOTHING
    client-supplied beyond these scalars is honored downstream.
    """
    if allowed_uid is None or peer_uid is None or peer_uid != allowed_uid:
        return False, "unauthorized peer", {}
    if not isinstance(req, dict):
        return False, "request must be a JSON object", {}
    for key in req:
        if key not in _ALLOWED_KEYS:
            return False, f"forbidden field: {key!r}", {}
    op = req.get("op")
    if op not in OPS:
        return False, f"unknown op: {op!r}", {}
    if op == "probe":
        return True, "", {"op": op}
    # All other ops require a strictly validated task_id. Import the single
    # canonical validator (stdlib-only module, safe to import anywhere).
    try:
        from app.sandbox import netd_client as _client
    except Exception as exc:  # pragma: no cover - daemon misconfigured
        return False, f"validator unavailable: {exc}", {}
    try:
        tid = _client.validate_task_id(req.get("task_id"))
    except Exception as exc:
        return False, str(exc)[:200], {}
    normalized: dict = {"op": op, "task_id": tid}
    if op == "spawn-jailer":
        try:
            vcpu, mem_mib, pids_max = _client.validate_spawn_resources(
                req.get("vcpu"), req.get("mem_mib"), req.get("pids_max")
            )
        except Exception as exc:
            return False, str(exc)[:200], {}
        normalized.update({"vcpu": vcpu, "mem_mib": mem_mib, "pids_max": pids_max})
    return True, "", normalized


# ------------------------------------------------- derived jail values ----
def jail_id_for(task_id: int) -> str:
    return f"task-{int(task_id)}"


def vm_uid_gid(task_id: int) -> tuple[int, int]:
    """Per-VM uid:gid. MUST match firecracker.provision (drift-guard tested)."""
    uid = 100000 + (int(task_id) % 50000)
    return uid, uid


def chroot_dir_for(task_id: int, base: str = "") -> str:
    root = base or _cfg("FC_CHROOT_BASE")
    return os.path.join(root, "firecracker", jail_id_for(task_id), "root")


def api_socket_for(chroot_dir: str) -> str:
    return os.path.join(chroot_dir, "api.socket")


def netns_path_for(task_id: int) -> str:
    from app.sandbox import net as _net

    return f"/var/run/netns/{_net.netns_name(int(task_id))}"


def cgroup_version() -> str:
    try:
        if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
            return "2"
        if os.path.isdir("/sys/fs/cgroup/cpu") or os.path.isdir("/sys/fs/cgroup/memory"):
            return "1"
    except Exception:
        pass
    return ""


def build_cgroup_args(
    *, version: str, vcpu: int, mem_mib: int, pids_max: int
) -> tuple[str, list[str]]:
    """Jailer --cgroup args. MUST match firecracker.jailer_cgroup_args."""
    mem_bytes = int(mem_mib) * 1024 * 1024
    if version == "2":
        quota = int(vcpu) * 100000
        return version, [
            "--cgroup", f"cpu.max={quota} 100000",
            "--cgroup", f"memory.max={mem_bytes}",
            "--cgroup", f"pids.max={int(pids_max)}",
        ]
    if version == "1":
        quota = int(vcpu) * 100000
        return version, [
            "--cgroup", f"cpu.cfs_quota_us={quota}",
            "--cgroup", "cpu.cfs_period_us=100000",
            "--cgroup", f"memory.limit_in_bytes={mem_bytes}",
            "--cgroup", f"pids.max={int(pids_max)}",
        ]
    raise RuntimeError("cgroup layout undetectable (need /sys/fs/cgroup); refusing boot")


def _read_fc_pid(chroot_dir: str) -> int | None:
    for name in ("firecracker.pid", "jailer.pid"):
        path = os.path.join(chroot_dir, name)
        parent_path = os.path.join(os.path.dirname(chroot_dir), name)
        for candidate in (path, parent_path):
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    return int(fh.read().strip().split()[0])
            except (OSError, ValueError, IndexError):
                continue
    return None


def _kill_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        import signal as _signal

        os.kill(int(pid), _signal.SIGKILL)
    except Exception:
        pass


def _kill_task_procs(jail_id: str) -> None:
    if not jail_id.startswith("task-") or not jail_id[5:].isdigit():
        return
    try:
        subprocess.run(
            ["pkill", "-f", "--", f"--id {jail_id}( |$)"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def _remove_jail_tree(task_id: int, base: str = "") -> None:
    root = base or _cfg("FC_CHROOT_BASE")
    for candidate in (
        os.path.join(root, "firecracker", jail_id_for(task_id)),
        os.path.join(root, jail_id_for(task_id)),
    ):
        try:
            if os.path.isdir(candidate):
                shutil.rmtree(candidate, ignore_errors=True)
        except Exception:
            pass


# ------------------------------------------------------------- operations ----
_PROCS: dict[int, subprocess.Popen] = {}
_PROCS_LOCK = threading.Lock()


def op_probe(peer_uid: int | None) -> dict:
    return {"uid": peer_uid, "ops": sorted(OPS)}


def op_setup(task_id: int, netmod) -> dict:
    """Canonical isolation setup (same code as the direct path). Fail closed."""
    return dict(netmod.ensure_isolation(task_id))


def op_teardown(task_id: int, netmod) -> dict:
    try:
        netmod.destroy_isolation(task_id)
    except Exception as exc:
        LOG.warning("teardown task=%s non-fatal: %s", task_id, exc)
    return {}


def op_prepare_jail(task_id: int, fixhub_ids: tuple[int, int] | None) -> dict:
    """mkdir the jail tree as root, hand ownership to fixhub for staging."""
    if fixhub_ids is None:
        raise RuntimeError("fixhub service user unknown; refusing jail prep")
    uid, gid = fixhub_ids
    chroot = chroot_dir_for(task_id)
    id_dir = os.path.dirname(chroot)
    os.makedirs(chroot, exist_ok=True)
    os.chmod(id_dir, 0o700)
    os.chmod(chroot, 0o700)
    os.chown(id_dir, uid, gid)
    os.chown(chroot, uid, gid)
    return {"chroot": chroot}


def op_finalize_jail(task_id: int) -> dict:
    """Chown staged kernel+overlay to the per-VM uid:gid (mirrors the direct path)."""
    uid, gid = vm_uid_gid(task_id)
    chroot = chroot_dir_for(task_id)
    kernel_base = os.path.basename(_cfg("FC_KERNEL_IMAGE")) or "vmlinux"
    kernel_dest = os.path.join(chroot, kernel_base)
    overlay = os.path.join(chroot, "overlay.ext4")
    for path in (kernel_dest, overlay):
        if not os.path.isfile(path):
            raise RuntimeError(f"staged file missing for chown: {path}")
    os.chown(kernel_dest, uid, gid)
    os.chown(overlay, uid, gid)
    return {"uid": uid, "gid": gid}


def _grant_vm_socket_access(
    task_id: int, socket_name: str, socket_path: str,
    fixhub_ids: tuple[int, int] | None,
) -> None:
    """Grant fixhub access to one known socket inside a single VM jail.

    Only the two fixed socket names are accepted. Their paths are derived from
    task_id; no client-supplied filesystem path reaches this function.
    """
    if socket_name not in {"api.socket", "v.sock"}:
        raise RuntimeError("unsupported VM socket name")
    if fixhub_ids is None:
        raise RuntimeError("fixhub service user unknown; refusing socket access")
    _fixhub_uid, fixhub_gid = fixhub_ids
    chroot = chroot_dir_for(task_id)
    expected_socket = os.path.join(chroot, socket_name)
    if os.path.abspath(socket_path) != os.path.abspath(expected_socket):
        raise RuntimeError("unexpected Firecracker socket path")

    base = os.path.abspath(_cfg("FC_CHROOT_BASE"))
    id_dir = os.path.dirname(chroot)
    for path in (base, os.path.join(base, "firecracker"), id_dir, chroot):
        try:
            st = os.lstat(path)
        except OSError as exc:
            raise RuntimeError(f"jail directory unavailable {path}: {exc}") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"unsafe jail directory component: {path}")

    # FixHub gets traversal only, not directory write access.
    for path in (id_dir, chroot):
        os.chown(path, -1, fixhub_gid)
        os.chmod(path, 0o710)

    try:
        st = os.lstat(socket_path)
    except OSError as exc:
        raise RuntimeError(f"Firecracker socket unavailable: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError("Firecracker path is not a Unix socket")
    os.chown(socket_path, -1, fixhub_gid)
    os.chmod(socket_path, 0o660)


def _grant_api_socket_access(
    task_id: int, api_socket: str, fixhub_ids: tuple[int, int] | None
) -> None:
    _grant_vm_socket_access(task_id, "api.socket", api_socket, fixhub_ids)


def _grant_vsock_socket_access(
    task_id: int, fixhub_ids: tuple[int, int] | None
) -> None:
    chroot = chroot_dir_for(task_id)
    _grant_vm_socket_access(
        task_id, "v.sock", os.path.join(chroot, "v.sock"), fixhub_ids
    )


def op_grant_vsock(task_id: int, fixhub_ids: tuple[int, int] | None) -> dict:
    _grant_vsock_socket_access(task_id, fixhub_ids)
    return {"vsock_socket": api_socket_for(chroot_dir_for(task_id)).replace("api.socket", "v.sock")}


def op_spawn_jailer(
    task_id: int, *, vcpu: int, mem_mib: int, pids_max: int,
    fixhub_ids: tuple[int, int] | None = None,
) -> dict:
    """Exec the jailer as root with fully server-derived args; wait for socket."""
    jailer = _cfg("JAILER_BINARY")
    fc = _cfg("FC_BINARY")
    chroot_base = _cfg("FC_CHROOT_BASE")
    for label, path in (("jailer", jailer), ("firecracker", fc)):
        if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise RuntimeError(f"{label} binary unusable: {path or '(unset)'}")
    version = cgroup_version()
    _, cgroup_args = build_cgroup_args(
        version=version, vcpu=vcpu, mem_mib=mem_mib, pids_max=pids_max
    )
    jail_id = jail_id_for(task_id)
    uid, gid = vm_uid_gid(task_id)
    chroot = chroot_dir_for(task_id, chroot_base)
    api_socket = api_socket_for(chroot)
    netns_path = netns_path_for(task_id)
    if not os.path.exists(netns_path):
        raise RuntimeError(
            f"netns {netns_path} missing (run setup first); refusing jailer spawn"
        )
    try:
        overlay_bytes = os.path.getsize(os.path.join(chroot, "overlay.ext4"))
    except OSError:
        overlay_bytes = 0
    fsize_cap = overlay_bytes + int(mem_mib) * 1024 * 1024 + 64 * 1024 * 1024
    id_dir = os.path.dirname(chroot)
    log_path = os.path.join(id_dir, "jailer.log")
    cmd = [
        jailer,
        "--id", jail_id,
        "--exec-file", fc,
        "--uid", str(uid),
        "--gid", str(gid),
        "--chroot-base-dir", chroot_base,
        "--cgroup-version", version,
        "--parent-cgroup", _CGROUP_PARENT,
        *cgroup_args,
        "--resource-limit", "no-file=1024",
        "--resource-limit", f"fsize={fsize_cap}",
        "--netns", netns_path,
        "--new-pid-ns",
        "--",
        "--api-sock", "/api.socket",
    ]
    try:
        log_fh = open(log_path, "ab")
    except OSError as exc:
        raise RuntimeError(f"could not open jailer log {log_path}: {exc}")
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        try:
            log_fh.close()
        except Exception:
            pass
        raise RuntimeError(f"jailer exec failed: {exc}")
    finally:
        try:
            log_fh.close()
        except Exception:
            pass
    deadline = time.monotonic() + _SPAWN_WAIT_S
    while time.monotonic() < deadline:
        if os.path.exists(api_socket):
            _grant_api_socket_access(task_id, api_socket, fixhub_ids)
            with _PROCS_LOCK:
                _PROCS[int(task_id)] = proc
            return {"api_socket": api_socket}
        rc = proc.poll()
        if rc is not None and rc != 0:
            try:
                with open(log_path, "rb") as fh:
                    tail = fh.read()[-2000:]
            except OSError:
                tail = b""
            raise RuntimeError(
                f"jailer exited rc={rc}: {tail.decode('utf-8', 'replace')[-500:]}"
            )
        time.sleep(0.2)
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    try:
        with open(log_path, "rb") as fh:
            tail = fh.read()[-2000:]
    except OSError:
        tail = b""
    raise RuntimeError(
        "Firecracker API socket never appeared after jailer spawn; "
        f"log_tail={tail.decode('utf-8', 'replace')[-1000:]}"
    )


def op_destroy_jail(task_id: int, netmod) -> dict:
    """Kill procs, remove the jail tree, tear down networking. Never raises."""
    jail_id = jail_id_for(task_id)
    try:
        with _PROCS_LOCK:
            proc = _PROCS.pop(int(task_id), None)
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        try:
            _kill_pid(_read_fc_pid(chroot_dir_for(task_id)))
        except Exception:
            pass
        _kill_task_procs(jail_id)
    except Exception as exc:
        LOG.warning("destroy-jail procs task=%s non-fatal: %s", task_id, exc)
    try:
        _remove_jail_tree(task_id)
    except Exception as exc:
        LOG.warning("destroy-jail tree task=%s non-fatal: %s", task_id, exc)
    try:
        netmod.destroy_isolation(task_id)
    except Exception as exc:
        LOG.warning("destroy-jail net task=%s non-fatal: %s", task_id, exc)
    return {}


# ---------------------------------------------------------------- server ----
def peer_uid_of(conn: socket.socket) -> int | None:
    """SO_PEERCRED uid of the Unix-socket peer (Linux). None when unavailable."""
    try:
        # SO_PEERCRED == 17 on Linux; struct ucred { pid, uid, gid }.
        raw = conn.getsockopt(socket.SOL_SOCKET, 17, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return int(uid)
    except Exception:
        return None


def handle_request(
    req: object, *, peer_uid: int | None, allowed_uid: int | None,
    netmod=None, egressmod=None, fixhub_ids: tuple[int, int] | None = None,
) -> dict:
    """Validate, authorize, and dispatch one request. Never raises."""
    try:
        ok, error, norm = validate_request(req, peer_uid=peer_uid, allowed_uid=allowed_uid)
        if not ok:
            return {"ok": False, "error": error}
        op = norm["op"]
        if op == "probe":
            return {"ok": True, "data": op_probe(peer_uid)}
        if netmod is None:  # pragma: no cover - daemon misconfigured
            return {"ok": False, "error": "backend modules unavailable"}
        tid = norm["task_id"]
        try:
            if op == "setup":
                if egressmod is not None:
                    # Egress must already run (systemd service); the daemon
                    # never spawns it implicitly — fail closed when down.
                    probed, detail = egressmod._probe_available()
                    if not probed:
                        return {
                            "ok": False,
                            "error": f"host egress unavailable: {detail} "
                            "(start fixhub-egress.service); refusing boot",
                        }
                return {"ok": True, "data": op_setup(tid, netmod)}
            if op == "teardown":
                return {"ok": True, "data": op_teardown(tid, netmod)}
            if op == "prepare-jail":
                return {"ok": True, "data": op_prepare_jail(tid, fixhub_ids)}
            if op == "finalize-jail":
                return {"ok": True, "data": op_finalize_jail(tid)}
            if op == "grant-vsock":
                return {"ok": True, "data": op_grant_vsock(tid, fixhub_ids)}
            if op == "spawn-jailer":
                return {
                    "ok": True,
                    "data": op_spawn_jailer(
                        tid, vcpu=norm["vcpu"], mem_mib=norm["mem_mib"],
                        pids_max=norm["pids_max"],
                        fixhub_ids=fixhub_ids,
                    ),
                }
            if op == "destroy-jail":
                return {"ok": True, "data": op_destroy_jail(tid, netmod)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}
        return {"ok": False, "error": f"unhandled op: {op!r}"}
    except Exception as exc:  # never let a handler kill the daemon
        LOG.exception("handle_request crashed")
        return {"ok": False, "error": f"daemon error: {exc}"[:200]}


def _read_request(conn: socket.socket) -> object:
    conn.settimeout(30)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        if chunk.endswith(b"\n"):
            break
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"unreadable request: {exc}")


def _serve_one(
    conn: socket.socket, *, allowed_uid: int | None, netmod, egressmod,
    fixhub_ids: tuple[int, int] | None,
) -> None:
    try:
        peer = peer_uid_of(conn)
        try:
            req = _read_request(conn)
        except ValueError as exc:
            resp = {"ok": False, "error": str(exc)[:200]}
        else:
            op = req.get("op") if isinstance(req, dict) else "?"
            tid = req.get("task_id") if isinstance(req, dict) else "?"
            resp = handle_request(
                req, peer_uid=peer, allowed_uid=allowed_uid,
                netmod=netmod, egressmod=egressmod, fixhub_ids=fixhub_ids,
            )
            LOG.info(
                "op=%s task=%s peer=%s ok=%s", op, tid, peer, resp.get("ok")
            )
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
    except Exception as exc:
        LOG.warning("connection failed: %s", exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_backend(backend_dir: str):
    """Import canonical backend modules. Raises with a clear error."""
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    try:
        from app.sandbox import egress as egressmod
        from app.sandbox import net as netmod
    except Exception as exc:
        raise RuntimeError(
            f"cannot import backend modules from {backend_dir}: {exc}"
        )
    return netmod, egressmod


def run_server(
    socket_path: str, *, allowed_uid: int | None, backend_dir: str,
    stop: threading.Event | None = None,
) -> None:
    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("Unix sockets unsupported on this platform; refusing start")
    netmod, egressmod = load_backend(backend_dir)
    fixhub_ids = resolve_fixhub_ids()
    if fixhub_ids is None:
        LOG.warning("fixhub user unknown: prepare-jail will refuse (fail closed)")
    # A stale live daemon owns the socket: refuse to shadow it.
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(2)
        probe.connect(socket_path)
        raise RuntimeError(f"another daemon already owns {socket_path}; refusing start")
    except OSError:
        pass
    finally:
        probe.close()
    try:
        if os.path.exists(socket_path):
            os.unlink(socket_path)
    except OSError as exc:
        raise RuntimeError(f"cannot clear stale socket {socket_path}: {exc}")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(socket_path)
        srv.listen(32)
        try:
            if grp is None:
                raise KeyError("grp unavailable")
            gid = grp.getgrnam("fixhub").gr_gid
            os.chown(socket_path, 0, gid)
            os.chmod(socket_path, 0o770)
        except (KeyError, OSError) as exc:
            LOG.warning("socket group setup incomplete (%s); API connect may fail", exc)
            try:
                os.chmod(socket_path, 0o770)
            except OSError:
                pass
        LOG.info("listening on %s (allowed_uid=%s)", socket_path, allowed_uid)
        srv.settimeout(1.0)
        while stop is None or not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(
                target=_serve_one,
                args=(conn,),
                kwargs={
                    "allowed_uid": allowed_uid,
                    "netmod": netmod,
                    "egressmod": egressmod,
                    "fixhub_ids": fixhub_ids,
                },
                daemon=True,
            ).start()
    finally:
        try:
            srv.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fixhub-netd privilege daemon")
    parser.add_argument("--socket", default="/run/fixhub-netd.sock")
    parser.add_argument("--backend-dir", default="")
    parser.add_argument("--allowed-uid", default="")
    parser.add_argument("--allow-nonroot", action="store_true",
                        help="permit running as non-root (testing only; logs loudly)")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s fixhub-netd %(levelname)s %(message)s",
    )
    try:
        if os.geteuid() == 0:  # noqa: F841 (informational)
            pass
    except AttributeError:
        pass
    else:
        if os.geteuid() != 0 and not args.allow_nonroot:
            print(
                "refusing: fixhub-netd must run as root "
                "(use --allow-nonroot only for testing)",
                file=sys.stderr,
            )
            return 2
        if os.geteuid() != 0:
            LOG.warning("running as non-root (--allow-nonroot testing mode)")
    allowed = resolve_allowed_uid(args.allowed_uid)
    if allowed is None:
        print(
            "refusing: cannot resolve served uid "
            "(no fixhub user and no --allowed-uid/FC_NETD_ALLOWED_UID)",
            file=sys.stderr,
        )
        return 2
    # The canonical net code resolves iproute2/nft/conntrack via PATH.
    os.environ["PATH"] = (
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    backend_dir = args.backend_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend"
    )
    backend_dir = os.path.abspath(backend_dir)

    def _stop(*_a):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0))
    try:
        run_server(args.socket, allowed_uid=allowed, backend_dir=backend_dir)
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        print(f"fixhub-netd: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
