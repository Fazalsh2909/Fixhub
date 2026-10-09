"""Phase 5: Firecracker microVM backend via jailer + vsock exec.

Security contract (never relax):
- Provision requires Linux + /dev/kvm + firecracker/jailer binaries + verified
  kernel/rootfs + enforceable net isolation. ANY missing piece raises
  SandboxBlockedError and the task goes BLOCKED — NEVER host fallback.
- Secrets never cross vsock: no API takes tokens/keys/URLs-with-credentials.
  Repo transfer uses guest_agent.make_tokenless_tree() semantics (per-file
  writes with .git/config stripped).
- LLM calls stay on the host (thread-local BYOK). GitHub push/PR stays on the
  host (service._publish_task). The guest holds a tokenless checkout only.
- Each task gets its own jail dir, API socket (0700, per-VM uid), netns/TAP,
  overlay copy, and vsock CID. Shared state across tasks is forbidden.
- destroy() is idempotent and best-effort; provision failure cleans up
  partial resources before raising.

Jail layout (official Firecracker jailer layout — see docs/jailer.md):
  <FC_CHROOT_BASE>/firecracker/task-<id>/root/   <- <chroot_dir>
    firecracker          (binary copy made by the jailer)
    firecracker.pid      (child PID, written by the jailer in ALL modes)
    api.socket           (explicit `--api-sock /api.socket` after `--`)
    vmlinux              (staged by us, hardlink-or-copy)
    overlay.ext4         (per-task writable copy of the immutable base)
The host talks to <chroot_dir>/api.socket. Drive/kernel API paths are
jail-relative (`./vmlinux`, `./overlay.ext4`) because Firecracker runs
chrooted at <chroot_dir>.

Transport: Firecracker REST over the jailed API unix socket (manual HTTP over
AF_UNIX — no new dependencies); guest exec over AF_VSOCK JSON frames
(see guest_agent.VsockClient).

Windows/laptop behavior: every entry raises SandboxBlockedError with a clear
message (no KVM on Windows by design). Real-VM tests gate on this.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat as _stat
import subprocess
import tempfile as _tempfile
import threading
import time

from app.sandbox import egress as _egress
from app.sandbox import guest_agent as _guest
from app.sandbox import images as _images
from app.sandbox import net as _net
from app.sandbox import sandbox as _host

# In-memory VM registry: task_id -> _VM. Guarded by _REG_LOCK.
_REG: dict[int, "_VM"] = {}
_REG_LOCK = threading.Lock()

# Fixed vsock port for the guest exec agent (per-VM CID differs, port shared).
_GUEST_AGENT_PORT = 5000
# Base CID for task VMs (CID 2 is host-reserved; 0/1/max are special).
_CID_BASE = 100
_CID_MOD = 50000
_CID_MAX = (1 << 32) - 1


def _local_host() -> str:
    try:
        import socket as _socket

        return _socket.gethostname()
    except Exception:
        return "unknown-host"


def _owner_stale_s() -> int:
    """Heartbeat age past which an owner is considered stale (seconds).

    Must exceed a healthy worker's longest quiet stretch between guest
    execs; the DB lease check below remains the safety net, so a stale
    heartbeat alone never reaps a live-leased task.
    """
    try:
        return int(_cfg("FC_OWNER_STALE_S", 900) or 900)
    except (TypeError, ValueError):
        return 900


def _pid_start(pid: int) -> str:
    """Owner-process start identity (Linux /proc starttime) against pid reuse."""
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8") as fh:
            parts = fh.read().rsplit(")", 1)[1].split()
            return parts[19]  # starttime (22nd field)
    except (OSError, IndexError, ValueError):
        return ""


def _pid_alive(pid: int | None, pid_start: str = "") -> bool:
    try:
        pid = int(pid)  # type: ignore[assignment]
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0 or pid >= 2**22:
        # Outside any real PID range (Linux pid_max default 2**22, Windows
        # PIDs are small): treat as dead WITHOUT probing the OS. Probing
        # absurd PIDs is both meaningless and, on some hosts, observable.
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError, OverflowError):
        return False
    if pid_start and os.path.exists(f"/proc/{pid}"):
        try:
            return _pid_start(pid) == pid_start
        except Exception:
            return False
    return True


def _owner_file_for(task_id: int) -> str:
    base = str(_cfg("FC_CHROOT_BASE", "/srv/firecracker/jails"))
    return os.path.join(base, "firecracker", jail_id_for(task_id), "owner.json")


def _write_owner(task_id: int, *, worker: str = "") -> dict:
    """Claim a VM for this process (durable, cross-process visible)."""
    try:
        import getpass as _getpass

        user = _getpass.getuser()
    except Exception:
        user = ""
    owner = {
        "task_id": int(task_id),
        "host": _local_host(),
        "worker": (worker or f"pid-{os.getpid()}")[:64],
        "pid": os.getpid(),
        "pid_start": _pid_start(os.getpid()),
        "user": user,
        "heartbeat": time.time(),
    }
    try:
        path = _owner_file_for(task_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(owner, fh)
        os.replace(tmp, path)
    except OSError:
        pass
    return owner


def _read_owner(task_id: int) -> dict | None:
    try:
        with open(_owner_file_for(task_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and int(data.get("task_id", -1)) == int(task_id):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _touch_owner(task_id: int) -> None:
    """Refresh this VM's heartbeat (owner process only, best-effort)."""
    try:
        owner = _read_owner(task_id)
        if owner is None:
            return
        if owner.get("host") != _local_host() or int(owner.get("pid", -1)) != os.getpid():
            return  # not ours: never rewrite a foreign owner's heartbeat
        owner["heartbeat"] = time.time()
        path = _owner_file_for(task_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(owner, fh)
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        pass


def _db_lease_live(task_id: int) -> bool | None:
    """True when the task row is RUNNING with an unexpired lease.

    None when the database cannot answer (unknown — callers must then
    require stronger local staleness before reaping). Never raises.
    """
    try:
        from app.db.database import SessionLocal, utcnow
        from app.db.models import Task as _Task

        db = SessionLocal()
        try:
            row = db.query(_Task).filter(_Task.id == int(task_id)).first()
            if row is None or row.status != "RUNNING":
                return False
            lease = getattr(row, "lease_expires_at", None)
            if lease is None:
                return False
            now = utcnow()
            try:
                return bool(lease > now)
            except TypeError:
                return False
        finally:
            db.close()
    except Exception:
        return None


class _VM:
    def __init__(
        self,
        *,
        task_id: int,
        jail_id: str,
        chroot_dir: str,
        api_socket: str,
        cid: int,
        overlay: str,
        firecracker_pid: int | None = None,
        jailer_proc: "subprocess.Popen | None" = None,
    ):
        self.task_id = int(task_id)
        self.jail_id = jail_id
        self.chroot_dir = chroot_dir
        # Legacy alias: the jail id dir (parent of the chroot root).
        self.jail_dir = os.path.dirname(chroot_dir)
        self.api_socket = api_socket
        self.cid = int(cid)
        self.overlay = overlay
        self.firecracker_pid = firecracker_pid
        self.jailer_proc = jailer_proc
        self.ready = False
        self.created_mono = time.monotonic()


def _cfg(name: str, default=""):
    try:
        from app.config import settings as _settings

        return getattr(_settings, name, default)
    except Exception:
        return default


def _cid_for(task_id: int) -> int:
    """Single source of truth for the guest vsock CID (host + config agree).

    Deterministic per task so reclaim-after-crash redials the same CID.
    Callers must pass this CID to BOTH the /vsock API object and every
    host-side VsockClient — a mismatch means the agent never answers.
    """
    cid = _CID_BASE + (int(task_id) % _CID_MOD)
    if cid >= _CID_MAX:
        cid = _CID_BASE + (cid % _CID_MOD)
    return int(cid)


def jail_id_for(task_id: int) -> str:
    return f"task-{int(task_id)}"


def exec_name() -> str:
    """Basename of the firecracker binary (jailer copies it under this name)."""
    return os.path.basename(_images.firecracker_binary() or "firecracker") or "firecracker"


def chroot_dir_for(task_id: int) -> str:
    """Official jail root: <base>/firecracker/task-<id>/root."""
    base = str(_cfg("FC_CHROOT_BASE", "/srv/firecracker/jails"))
    return os.path.join(base, "firecracker", jail_id_for(task_id), "root")


def api_socket_for(chroot_dir: str) -> str:
    return os.path.join(chroot_dir, "api.socket")


def pid_file_for(chroot_dir: str) -> str:
    # Official: <exec_file_name>.pid in the jail root, all modes.
    return os.path.join(chroot_dir, f"{exec_name()}.pid")


def _api_put(api_socket: str, path: str, payload: dict, timeout_s: int = 10) -> None:
    """PUT JSON to the Firecracker API unix socket. Raises on any failure."""
    body = json.dumps(payload).encode("utf-8")
    req = (
        f"PUT {path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("utf-8") + body
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout_s)
        sock.connect(api_socket)
        sock.sendall(req)
        resp = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                raise RuntimeError(f"FC API timeout on {path}")
            if not chunk:
                break
            resp += chunk
            if len(resp) > 1024 * 1024:
                break
    finally:
        try:
            sock.close()
        except Exception:
            pass
    try:
        status_line = resp.split(b"\r\n", 1)[0].decode("latin1")
        code = int(status_line.split(" ")[1])
    except Exception:
        raise RuntimeError(f"FC API unreadable response on {path}: {resp[:200]!r}")
    if code not in (200, 201, 204):
        raise RuntimeError(f"FC API {path} -> HTTP {code}: {resp[:500]!r}")


def _api_put_action(api_socket: str, action: str) -> None:
    _api_put(api_socket, "/actions", {"action_type": action})


def _read_fc_pid(chroot_dir: str) -> int | None:
    """Read the jailer-written child PID (official firecracker.pid first)."""
    candidates = [pid_file_for(chroot_dir)]
    # Legacy fallbacks (pre-layout-fix RCs wrote beside the jail dir).
    parent = os.path.dirname(chroot_dir)
    candidates += [
        os.path.join(parent, "firecracker.pid"),
        os.path.join(parent, "jailer.pid"),
    ]
    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return int(fh.read().strip().split()[0])
        except (OSError, ValueError, IndexError):
            continue
    return None


def _kill_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
            )
        else:
            import signal as _signal

            os.kill(pid, _signal.SIGKILL)
    except Exception:
        pass


def _kill_task_procs(jail_id: str) -> None:
    """Kill leftover jailer/firecracker processes for one jail id. Best-effort."""
    try:
        if os.name != "nt":
            subprocess.run(
                ["pkill", "-f", f"--id {jail_id}( |$)"],
                capture_output=True,
                timeout=10,
            )
    except Exception:
        pass


def cgroup_version() -> str:
    """Detect the host cgroup version ('2', '1', or '' when undetectable)."""
    try:
        if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
            return "2"
        if os.path.isdir("/sys/fs/cgroup/cpu") or os.path.isdir("/sys/fs/cgroup/memory"):
            return "1"
    except Exception:
        pass
    return ""


def jailer_cgroup_args(*, vcpu: int, mem_mib: int, pids_max: int) -> tuple[str, list[str]]:
    """Build jailer --cgroup-version/--cgroup args. Raises when undetectable.

    v2 (Ubuntu 24.04 default): cpu.max ("<quota> <period>"), memory.max
    (bytes), pids.max. v1: cpu.cfs_quota_us/cpu.cfs_period_us (REAL quota —
    cpu.shares is only a scheduling weight and must never be claimed as a
    hard limit), memory.limit_in_bytes, pids.max. Unknown layout fails
    closed (never boot unbounded).
    """
    version = cgroup_version()
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


def vm_max_runtime_s() -> int:
    try:
        return int(_cfg("FC_VM_MAX_RUNTIME_S", 1500) or 1500)
    except (TypeError, ValueError):
        return 1500


def provision(task_id: int, workspace: str, *, owner_worker: str = "") -> _VM:
    """Boot (or reuse) the task's microVM and sync the tokenless repo tree.

    Raises SandboxBlockedError on ANY failure (missing host support, isolation
    failure, boot failure, transfer failure). Never falls back to host exec.

    Ownership: the calling (worker) process durably claims the VM via an
    owner file (host + worker + pid + heartbeat) BEFORE spawning the jailer,
    so a foreign process's reaper can distinguish "mine and live" from
    "crashed and reclaimable" without trusting its own memory.
    """
    task_id = int(task_id)
    with _REG_LOCK:
        existing = _REG.get(task_id)
        if existing is not None and existing.ready:
            _touch_owner(task_id)
            return existing
        # CID collision guard: two live VMs must never share a CID.
        cid = _cid_for(task_id)
        for other_id, other in _REG.items():
            if other_id != task_id and other.ready and other.cid == cid:
                raise _host.SandboxBlockedError(
                    f"vsock CID collision: task {task_id} and task {other_id} "
                    f"both map to CID {cid}; refusing boot"
                )
    # 1. Host prerequisites (fail closed).
    if os.name == "nt":
        raise _host.SandboxBlockedError(
            "firecracker unavailable: Windows has no KVM "
            "(use the dedicated Linux/KVM host; see docs/FIRECRACKER_HOST.md)"
        )
    try:
        _images.require_artifacts()
    except Exception as exc:
        raise _host.SandboxBlockedError(f"firecracker unavailable: {exc}")
    # 2. Network isolation BEFORE boot (fail closed).
    try:
        netinfo = _net.ensure_isolation(task_id)
    except Exception as exc:
        raise _host.SandboxBlockedError(f"network isolation failed: {exc}")
    # 3. Jail dir + overlay + staged boot files (official layout).
    jail_id = jail_id_for(task_id)
    chroot = chroot_dir_for(task_id)
    api_socket = api_socket_for(chroot)
    overlay = os.path.join(chroot, "overlay.ext4")
    uid = 100000 + (int(task_id) % 50000)
    gid = uid
    jailer_proc: "subprocess.Popen | None" = None
    try:
        os.makedirs(chroot, exist_ok=True)
        os.chmod(chroot, 0o700)
        _write_owner(task_id, worker=owner_worker)
        _prepare_overlay(overlay)
        _stage_boot_files(chroot, overlay, uid=uid, gid=gid)
        jailer_proc = _spawn_jailer(
            task_id=task_id, jail_id=jail_id, chroot_dir=chroot,
            api_socket=api_socket, netns=netinfo["netns"],
            uid=uid, gid=gid,
        )
        _configure_and_boot(
            api_socket=api_socket,
            overlay_name=os.path.basename(overlay),
            cid=cid,
            tap=netinfo["tap"],
        )
        vm = _VM(
            task_id=task_id,
            jail_id=jail_id,
            chroot_dir=chroot,
            api_socket=api_socket,
            cid=cid,
            overlay=overlay,
            firecracker_pid=_read_fc_pid(chroot),
            jailer_proc=jailer_proc,
        )
        _wait_guest_agent(vm)
        vm.ready = True
        with _REG_LOCK:
            _REG[task_id] = vm
        # 4. Tokenless repo sync (host -> guest). Failure destroys the VM.
        try:
            sync_repo_to_guest(task_id=task_id, workspace=workspace)
        except Exception as exc:
            destroy(task_id)
            raise _host.SandboxBlockedError(f"repo transfer to guest failed: {exc}")
        return vm
    except _host.SandboxBlockedError:
        try:
            if jailer_proc is not None and jailer_proc.poll() is None:
                jailer_proc.kill()
        except Exception:
            pass
        _cleanup_partial(task_id)
        raise
    except Exception as exc:
        try:
            if jailer_proc is not None and jailer_proc.poll() is None:
                jailer_proc.kill()
        except Exception:
            pass
        _cleanup_partial(task_id)
        raise _host.SandboxBlockedError(f"firecracker provision failed: {exc}")


def _prepare_overlay(overlay: str) -> None:
    """Create the disposable writable overlay from the immutable base rootfs.

    FC_OVERLAY_MB is the disk cap: the base image must fit inside it
    (fail-closed otherwise). The overlay is an exact copy, so the VM's
    writable disk can never exceed the cap. Verified by size after copy.
    """
    base = _images.rootfs_image()
    max_mb = int(_cfg("FC_OVERLAY_MB", 5120) or 5120)
    if os.path.exists(overlay):
        return
    try:
        size = os.path.getsize(base)
    except OSError as exc:
        raise RuntimeError(f"base rootfs unreadable: {exc}")
    if size <= 0:
        raise RuntimeError("base rootfs is empty; refusing boot")
    if size > max_mb * 1024 * 1024:
        raise RuntimeError(
            f"base rootfs ({size} bytes) exceeds overlay cap "
            f"FC_OVERLAY_MB={max_mb}; refusing boot"
        )
    shutil.copyfile(base, overlay)
    try:
        if os.path.getsize(overlay) != size:
            raise RuntimeError("overlay copy size mismatch")
    except OSError as exc:
        raise RuntimeError(f"overlay copy unverifiable: {exc}")
    os.chmod(overlay, 0o600)


def _stage_boot_files(chroot: str, overlay: str, *, uid: int, gid: int) -> None:
    """Hardlink (or copy) the kernel into the jail root (jailer requirement).

    The overlay already lives at <chroot>/overlay.ext4. Both files are
    chowned to the per-VM uid:gid because the unprivileged Firecracker
    process needs read (kernel) and read+write (RW drive) access.
    """
    kernel = _images.kernel_image()
    dest = os.path.join(chroot, os.path.basename(kernel) or "vmlinux")
    if not os.path.exists(dest):
        try:
            os.link(kernel, dest)
        except OSError:
            shutil.copyfile(kernel, dest)
    try:
        os.chown(dest, uid, gid)
        os.chown(overlay, uid, gid)
    except OSError as exc:
        raise RuntimeError(f"could not chown staged boot files to {uid}:{gid}: {exc}")


def _spawn_jailer(
    *, task_id: int, jail_id: str, chroot_dir: str, api_socket: str,
    netns: str, uid: int, gid: int,
) -> "subprocess.Popen":
    """Launch the jailer (foreground, PID-tracked) and wait for its socket.

    Foreground (no --daemonize) so the worker owns the jailer PID and can
    kill the whole group on destroy; the jailer still writes firecracker.pid
    in the jail root. Extra Firecracker args after `--`: explicit
    `--api-sock /api.socket` so the host talks to the socket the jail
    actually creates (<chroot>/api.socket).
    """
    jailer = _images.jailer_binary()
    fc = _images.firecracker_binary()
    chroot_base = _images.chroot_base()
    vcpu = int(_cfg("FC_GUEST_VCPU", 2) or 2)
    mem_mib = int(_cfg("FC_GUEST_MEM_MIB", 1024) or 1024)
    try:
        pids_max = int(_cfg("FC_PIDS_MAX", 256) or 256)
    except (TypeError, ValueError):
        pids_max = 256
    cgroup_version, cgroup_args = jailer_cgroup_args(vcpu=vcpu, mem_mib=mem_mib, pids_max=pids_max)
    overlay_bytes = 0
    try:
        overlay_bytes = os.path.getsize(os.path.join(chroot_dir, "overlay.ext4"))
    except OSError:
        pass
    fsize_cap = overlay_bytes + mem_mib * 1024 * 1024 + 64 * 1024 * 1024
    netns_path = f"/var/run/netns/{netns}"
    id_dir = os.path.dirname(chroot_dir)
    log_path = os.path.join(id_dir, "jailer.log")
    cmd = [
        jailer,
        "--id", jail_id,
        "--exec-file", fc,
        "--uid", str(uid),
        "--gid", str(gid),
        "--chroot-base-dir", chroot_base,
        "--cgroup-version", cgroup_version,
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
        # Parent copy closed: the child keeps its own dup. Avoids fd leaks
        # across many provisions in a long-lived worker.
        try:
            log_fh.close()
        except Exception:
            pass
    # Wait for the API socket the jail actually created.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if os.path.exists(api_socket):
            return proc
        if proc.poll() is not None:
            try:
                with open(log_path, "rb") as fh:
                    tail = fh.read()[-2000:]
            except OSError:
                tail = b""
            raise RuntimeError(
                f"jailer exited rc={proc.returncode}: {tail.decode('utf-8', 'replace')[-500:]}"
            )
        time.sleep(0.2)
    try:
        proc.kill()
    except Exception:
        pass
    raise RuntimeError("firecracker API socket never appeared after jailer spawn")


def _configure_and_boot(
    *, api_socket: str, overlay_name: str, cid: int, tap: str
) -> None:
    vcpu = int(_cfg("FC_GUEST_VCPU", 2) or 2)
    mem = int(_cfg("FC_GUEST_MEM_MIB", 1024) or 1024)
    kernel_name = os.path.basename(_images.kernel_image()) or "vmlinux"
    boot_args = (
        "console=ttyS0 reboot=k panic=1 pci=off ipv6.disable=1 "
        f"ip={_net.GUEST_VM_IP}::{_net.GUEST_HOST_IP}:"
        f"255.255.255.252::eth0:off"
    )
    _api_put(api_socket, "/boot-source", {
        "kernel_image_path": f"./{kernel_name}",
        "boot_args": boot_args,
    })
    _api_put(api_socket, "/drives/rootfs", {
        "drive_id": "rootfs",
        "path_on_host": f"./{overlay_name}",
        "is_root_device": True,
        "is_read_only": False,
        "rate_limiter": {
            "bandwidth": {"size": 50 * 1024 * 1024, "refill_time": 1000},
            "ops": {"size": 1000, "refill_time": 1000},
        },
    })
    _api_put(api_socket, "/machine-config", {
        "vcpu_count": vcpu,
        "mem_size_mib": mem,
        "smt": False,
    })
    # The guest CID MUST equal the task's _cid_for() value: it is the same
    # identity the host VsockClient dials. A mismatch = agent never answers.
    # uds_path is absolute inside the jail (Firecracker is chrooted at the
    # jail root, so /v.sock == <chroot>/v.sock on the host).
    _api_put(api_socket, "/vsock", {
        "guest_cid": cid,
        "uds_path": "/v.sock",
    })
    _api_put(api_socket, "/network-interfaces/eth0", {
        "iface_id": "eth0",
        "guest_mac": _mac_for(cid),
        "host_dev_name": tap,
        "rx_rate_limiter": {
            "bandwidth": {"size": 10 * 1024 * 1024, "refill_time": 1000},
            "ops": {"size": 1000, "refill_time": 1000},
        },
        "tx_rate_limiter": {
            "bandwidth": {"size": 10 * 1024 * 1024, "refill_time": 1000},
            "ops": {"size": 1000, "refill_time": 1000},
        },
    })
    _api_put_action(api_socket, "InstanceStart")


def _mac_for(cid: int) -> str:
    b = cid % 256
    c = (cid // 256) % 256
    return f"AA:FC:00:00:{b:02X}:{c:02X}"


def _wait_guest_agent(vm: _VM) -> None:
    timeout = int(_cfg("FC_BOOT_TIMEOUT_S", 30) or 30)
    client = _guest.VsockClient(cid=vm.cid, port=_GUEST_AGENT_PORT)
    deadline = time.monotonic() + max(5, timeout)
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = client.call(
                _guest.build_request(op="exec", argv=["echo", "ok"], timeout_s=5),
                timeout_s=8,
            )
            if str(resp.get("stdout", "")).strip() == "ok":
                return
        except Exception as exc:
            last_err = exc
        time.sleep(1.0)
    raise RuntimeError(f"guest agent never answered: {last_err}")


def _is_symlink(path: str) -> bool:
    """lstat-based symlink test. Missing/unreadable paths report False."""
    try:
        return os.path.islink(path)
    except OSError:
        return False


def _collect_transfer_files(workspace: str) -> list[tuple[str, str]]:
    """Enumerate regular files for host->guest transfer (pure enumeration).

    Symlinks are NEVER followed: symlinked dirs are pruned from the walk and
    symlinked/non-regular files are skipped, so external file contents can
    never enter the guest under an innocent-looking repo filename.
    Sensitive names (.env, keys, *secret*/*token*) are excluded here too
    (defense in depth; the guest re-validates on write).
    Returns [(repo_relative_posix_path, host_path)].
    """
    from app.agent.paths import is_sensitive as _is_sensitive

    collected: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(workspace, followlinks=False):
        kept: list[str] = []
        for d in dirs:
            if d in ("__pycache__", ".git"):
                continue
            if _is_symlink(os.path.join(root, d)):
                continue
            kept.append(d)
        dirs[:] = kept
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, workspace).replace(os.sep, "/")
            if "__pycache__" in rel:
                continue
            try:
                if _is_symlink(full):
                    continue
                if not _stat.S_ISREG(os.lstat(full).st_mode):
                    continue
            except OSError:
                continue
            if _is_sensitive(rel):
                continue
            collected.append((rel, full))
    return collected


def _read_host_file_nofollow(full: str) -> str:
    """Read a regular file without following symlinks (TOCTOU-resistant).

    O_NOFOLLOW makes the open itself fail if `full` is a symlink, so a link
    swapped in between enumeration and read cannot leak target bytes.
    Raises OSError/UnicodeDecodeError on failure (caller skips the file).
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(full, flags)
    try:
        with os.fdopen(fd, "r", encoding="utf-8", errors="strict") as fh:
            return fh.read()
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def sync_repo_to_guest(*, task_id: int, workspace: str) -> None:
    """Stream the host workspace into the guest with credentials stripped.

    Per-file `write` ops (bounded, skips __pycache__ + tokenless .git/config).
    Symlinks are never followed (see _collect_transfer_files); sensitive
    names never cross. Raises on first transport failure (caller destroys
    the VM).
    """
    with _REG_LOCK:
        vm = _REG.get(int(task_id))
    if vm is None or not vm.ready:
        raise RuntimeError("no live VM for repo sync")
    if not workspace or not os.path.isdir(workspace):
        raise RuntimeError("host workspace missing for repo sync")
    client = _guest.VsockClient(cid=vm.cid, port=_GUEST_AGENT_PORT)
    _touch_owner(task_id)
    for rel, full in _collect_transfer_files(workspace):
        try:
            content = _read_host_file_nofollow(full)
        except (OSError, UnicodeDecodeError):
            continue  # vanished, relinked, binary: never ship target bytes
        if len(content) > 200_000:
            continue
        req = _guest.build_request(op="write", cwd=".", timeout_s=30)
        req["path"] = rel
        req["content"] = content
        resp = client.call(req, timeout_s=35)
        if not resp.get("ok", True) and resp.get("exit_code", 1) != 0:
            raise RuntimeError(f"guest write failed for {rel}")
    # NOTE: .git objects/refs are intentionally NOT shipped (host-side git
    # architecture: status/diff run on the trusted host after reconciling
    # guest changes). Only a tokenless .git/config is provided when present
    # and only when it is a real file (never a symlink).
    git_config = os.path.join(workspace, ".git", "config")
    if not _is_symlink(git_config) and os.path.isfile(git_config):
        try:
            cleaned = _guest.strip_token_from_git_config(
                _read_host_file_nofollow(git_config)
            )
            req = _guest.build_request(op="write", cwd=".", timeout_s=15)
            req["path"] = ".git/config"
            req["content"] = cleaned
            client.call(req, timeout_s=20)
        except (OSError, UnicodeDecodeError):
            pass


def exec_in_guest(
    task_id: int,
    *,
    command: str,
    cwd: str = ".",
    timeout_s: int | None = None,
) -> dict:
    """Execute one shell command in the task VM. Raises SandboxBlockedError on failure."""
    from app.config import settings as _settings

    with _REG_LOCK:
        vm = _REG.get(int(task_id))
    if vm is None or not vm.ready:
        raise _host.SandboxBlockedError("no live microVM for this task")
    # Policy + cwd checks mirror the host backend (defense in depth; the
    # guest re-validates too). Import lazily to avoid cycles.
    try:
        _host._check_allowed(command)
    except _host.SandboxBlockedError:
        raise
    try:
        from app.agent.paths import resolve as _resolve

        ws = os.path.abspath(_settings.WORKSPACE_ROOT)
        _resolve(os.path.join(ws, f"task-{task_id}"), (cwd or ".").strip() or ".")
    except ValueError as exc:
        raise _host.SandboxBlockedError(str(exc))
    timeout = timeout_s or _settings.COMMAND_TIMEOUT_S
    client = _guest.VsockClient(cid=vm.cid, port=_GUEST_AGENT_PORT)
    _touch_owner(vm.task_id)
    started = time.monotonic()
    try:
        resp = client.call(
            _guest.build_request(
                op="exec_shell", cwd=cwd or ".", command=command,
                timeout_s=timeout,
                cap_bytes=_settings.TOOL_OUTPUT_MAX_BYTES,
            ),
            timeout_s=timeout + 10,
        )
    except Exception as exc:
        raise _host.SandboxBlockedError(f"guest exec transport failed: {exc}")
    duration_ms = int((time.monotonic() - started) * 1000)
    # Redact host-registered secrets from guest output (defense in depth;
    # the guest should never have seen them in the first place).
    stdout = _host.redact(str(resp.get("stdout", "")))
    stderr = _host.redact(str(resp.get("stderr", "")))
    exit_code = resp.get("exit_code")
    timed_out = bool(resp.get("timed_out", exit_code is None))
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": bool(resp.get("truncated", False)),
        "duration_ms": resp.get("duration_ms", duration_ms),
        "timed_out": timed_out,
        "cwd": cwd or ".",
    }


def _safe_host_dest(workspace: str, rel: str) -> str:
    """Resolve a guest-supplied path to a host destination, symlink-aware.

    Rejects (ValueError): empty/absolute/drive/NUL paths, `.`/`..` components,
    and any destination whose existing parents form a symlink chain escaping
    the workspace — including a symlinked final component. Missing parents
    are fine (created by the atomic writer, then re-validated). Lexical
    `resolve()` alone cannot see symlinks, so every existing component is
    lstat-checked here.
    """
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        raise ValueError("path is required and must be non-empty")
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or rel.startswith("~") or len(rel) > 2 and rel[1] == ":":
        raise ValueError(f"path escapes workspace: {rel[:200]}")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"path escapes workspace: {rel[:200]}")
    base = os.path.abspath(workspace)
    cur = base
    for p in parts[:-1]:
        cur = os.path.join(cur, p)
        try:
            st = os.lstat(cur)
        except OSError:
            continue  # missing: will be created, then re-validated
        if _stat.S_ISLNK(st.st_mode):
            raise ValueError(f"symlinked parent in workspace path: {rel[:200]}")
        if not _stat.S_ISDIR(st.st_mode):
            raise ValueError(f"non-directory parent in workspace path: {rel[:200]}")
    dest = os.path.join(cur, parts[-1])
    try:
        if _stat.S_ISLNK(os.lstat(dest).st_mode):
            raise ValueError(f"destination is a symlink: {rel[:200]}")
    except OSError:
        pass  # missing destination: fine
    if os.path.abspath(dest) != base and not os.path.abspath(dest).startswith(
        base + os.sep
    ):
        raise ValueError(f"path escapes workspace: {rel[:200]}")
    return dest


def _atomic_write_text(dest: str, content: str) -> None:
    """Write text to `dest` atomically via temp-file + rename.

    Parents are re-validated symlink-free AFTER creation (a concurrent
    symlink plant between check and write is rejected instead of followed).
    Raises OSError/ValueError on failure.
    """
    import os as _os

    parent = _os.path.dirname(dest)
    if parent:
        _os.makedirs(parent, exist_ok=True)
        # Re-walk the created parents: any symlink invalidates the write.
        # Walk up until the filesystem root.
        node = parent
        seen: set[str] = set()
        while node and node not in seen:
            seen.add(node)
            try:
                if _stat.S_ISLNK(_os.lstat(node).st_mode):
                    raise ValueError(f"symlinked parent appeared: {node[:200]}")
            except OSError:
                pass
            parent_of = _os.path.dirname(node)
            if parent_of == node:
                break
            node = parent_of
    fd, tmp = _tempfile.mkstemp(
        dir=parent or ".", prefix=".fixhub-", suffix=".tmp"
    )
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        _os.replace(tmp, dest)
    except Exception:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise


def write_host_result(workspace: str, rel: str, content: str) -> str:
    """Store one guest file into the host workspace (symlink-safe).

    Returns the host destination path. Raises ValueError (unsafe path) or
    OSError (I/O failure); callers treat refusal as skip, never as fallback.
    """
    from app.agent.paths import is_sensitive as _is_sensitive

    if _is_sensitive(str(rel).split(":")[0]):
        raise ValueError("sensitive file is blocked")
    dest = _safe_host_dest(workspace, rel)
    _atomic_write_text(dest, content)
    return dest


def sync_guest_to_host(*, task_id: int, workspace: str) -> dict:
    """Copy guest working-tree changes back to the host workspace for publish.

    Runs on the TRUSTED host after the agent loop and gates complete. Reads
    guest files over vsock and writes them to the host workspace through the
    symlink-aware jail (symlinked parents/destinations rejected, sensitive
    names blocked, atomic writes). `.git` internals are NOT copied (host git
    state stays authoritative; publish uses host git).
    Returns {files, bytes}. Raises SandboxBlockedError on transport failure
    (caller fails the task; never publishes stale state silently).
    """
    import os as _os

    with _REG_LOCK:
        vm = _REG.get(int(task_id))
    if vm is None or not vm.ready:
        raise _host.SandboxBlockedError("no live microVM for result sync")
    if not workspace or not _os.path.isdir(workspace):
        raise _host.SandboxBlockedError("host workspace missing for result sync")

    client = _guest.VsockClient(cid=vm.cid, port=_GUEST_AGENT_PORT)
    # 1. List guest tree (guest agent returns repo-relative paths).
    try:
        resp = client.call(
            {**_guest.build_request(op="list", cwd=".", timeout_s=15), "path": ".", "recursive": True},
            timeout_s=20,
        )
    except Exception as exc:
        raise _host.SandboxBlockedError(f"guest result listing failed: {exc}")
    entries = resp.get("entries", []) or []
    files = 0
    total_bytes = 0
    for rel in entries:
        rel = str(rel).replace("\\", "/")
        if not rel or rel.startswith(".git/") or rel == ".git":
            continue
        if "__pycache__" in rel:
            continue
        # 2. Read from guest, write to host (refusals skip the file).
        try:
            rreq = _guest.build_request(op="read", cwd=".", timeout_s=15)
            rreq["path"] = rel
            rresp = client.call(rreq, timeout_s=20)
        except Exception:
            continue
        if rresp.get("error"):
            continue
        try:
            write_host_result(workspace, rel, str(rresp.get("content", "")))
        except (ValueError, OSError):
            continue
        files += 1
        total_bytes += len(str(rresp.get("content", "")))
        if total_bytes > 10 * 1024 * 1024 or files > 2000:
            break
    return {"files": files, "bytes": total_bytes}


def _remove_jail_tree(task_id: int, jail_id: str) -> None:
    """Remove the jail tree (new layout + legacy pre-fix layout). Best-effort."""
    base = str(_cfg("FC_CHROOT_BASE", "/srv/firecracker/jails"))
    for candidate in (
        os.path.join(base, "firecracker", jail_id),
        os.path.join(base, jail_id),  # legacy layout (pre-Gate-0-fix RCs)
    ):
        try:
            if os.path.isdir(candidate):
                shutil.rmtree(candidate, ignore_errors=True)
        except Exception:
            pass


def destroy(task_id: int) -> None:
    """Halt the VM, kill its processes, remove jail/netns. Never raises."""
    task_id = int(task_id)
    with _REG_LOCK:
        vm = _REG.pop(task_id, None)
    try:
        if vm is not None:
            try:
                _api_put_action(vm.api_socket, "SendCtrlAltDel")
            except Exception:
                pass
            time.sleep(0.5)
            pid = vm.firecracker_pid or _read_fc_pid(vm.chroot_dir)
            _kill_pid(pid)
            try:
                proc = vm.jailer_proc
                if proc is not None and proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            # Extra sweep: any jailer/firecracker process for this jail id.
            _kill_task_procs(vm.jail_id)
        else:
            # Orphan path (worker crashed before registry): best-effort by dir.
            try:
                _kill_pid(_read_fc_pid(chroot_dir_for(task_id)))
            except Exception:
                pass
            _kill_task_procs(jail_id_for(task_id))
    finally:
        try:
            _remove_jail_tree(task_id, jail_id_for(task_id))
        except Exception:
            pass
        try:
            _net.destroy_isolation(task_id)
        except Exception:
            pass


def _owner_reclaimable(owner: dict | None, *, stale_s: int) -> bool:
    """Decide whether an on-disk VM may be reclaimed by THIS process.

    Never trusts the local `_REG` (another worker's live VM is absent from
    it by design). Reclaim requires ALL of:
    - an owner record exists and names THIS host (foreign hosts are never
      touched; only the per-host manager reaps its own VMs);
    - the owner process is dead (pid liveness + start-identity against
      pid reuse; unknown platform counts as dead only with a very stale
      heartbeat, see below);
    - the heartbeat is older than `stale_s`;
    - the task row is NOT RUNNING with a live lease (a live lease always
      wins; an unreachable database counts as unknown and then requires
      both a dead pid AND a heartbeat older than the VM runtime cap).
    """
    if not owner:
        return False
    try:
        if str(owner.get("host", "")) != _local_host():
            return False
        pid = int(owner.get("pid", 0) or 0)
        heartbeat = float(owner.get("heartbeat", 0) or 0)
    except (TypeError, ValueError):
        return False
    now = time.time()
    heartbeat_age = now - heartbeat if heartbeat > 0 else float("inf")
    if heartbeat_age < 0:
        return False  # clock skew: never reap
    alive = _pid_alive(pid, str(owner.get("pid_start", "") or ""))
    lease = _db_lease_live(int(owner.get("task_id", -1)))
    if lease is True:
        return False  # live lease always protects the VM
    if lease is False:
        return (not alive) and heartbeat_age >= stale_s
    # Database unknown: conservatively require dead pid AND a heartbeat
    # older than the VM runtime cap (not just the stale threshold).
    try:
        cap = vm_max_runtime_s()
    except Exception:
        cap = 1500
    return (not alive) and heartbeat_age >= max(stale_s, cap)


def destroy_orphans(*, max_runtime_s: int | None = None) -> int:
    """Reclaim crashed/overstayed VMs. Returns the number cleaned.

    Multiprocess-safe (unlike a pure in-memory sweep):
    1. Overstays: live `_REG` VMs older than FC_VM_MAX_RUNTIME_S — these
       entries are owned by THIS process by construction, so destroy()
       is safe for them.
    2. Crash orphans: jail dirs on disk whose durable owner record proves
       the owner is dead AND stale (see _owner_reclaimable). A live VM
       owned by another worker/host is never touched merely because it
       is absent from this process's registry.
    Never raises. Call ONLY from the sandbox host's manager/worker process
    (same host as the VMs), never from a foreign host or the generic API
    tier — foreign-host records are skipped by construction.
    """
    cleaned = 0
    try:
        limit = int(max_runtime_s) if max_runtime_s is not None else vm_max_runtime_s()
    except (TypeError, ValueError):
        limit = 1500
    try:
        stale_s = int(max_runtime_s) if max_runtime_s is not None else _owner_stale_s()
    except (TypeError, ValueError):
        stale_s = 900
    now = time.monotonic()
    # Mode 1: reap overstayed live VMs owned by THIS process first.
    overstayed: list[int] = []
    with _REG_LOCK:
        for tid, vm in list(_REG.items()):
            try:
                if now - vm.created_mono > limit:
                    overstayed.append(int(tid))
            except Exception:
                continue
    for tid in overstayed:
        try:
            destroy(tid)
            cleaned += 1
        except Exception:
            continue
    # Mode 2: on-disk jail dirs with a provably dead owner (this host only).
    base = str(_cfg("FC_CHROOT_BASE", "/srv/firecracker/jails") or "")
    seen: set[int] = set()
    for parent in (os.path.join(base, "firecracker"), base):
        try:
            names = os.listdir(parent)
        except OSError:
            continue
        for name in names:
            if not name.startswith("task-"):
                continue
            suffix = name.split("task-", 1)[1]
            if not suffix.isdigit():
                continue
            tid = int(suffix)
            if tid in seen:
                continue
            seen.add(tid)
            with _REG_LOCK:
                if tid in _REG:
                    continue  # live in THIS process: not an orphan
            try:
                if not _owner_reclaimable(_read_owner(tid), stale_s=stale_s):
                    continue
                # Kill anything still running for this jail, then remove it.
                try:
                    _kill_pid(_read_fc_pid(chroot_dir_for(tid)))
                except Exception:
                    pass
                _kill_task_procs(jail_id_for(tid))
                _remove_jail_tree(tid, jail_id_for(tid))
                try:
                    _net.destroy_isolation(tid)
                except Exception:
                    pass
                cleaned += 1
            except Exception:
                continue
    return cleaned


def _cleanup_partial(task_id: int) -> None:
    """Remove a half-provisioned jail + its network. Never raises."""
    try:
        _kill_task_procs(jail_id_for(task_id))
    except Exception:
        pass
    try:
        _remove_jail_tree(task_id, jail_id_for(task_id))
    except Exception:
        pass
    try:
        _net.destroy_isolation(task_id)
    except Exception:
        pass


def _task_id_from_workspace(workspace: str) -> int | None:
    try:
        base = os.path.basename(os.path.abspath(workspace))
        if base.startswith("task-"):
            return int(base.split("task-", 1)[1])
    except (ValueError, IndexError):
        pass
    return None


def _run_host_git(workspace: str, *args: str, cap: int = 8000) -> str:
    """Run host git without a shell (Windows-safe) and truncate in Python."""
    import subprocess as _sp

    try:
        proc = _sp.run(
            ["git", *args], cwd=workspace, capture_output=True, text=True,
            timeout=30,
        )
    except (OSError, _sp.TimeoutExpired) as exc:
        return f"ERROR: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > cap:
        out = out[:cap] + f"\n...[truncated {len(out) - cap} bytes]..."
    return f"exit_code: {proc.returncode}\n{out}"


def guest_status_and_diff(workspace: str, *, what: str) -> str:
    """Host-side git status/diff reflecting the guest's current edits.

    Architecture (P1-4): `.git` objects/refs NEVER enter the guest, so git
    cannot run there. Instead the guest working tree is reconciled into
    the trusted host workspace (symlink-safe, sensitive names blocked;
    `.git` untouched) and host git answers from the authoritative repo.
    Push-capable credentials live only on the host and are never sent to
    the guest. Raises SandboxBlockedError when there is no live VM or the
    listing transport fails (fail closed, never stale output).
    """
    task_id = _task_id_from_workspace(workspace)
    if task_id is None:
        raise _host.SandboxBlockedError(
            "firecracker backend requires a task workspace (task-<id>)"
        )
    sync_guest_to_host(task_id=task_id, workspace=workspace)
    if what == "status":
        return _run_host_git(workspace, "status", "--porcelain=v1", "-uall")
    if what == "diff":
        stat = _run_host_git(workspace, "diff", "--stat", cap=4000)
        diff = _run_host_git(workspace, "diff", cap=16000)
        return f"{stat}\n{diff}"
    raise ValueError(f"unknown git view: {what}")


class FirecrackerBackend:
    """`firecracker` backend: real microVMs, fail-closed, no host fallback."""

    name = "firecracker"

    def _task_id_from_workspace(self, workspace: str) -> int | None:
        return _task_id_from_workspace(workspace)

    def _ensure_vm(self, workspace: str):
        task_id = self._task_id_from_workspace(workspace)
        if task_id is None:
            raise _host.SandboxBlockedError(
                "firecracker backend requires a task workspace (task-<id>)"
            )
        with _REG_LOCK:
            vm = _REG.get(task_id)
        if vm is not None and vm.ready:
            return vm
        return provision(task_id, workspace)

    def run_command(self, workspace: str, command: str, timeout_s=None, cwd="."):
        from app.sandbox.backend import ExecResult as _ExecResult

        if not workspace or not os.path.isdir(workspace):
            raise _host.SandboxBlockedError("sandbox unavailable: workspace does not exist")
        vm = self._ensure_vm(workspace)
        out = exec_in_guest(
            vm.task_id, command=command, cwd=cwd or ".", timeout_s=timeout_s
        )
        return _ExecResult(
            exit_code=out["exit_code"],
            stdout=out["stdout"],
            stderr=out["stderr"],
            truncated=out["truncated"],
            duration_ms=out["duration_ms"],
            timed_out=out["timed_out"],
            cwd=out["cwd"],
        )

    def destroy(self, workspace: str) -> None:
        task_id = self._task_id_from_workspace(workspace)
        if task_id is not None:
            destroy(task_id)

    # --- File ops over vsock (guest is authoritative when this backend is active) ---
    def _guest_call(self, workspace: str, request: dict, timeout_s: int = 30) -> dict:
        vm = self._ensure_vm(workspace)
        client = _guest.VsockClient(cid=vm.cid, port=_GUEST_AGENT_PORT)
        try:
            return client.call(request, timeout_s=timeout_s)
        except Exception as exc:
            raise _host.SandboxBlockedError(f"guest file op failed: {exc}")

    def read_file(self, workspace: str, path: str) -> str:
        from app.sandbox import sandbox as _s

        req = _guest.build_request(op="read", cwd=".", timeout_s=15)
        req["path"] = path
        resp = self._guest_call(workspace, req, timeout_s=20)
        if resp.get("error"):
            return f"ERROR: {resp['error']}"
        return _s.redact(str(resp.get("content", "")))

    def write_file(self, workspace: str, path: str, content: str) -> str:
        req = _guest.build_request(op="write", cwd=".", timeout_s=30)
        req["path"] = path
        req["content"] = content
        resp = self._guest_call(workspace, req, timeout_s=35)
        if resp.get("error"):
            return f"ERROR: {resp['error']}"
        return f"WROTE {path} ({len(content)} bytes)"

    def list_directory(self, workspace: str, path: str = ".") -> str:
        req = _guest.build_request(op="list", cwd=".", timeout_s=15)
        req["path"] = path
        resp = self._guest_call(workspace, req, timeout_s=20)
        if resp.get("error"):
            return f"ERROR: {resp['error']}"
        entries = resp.get("entries", [])
        if not entries:
            return "(empty directory)"
        return "\n".join(str(e) for e in entries[:500])

    def search_code(self, workspace: str, pattern: str) -> str:
        # Regex search executes INSIDE the guest (rg/grep on guest FS).
        vm = self._ensure_vm(workspace)
        client = _guest.VsockClient(cid=vm.cid, port=_GUEST_AGENT_PORT)
        # Quote pattern safely: argv path avoids shell injection.
        req = _guest.build_request(
            op="exec", cwd=".", argv=["sh", "-c", "rg --no-heading --line-number --max-count 40 -e \"$0\" . || grep -rn --exclude-dir=.git -m 40 -e \"$0\" .", pattern],
            timeout_s=30,
        )
        try:
            resp = client.call(req, timeout_s=35)
        except Exception as exc:
            raise _host.SandboxBlockedError(f"guest search failed: {exc}")
        from app.sandbox import sandbox as _s

        out = str(resp.get("stdout", "")).strip() or "(no matches)"
        return _s.redact(out)
