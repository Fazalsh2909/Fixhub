"""Phase 5: unprivileged client for the root-owned network daemon (fixhub-netd).

The FastAPI/Uvicorn API runs as ``User=fixhub`` with ``NoNewPrivileges=true``,
so it cannot create network namespaces, TAP/veth devices, nftables rules, or
spawn the jailer directly. All privileged host mutations go through
``fixhub-netd`` (``infra/firecracker/fixhub-netd.py``, a root-owned systemd
service) over a Unix-domain socket.

This module is deliberately stdlib-only and exposes a STRICT, TYPED operation
interface:

* fixed op enum: probe | setup | teardown | prepare-jail | finalize-jail |
  spawn-jailer | destroy-jail. Anything else is rejected by the daemon.
* the only client-supplied values are ``task_id`` (strictly validated) and,
  for spawn-jailer, three bounded resource scalars. Interface names, IP
  addresses, routes, nftables chains, executable paths, and argv are ALWAYS
  derived server-side from ``task_id`` — a client can never ask the daemon to
  touch another task's objects, and there is no generic command runner here
  (no ``shell=True``, no argv forwarding, no executable paths on the wire).

Activation is explicit: delegation happens only when the ``FC_NETD_SOCKET``
environment variable is set (the production systemd unit sets it), the
process is NOT root, and ``FC_NETD_DIRECT=1`` is not set. Everywhere else
(laptop dev, pytest, manual root runs) the existing direct code paths run
unchanged. When delegation is active but the daemon is unreachable, every
setup call raises ``NetdError`` so callers fail closed — never host fallback.
"""

from __future__ import annotations

import json
import os
import socket

DEFAULT_SOCKET_PATH = "/run/fixhub-netd.sock"
MAX_TASK_ID = 2**31 - 1

# Operations the daemon implements. Kept as a frozenset so membership tests
# are exact; the wire format carries the op name, never code or argv.
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

# Bounds for spawn-jailer resource scalars (mirror the sane operating
# envelope; the daemon re-validates and clamps independently).
VCPU_MIN, VCPU_MAX = 1, 16
MEM_MIN_MIB, MEM_MAX_MIB = 128, 16384
PIDS_MIN, PIDS_MAX = 16, 4096

_REQUEST_TIMEOUT_S = 90
_MAX_RESPONSE_BYTES = 1024 * 1024


class NetdError(RuntimeError):
    """Privilege delegation failed. Callers must refuse to boot (fail closed)."""


def validate_task_id(task_id: int | str) -> int:
    """Strictly validate a task id for the privilege boundary.

    Returns the canonical int. Rejects bools, floats, empty/negative/zero/
    out-of-range values, and any string that is not pure ASCII digits.
    """
    if isinstance(task_id, bool):
        raise NetdError(f"invalid task_id: {task_id!r}")
    if isinstance(task_id, int):
        tid = task_id
    elif isinstance(task_id, str):
        text = task_id.strip()
        if not text or not text.isascii() or not text.isdigit():
            raise NetdError(f"invalid task_id: {task_id!r}")
        try:
            tid = int(text, 10)
        except ValueError:
            raise NetdError(f"invalid task_id: {task_id!r}")
    else:
        raise NetdError(f"invalid task_id: {task_id!r}")
    if tid < 1 or tid > MAX_TASK_ID:
        raise NetdError(f"task_id out of range: {task_id!r}")
    return tid


def validate_spawn_resources(vcpu: int, mem_mib: int, pids_max: int) -> tuple[int, int, int]:
    """Validate spawn-jailer resource scalars (ints inside hard bounds)."""
    for name, value, lo, hi in (
        ("vcpu", vcpu, VCPU_MIN, VCPU_MAX),
        ("mem_mib", mem_mib, MEM_MIN_MIB, MEM_MAX_MIB),
        ("pids_max", pids_max, PIDS_MIN, PIDS_MAX),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise NetdError(f"invalid {name}: {value!r}")
        if value < lo or value > hi:
            raise NetdError(f"{name} out of range [{lo},{hi}]: {value!r}")
    return vcpu, mem_mib, pids_max


def socket_path() -> str:
    """Resolve the daemon socket path (env wins, then settings, then default)."""
    env = (os.environ.get("FC_NETD_SOCKET", "") or "").strip()
    if env:
        return env
    try:
        from app.config import settings as _settings

        configured = str(getattr(_settings, "FC_NETD_SOCKET", "") or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    return DEFAULT_SOCKET_PATH


def _is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:
        # Windows / platforms without geteuid: never privileged.
        return False


def delegation_active() -> bool:
    """True when network/jail ops must go through the daemon.

    Explicit opt-in only: ``FC_NETD_SOCKET`` must be set in the environment
    (the production API unit sets it), ``FC_NETD_DIRECT=1`` must not be set,
    and the process must not be root. Everywhere else the direct code paths
    run unchanged, so laptop dev and the existing test suite are unaffected.
    """
    if (os.environ.get("FC_NETD_DIRECT", "") or "").strip() == "1":
        return False
    if not (os.environ.get("FC_NETD_SOCKET", "") or "").strip():
        return False
    if _is_root():
        return False
    return True


def _call(op: str, payload: dict | None = None, timeout_s: int = _REQUEST_TIMEOUT_S) -> dict:
    if op not in OPS:
        raise NetdError(f"unknown netd op: {op!r}")
    if not hasattr(socket, "AF_UNIX"):
        raise NetdError(
            "privilege delegation unavailable "
            "(fixhub-netd needs Unix sockets, unsupported on this platform); "
            "refusing boot"
        )
    request = {"op": op}
    if payload:
        request.update(payload)
    # The wire schema is closed: only these scalar keys may be sent. Anything
    # else (interface names, IPs, chains, paths, argv) is a programming error.
    allowed_keys = {"op", "task_id", "vcpu", "mem_mib", "pids_max"}
    for key in request:
        if key not in allowed_keys:
            raise NetdError(f"netd request carries a forbidden field: {key!r}")
    try:
        raw = (json.dumps(request) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NetdError(f"netd request unserializable: {exc}")
    path = socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            sock.settimeout(timeout_s)
            sock.connect(path)
        except OSError as exc:
            raise NetdError(
                "privilege delegation unavailable "
                f"(cannot reach fixhub-netd at {path}: {exc}); refusing boot"
            )
        try:
            sock.sendall(raw)
        except OSError as exc:
            raise NetdError(f"netd request send failed: {exc}")
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                raise NetdError(f"netd op {op!r} timed out: {exc}")
            except OSError as exc:
                raise NetdError(f"netd response read failed: {exc}")
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise NetdError("netd response too large")
            if chunk.endswith(b"\n"):
                break
        try:
            response = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise NetdError(f"netd response unreadable: {exc}")
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        raise NetdError("netd response malformed")
    if not response["ok"]:
        detail = str(response.get("error", "unknown daemon error"))[:500]
        raise NetdError(f"netd op {op!r} failed: {detail}")
    data = response.get("data", {})
    return data if isinstance(data, dict) else {}


def probe() -> dict:
    """Check daemon reachability + authorization. Raises NetdError when unusable."""
    return _call("probe", None, timeout_s=10)


def setup(task_id: int | str) -> dict:
    """Establish per-task network isolation via the daemon (fail closed)."""
    tid = validate_task_id(task_id)
    return _call("setup", {"task_id": tid}, timeout_s=_REQUEST_TIMEOUT_S)


def teardown(task_id: int | str) -> dict:
    """Best-effort per-task network teardown via the daemon (never raises NetdError)."""
    try:
        tid = validate_task_id(task_id)
    except NetdError:
        return {}
    try:
        return _call("teardown", {"task_id": tid}, timeout_s=60)
    except NetdError:
        return {}


def prepare_jail(task_id: int | str) -> dict:
    """Create the per-task jail dirs (root) and hand them to fixhub. Fail closed."""
    tid = validate_task_id(task_id)
    return _call("prepare-jail", {"task_id": tid}, timeout_s=60)


def finalize_jail(task_id: int | str) -> dict:
    """Chown staged kernel+overlay to the per-VM uid:gid (root). Fail closed."""
    tid = validate_task_id(task_id)
    return _call("finalize-jail", {"task_id": tid}, timeout_s=60)


def spawn_jailer(task_id: int | str, *, vcpu: int, mem_mib: int, pids_max: int) -> dict:
    """Spawn the jailer as root with fully server-derived args. Fail closed."""
    tid = validate_task_id(task_id)
    vcpu, mem_mib, pids_max = validate_spawn_resources(vcpu, mem_mib, pids_max)
    return _call(
        "spawn-jailer",
        {"task_id": tid, "vcpu": vcpu, "mem_mib": mem_mib, "pids_max": pids_max},
        timeout_s=_REQUEST_TIMEOUT_S,
    )


def grant_vsock(task_id: int | str) -> dict:
    """Grant the fixhub group access to this VM's host-side vsock socket."""
    tid = validate_task_id(task_id)
    return _call("grant-vsock", {"task_id": tid}, timeout_s=10)


def destroy_jail(task_id: int | str) -> dict:
    """Best-effort full jail destroy (procs + tree + network). Never raises."""
    try:
        tid = validate_task_id(task_id)
    except NetdError:
        return {}
    try:
        return _call("destroy-jail", {"task_id": tid}, timeout_s=60)
    except NetdError:
        return {}
