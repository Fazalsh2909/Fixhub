"""Phase 5 M4: per-VM network isolation enforced on the HOST.

Design (upstream Firecracker guidance: the VMM performs NO filtering;
the host must):
- each task VM gets its own netns + TAP device (no shared bridge member
  across tenants beyond the filtered uplink);
- nftables default-deny egress on the TAP uplink: allow only the configured
  egress allowlist (TCP 443 to named suffixes resolved via the host stub),
  drop everything else: metadata 169.254.169.254/32, RFC1918, host services,
  inter-VM CIDRs, direct DNS bypass (UDP/TCP 53 except via the stub);
- IPv6 dropped unless explicitly enabled (not in v1).

All functions are best-effort wrappers around `ip`/`nft` that raise
NetworkIsolationError when enforcement cannot be established — callers must
then refuse to boot the VM (fail closed). On hosts without `ip`/`nft`
(Windows laptop) every function raises immediately.

This module never runs inside the guest.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess

FORBIDDEN_CIDRS = (
    "169.254.169.254/32",  # cloud metadata
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
)


class NetworkIsolationError(RuntimeError):
    pass


def _have_tools() -> bool:
    return bool(shutil.which("ip")) and bool(
        shutil.which("nft") or shutil.which("iptables")
    )


def _run(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), capture_output=True, text=True, timeout=timeout
    )


def egress_allowlist() -> list[str]:
    try:
        from app.config import settings as _settings

        raw = str(getattr(_settings, "FC_EGRESS_ALLOWLIST", "") or "")
    except Exception:
        raw = ""
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def netns_name(task_id: int | str) -> str:
    return f"fixhub-t{task_id}"


def tap_name(task_id: int | str) -> str:
    # Linux ifname limit is 15 chars: keep short.
    return f"ftap{int(task_id) % 100000}"


def ensure_isolation(task_id: int | str) -> dict:
    """Create per-VM netns + TAP and apply default-deny nft rules.

    Returns {netns, tap, nft_applied}. Raises NetworkIsolationError when the
    host cannot enforce isolation (missing tools, non-Linux, command failure).
    """
    import os

    if os.name == "nt":
        raise NetworkIsolationError(
            "network isolation requires Linux (ip/nft); refusing VM boot on Windows"
        )
    if not _have_tools():
        raise NetworkIsolationError(
            "network isolation tools missing (need `ip` + `nft`/`iptables`)"
        )
    netns = netns_name(task_id)
    tap = tap_name(task_id)
    # 1. netns (idempotent: exists is fine, we reuse the name per task).
    proc = _run("ip", "netns", "add", netns)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"ip netns add failed: {(proc.stderr or '')[-300:]}")
    # 2. TAP device bound to the netns.
    proc = _run("ip", "tuntap", "add", "dev", tap, "mode", "tap")
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"tap create failed: {(proc.stderr or '')[-300:]}")
    proc = _run("ip", "link", "set", tap, "netns", netns)
    if proc.returncode != 0:
        raise NetworkIsolationError(f"tap->netns failed: {(proc.stderr or '')[-300:]}")
    # 3. nft default-deny. Prefer nft; fall back to iptables-nft errors as failure.
    nft = shutil.which("nft")
    if not nft:
        raise NetworkIsolationError("nft not found (iptables fallback not allowlisted for v1)")
    rules = _nft_ruleset(tap=tap, allow=egress_allowlist())
    proc = subprocess.run(
        [nft, "-f", "-"], input=rules, capture_output=True, text=True, timeout=15
    )
    if proc.returncode != 0:
        raise NetworkIsolationError(f"nft apply failed: {(proc.stderr or '')[-300:]}")
    return {"netns": netns, "tap": tap, "nft_applied": True}


def _nft_ruleset(*, tap: str, allow: list[str]) -> str:
    """Build a minimal default-deny ruleset for one TAP uplink.

    NOTE: hostname allowlisting happens at the egress proxy/stub layer (DNS
    resolution is host-controlled); nft pins numeric forbiddens + stateful
    allow-established. L7 hostname filtering is enforced by routing ALL guest
    TCP 443 through the host proxy (see docs/FIRECRACKER_NETWORK.md); nft is
    the backstop that drops direct bypasses.
    """
    lines = [
        "flush chain inet fixhub_vm out;",
        "table inet fixhub_vm {",
        "  chain out {",
        '    type filter hook forward priority 0; policy drop;',
        f'    iifname "{tap}" ct state established,related accept;',
        # Hard drops first (metadata, private nets, loopback via forward path).
    ]
    for cidr in FORBIDDEN_CIDRS:
        lines.append(f'    iifname "{tap}" ip daddr {cidr} drop;')
    # No direct DNS: guests must use the host stub (10.200.0.1:53 in netns).
    lines.append(f'    iifname "{tap}" udp dport 53 drop;')
    lines.append(f'    iifname "{tap}" tcp dport 53 drop;')
    # IPv6 closed in v1.
    lines.append(f'    iifname "{tap}" meta l4proto ipv6-icmp drop;')
    lines.append(f'    iifname "{tap}" ip6 daddr ::/0 drop;')
    # Allowed egress: TCP 443 toward the uplink (proxy enforces hostnames).
    # The proxy listens on the host side; guests have no other route out.
    lines.append(f'    iifname "{tap}" tcp dport 443 accept;')
    lines.append("  }")
    lines.append("}")
    lines.append(f"# allowlist (proxy-enforced): {', '.join(allow) or '(empty)'}")
    return "\n".join(lines) + "\n"


def destroy_isolation(task_id: int | str) -> None:
    """Best-effort removal of per-VM netns/TAP. Never raises."""
    try:
        netns = netns_name(task_id)
        tap = tap_name(task_id)
        _run("ip", "netns", "delete", netns)
        _run("ip", "link", "delete", tap)
    except Exception:
        pass


def check_forbidden(cidrs: tuple[str, ...] = FORBIDDEN_CIDRS) -> bool:
    """Validate the forbidden list parses as CIDRs (unit-testable, no root)."""
    try:
        for c in cidrs:
            ipaddress.ip_network(c)
        return True
    except ValueError:
        return False
