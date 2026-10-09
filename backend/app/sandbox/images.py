"""Phase 5 M1: Firecracker artifact resolution + verification.

Immutable base kernel/rootfs live on the HOST and are versioned + checksummed.
Per-task overlays are disposable copies destroyed with the VM. Nothing here
executes guest code; it only locates and validates host files.
"""

from __future__ import annotations

import hashlib
import os


class ArtifactMissing(RuntimeError):
    pass


def _setting(name: str, default: str = "") -> str:
    try:
        from app.config import settings as _settings

        return str(getattr(_settings, name, default) or default)
    except Exception:
        return default


def kernel_image() -> str:
    return _setting("FC_KERNEL_IMAGE", "/srv/firecracker/kernel/vmlinux")


def rootfs_image() -> str:
    return _setting("FC_ROOTFS_IMAGE", "/srv/firecracker/rootfs/base.ext4")


def chroot_base() -> str:
    return _setting("FC_CHROOT_BASE", "/srv/firecracker/jails")


def firecracker_binary() -> str:
    return _setting("FC_BINARY", "/usr/local/bin/firecracker")


def jailer_binary() -> str:
    return _setting("JAILER_BINARY", "/usr/local/bin/jailer")


def verify_artifacts() -> dict:
    """Check all host prerequisites. Returns {ok, missing[], info{}}.

    Never raises. Callers decide fail-closed (prod boot, KVM CI) vs skip (laptop dev).
    """
    missing: list[str] = []
    info: dict = {}
    # Binaries must exist + be executable.
    for label, path in (
        ("firecracker", firecracker_binary()),
        ("jailer", jailer_binary()),
    ):
        ok = bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)
        info[label] = path
        if not ok:
            missing.append(f"{label}:{path or '(unset)'}")
    # Images must exist + be non-empty regular files.
    for label, path in (("kernel", kernel_image()), ("rootfs", rootfs_image())):
        ok = bool(path) and os.path.isfile(path)
        try:
            size = os.path.getsize(path) if ok else 0
        except OSError:
            size = 0
        info[label] = path
        info[f"{label}_bytes"] = size
        if not ok or size <= 0:
            missing.append(f"{label}:{path or '(unset)'}")
    # Kernel must look uncompressed (Firecracker rejects bzImage wrappers).
    # Heuristic only: ELF magic or "Linux version" string near the head.
    kpath = kernel_image()
    if os.path.isfile(kpath):
        try:
            with open(kpath, "rb") as fh:
                head = fh.read(4)
            info["kernel_magic"] = head.hex()
            if head[:4] != b"\x7fELF" and head[:2] != b"MZ":
                # vmlinux is ELF; anything else is suspicious but not fatal here —
                # the real boot test is authoritative. Record, don't invent failure.
                info["kernel_magic_note"] = "not ELF; boot test decides"
        except OSError:
            pass
    # chroot base must exist + be a directory (jailer requires root-owned,
    # non-world-writable — checked at provision time, not here).
    cbase = chroot_base()
    info["chroot_base"] = cbase
    if not cbase or not os.path.isdir(cbase):
        missing.append(f"chroot_base:{cbase or '(unset)'}")
    # KVM availability.
    kvm = os.path.exists("/dev/kvm")
    info["kvm"] = "/dev/kvm" if kvm else "missing"
    try:
        info["kvm_rw"] = bool(os.access("/dev/kvm", os.R_OK | os.W_OK))
    except Exception:
        info["kvm_rw"] = False
    if not kvm:
        missing.append("kvm:/dev/kvm")
    return {"ok": not missing, "missing": missing, "info": info}


def require_artifacts() -> dict:
    """verify_artifacts() or raise ArtifactMissing (fail-closed entry)."""
    result = verify_artifacts()
    if not result["ok"]:
        raise ArtifactMissing(
            "firecracker prerequisites missing: " + "; ".join(result["missing"])
        )
    return result["info"]


def sha256_file(path: str, limit_bytes: int = 64 * 1024 * 1024) -> str:
    """Hex digest of a file prefix (bounded; full-file when small). Never raises."""
    try:
        h = hashlib.sha256()
        remaining = limit_bytes
        with open(path, "rb") as fh:
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
        return h.hexdigest()
    except OSError:
        return ""
