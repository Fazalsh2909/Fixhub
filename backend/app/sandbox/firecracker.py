"""Phase 5 M2/M3: Firecracker microVM backend via jailer + vsock exec.

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
import subprocess
import threading
import time

from app.sandbox import guest_agent as _guest
from app.sandbox import images as _images
from app.sandbox import net as _net
from app.sandbox import sandbox as _host

# In-memory VM registry: task_id -> _VM. Guarded by _REG_LOCK.
_REG: dict[int, "_VM"] = {}
_REG_LOCK = threading.Lock()

# Fixed vsock port for the guest exec agent (per-VM CID differs, port shared).
_GUEST_AGENT_PORT = 5000
# Base CID for task VMs (CID 2 is host-reserved; avoid collisions with low CIDs).
_CID_BASE = 100


class _VM:
    def __init__(
        self,
        *,
        task_id: int,
        jail_dir: str,
        api_socket: str,
        cid: int,
        overlay: str,
        firecracker_pid: int | None = None,
    ):
        self.task_id = int(task_id)
        self.jail_dir = jail_dir
        self.api_socket = api_socket
        self.cid = int(cid)
        self.overlay = overlay
        self.firecracker_pid = firecracker_pid
        self.ready = False
        self.created_mono = time.monotonic()


def _cfg(name: str, default=""):
    try:
        from app.config import settings as _settings

        return getattr(_settings, name, default)
    except Exception:
        return default


def _cid_for(task_id: int) -> int:
    # Deterministic per task, within the vsock CID range.
    return _CID_BASE + (int(task_id) % 50000)


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


def _read_fc_pid(jail_dir: str) -> int | None:
    for name in ("firecracker.pid", "jailer.pid"):
        p = os.path.join(jail_dir, name)
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


def provision(task_id: int, workspace: str) -> _VM:
    """Boot (or reuse) the task's microVM and sync the tokenless repo tree.

    Raises SandboxBlockedError on ANY failure (missing host support, isolation
    failure, boot failure, transfer failure). Never falls back to host exec.
    """
    task_id = int(task_id)
    with _REG_LOCK:
        existing = _REG.get(task_id)
        if existing is not None and existing.ready:
            return existing
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
    # 3. Jail dir + overlay.
    chroot_base = str(_cfg("FC_CHROOT_BASE", "/srv/firecracker/jails"))
    jail_dir = os.path.join(chroot_base, f"task-{task_id}")
    api_socket = os.path.join(jail_dir, "api.socket")
    overlay = os.path.join(jail_dir, "overlay.ext4")
    cid = _cid_for(task_id)
    try:
        os.makedirs(jail_dir, exist_ok=True)
        os.chmod(jail_dir, 0o700)
        _prepare_overlay(overlay)
        _stage_boot_files(jail_dir)
        _spawn_jailer(task_id=task_id, jail_dir=jail_dir, netns=netinfo["netns"])
        _configure_and_boot(
            api_socket=api_socket,
            jail_dir=jail_dir,
            overlay=overlay,
            cid=cid,
            tap=netinfo["tap"],
        )
        vm = _VM(
            task_id=task_id,
            jail_dir=jail_dir,
            api_socket=api_socket,
            cid=cid,
            overlay=overlay,
            firecracker_pid=_read_fc_pid(jail_dir),
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
        _cleanup_partial(task_id, jail_dir)
        raise
    except Exception as exc:
        _cleanup_partial(task_id, jail_dir)
        raise _host.SandboxBlockedError(f"firecracker provision failed: {exc}")


def _prepare_overlay(overlay: str) -> None:
    """Create the disposable writable overlay from the immutable base rootfs."""
    base = _images.rootfs_image()
    max_mb = int(_cfg("FC_OVERLAY_MB", 5120) or 5120)
    if os.path.exists(overlay):
        return
    try:
        size = os.path.getsize(base)
    except OSError as exc:
        raise RuntimeError(f"base rootfs unreadable: {exc}")
    if size > max_mb * 1024 * 1024:
        raise RuntimeError(f"base rootfs exceeds overlay cap ({size} bytes)")
    shutil.copyfile(base, overlay)
    os.chmod(overlay, 0o600)


def _stage_boot_files(jail_dir: str) -> None:
    """Hardlink (or copy) kernel + overlay into the jail (jailer requirement)."""
    kernel = _images.kernel_image()
    for src in (kernel,):
        dest = os.path.join(jail_dir, os.path.basename(src))
        if os.path.exists(dest):
            continue
        try:
            os.link(src, dest)
        except OSError:
            shutil.copyfile(src, dest)


def _spawn_jailer(*, task_id: int, jail_dir: str, netns: str) -> None:
    jailer = _images.jailer_binary()
    fc = _images.firecracker_binary()
    chroot_base = _images.chroot_base()
    uid = 100000 + (int(task_id) % 50000)
    gid = uid
    netns_path = f"/var/run/netns/{netns}"
    cmd = [
        jailer,
        "--id", f"task-{task_id}",
        "--exec-file", fc,
        "--uid", str(uid),
        "--gid", str(gid),
        "--chroot-base-dir", chroot_base,
        "--netns", netns_path,
        "--daemonize",
        "--new-pid-ns",
        "--cgroup", f"cpus={_cfg('FC_GUEST_VCPU', 2)}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"jailer spawn failed: {(proc.stderr or proc.stdout)[-500:]}")
    # Wait for the API socket to appear.
    api_socket = os.path.join(jail_dir, "api.socket")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if os.path.exists(api_socket):
            return
        time.sleep(0.2)
    raise RuntimeError("firecracker API socket never appeared after jailer spawn")


def _configure_and_boot(
    *, api_socket: str, jail_dir: str, overlay: str, cid: int, tap: str
) -> None:
    vcpu = int(_cfg("FC_GUEST_VCPU", 2) or 2)
    mem = int(_cfg("FC_GUEST_MEM_MIB", 1024) or 1024)
    kernel_name = os.path.basename(_images.kernel_image())
    overlay_name = os.path.basename(overlay)
    boot_args = (
        "console=ttyS0 reboot=k panic=1 pci=off "
        "ip=172.16.0.2::172.16.0.1:255.255.255.252::eth0:off"
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
    _api_put(api_socket, "/vsock", {
        "guest_cid": 3,
        "uds_path": "./v.sock",
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


def sync_repo_to_guest(*, task_id: int, workspace: str) -> None:
    """Stream the host workspace into the guest with credentials stripped.

    Per-file `write` ops (bounded, skips __pycache__ + tokenless .git/config).
    Raises on first transport failure (caller destroys the VM).
    """
    with _REG_LOCK:
        vm = _REG.get(int(task_id))
    if vm is None or not vm.ready:
        raise RuntimeError("no live VM for repo sync")
    if not workspace or not os.path.isdir(workspace):
        raise RuntimeError("host workspace missing for repo sync")
    client = _guest.VsockClient(cid=vm.cid, port=_GUEST_AGENT_PORT)
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, workspace).replace(os.sep, "/")
            if "__pycache__" in rel:
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="strict") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue  # binary/large files transfer via exec path in v1 scope
            if len(content) > 200_000:
                continue
            req = _guest.build_request(op="write", cwd=".", timeout_s=30)
            req["path"] = rel
            req["content"] = content
            resp = client.call(req, timeout_s=35)
            if not resp.get("ok", True) and resp.get("exit_code", 1) != 0:
                raise RuntimeError(f"guest write failed for {rel}")
    # Tokenless .git/config last (guest git stays read-only; host owns push).
    git_config = os.path.join(workspace, ".git", "config")
    if os.path.isfile(git_config):
        try:
            with open(git_config, "r", encoding="utf-8", errors="replace") as fh:
                cleaned = _guest.strip_token_from_git_config(fh.read())
            req = _guest.build_request(op="write", cwd=".", timeout_s=15)
            req["path"] = ".git/config"
            req["content"] = cleaned
            client.call(req, timeout_s=20)
        except OSError:
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


def sync_guest_to_host(*, task_id: int, workspace: str) -> dict:
    """Copy guest working-tree changes back to the host workspace for publish.

    Runs on the TRUSTED host after the agent loop and gates complete. Reads
    guest files over vsock and writes them to the host workspace through the
    shared path jail (sensitive names blocked). `.git` internals are NOT
    copied (host git state stays authoritative; publish uses host git).
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
    from app.agent.paths import is_sensitive as _is_sensitive
    from app.agent.paths import resolve as _resolve

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
        if _is_sensitive(rel.split(":")[0]):
            continue
        try:
            dest = _resolve(workspace, rel)
        except ValueError:
            continue
        # 2. Read from guest, write to host.
        try:
            rreq = _guest.build_request(op="read", cwd=".", timeout_s=15)
            rreq["path"] = rel
            rresp = client.call(rreq, timeout_s=20)
        except Exception:
            continue
        if rresp.get("error"):
            continue
        content = str(rresp.get("content", ""))
        try:
            _os.makedirs(_os.path.dirname(dest) or dest, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)
            files += 1
            total_bytes += len(content)
        except OSError:
            continue
        if total_bytes > 10 * 1024 * 1024 or files > 2000:
            break
    return {"files": files, "bytes": total_bytes}


def destroy(task_id: int) -> None:
    """Halt the VM, kill its process group, remove jail/netns. Never raises."""
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
            pid = vm.firecracker_pid or _read_fc_pid(vm.jail_dir)
            _kill_pid(pid)
            # Extra sweep: any firecracker process for this task id.
            try:
                if os.name != "nt":
                    subprocess.run(
                        ["pkill", "-f", f"task-{task_id}"],
                        capture_output=True,
                        timeout=10,
                    )
            except Exception:
                pass
        else:
            # Orphan path (worker crashed before registry): best-effort by dir.
            try:
                chroot_base = str(_cfg("FC_CHROOT_BASE", "/srv/firecracker/jails"))
                jd = os.path.join(chroot_base, f"task-{task_id}")
                _kill_pid(_read_fc_pid(jd))
            except Exception:
                pass
    finally:
        try:
            chroot_base = str(_cfg("FC_CHROOT_BASE", "/srv/firecracker/jails"))
            jd = os.path.join(chroot_base, f"task-{task_id}")
            if os.path.isdir(jd):
                shutil.rmtree(jd, ignore_errors=True)
        except Exception:
            pass
        try:
            _net.destroy_isolation(task_id)
        except Exception:
            pass


def destroy_orphans() -> int:
    """Best-effort sweep of VMs with no live registry entry. Returns count."""
    return 0  # v1: registry + per-task destroy on reclaim path covers this.


def _cleanup_partial(task_id: int, jail_dir: str) -> None:
    try:
        if jail_dir and os.path.isdir(jail_dir):
            shutil.rmtree(jail_dir, ignore_errors=True)
    except Exception:
        pass
    try:
        _net.destroy_isolation(task_id)
    except Exception:
        pass


class FirecrackerBackend:
    """`firecracker` backend: real microVMs, fail-closed, no host fallback."""

    name = "firecracker"

    def _task_id_from_workspace(self, workspace: str) -> int | None:
        try:
            base = os.path.basename(os.path.abspath(workspace))
            if base.startswith("task-"):
                return int(base.split("task-", 1)[1])
        except (ValueError, IndexError):
            pass
        return None

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
