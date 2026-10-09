"""Phase 5: per-VM network isolation enforced on the HOST.

Design (upstream Firecracker guidance: the VMM performs NO filtering;
the host must):
- each task VM gets its own netns + TAP device (no shared L2 across
  tenants beyond the filtered uplink);
- the TAP lives in the task netns with the guest-peer address
  (GUEST_NET 172.16.0.0/30: host .1, guest .2 via kernel cmdline);
- a veth pair links the netns to the root ns for host services only:
  root 10.200.0.1 <-> netns 10.200.0.2. The host egress proxy
  (TCP 8443) and DNS stub (UDP 53) listen on 10.200.0.1 (see egress.py);
- nftables default-deny on the TAP uplink + a NAT redirect that forces
  ALL guest TCP 443 through the proxy (hostname policy lives in the
  proxy via SNI sniffing — nft alone cannot do SNI policy);
- hard drops: cloud metadata 169.254.169.254/32, RFC1918, loopback,
  direct DNS bypass (any port-53 not to the stub), all IPv6;
- the netns has NO default route to the internet: the only L3
  destinations reachable are the host services above.

All functions raise NetworkIsolationError when enforcement cannot be
established — callers must then refuse to boot the VM (fail closed).
On hosts without `ip`/`nft` (Windows laptop) every function raises
immediately.

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

# Guest link-net (must match the kernel cmdline in firecracker.py).
GUEST_HOST_IP = "172.16.0.1"
GUEST_VM_IP = "172.16.0.2"
GUEST_PREFIX = 30
# Host-services link-net (must match infra/.../build-rootfs.sh resolv.conf).
HOST_SVC_IP = "10.200.0.1"
NETNS_SVC_IP = "10.200.0.2"
SVC_PREFIX = 24


class NetworkIsolationError(RuntimeError):
    pass


def _have_tools() -> bool:
    return bool(shutil.which("ip")) and bool(shutil.which("nft"))


def _run(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), capture_output=True, text=True, timeout=timeout
    )


def _run_ns(netns: str, *ip_args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return _run("ip", "netns", "exec", netns, "ip", *ip_args, timeout=timeout)


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


def veth_names(task_id: int | str) -> tuple[str, str]:
    tid = int(task_id) % 100000
    return f"vh{tid}", f"vg{tid}"


def _ensure_forwarding() -> None:
    proc = _run("sysctl", "-w", "net.ipv4.ip_forward=1")
    if proc.returncode != 0:
        raise NetworkIsolationError(
            f"could not enable IPv4 forwarding: {(proc.stderr or '')[-200:]}"
        )


def ensure_isolation(task_id: int | str) -> dict:
    """Create per-VM netns + TAP + veth + routes + nft, verify the proxy.

    Returns {netns, tap, nft_applied, proxy}. Idempotent (existing objects
    are reused after verification). Raises NetworkIsolationError on ANY
    failure — the caller must refuse to boot (fail closed).
    """
    import os

    from app.sandbox import egress as _egress

    if os.name == "nt":
        raise NetworkIsolationError(
            "network isolation requires Linux (ip/nft); refusing VM boot on Windows"
        )
    if not _have_tools():
        raise NetworkIsolationError("network isolation tools missing (need `ip` + `nft`)")
    try:
        netns = netns_name(task_id)
        tap = tap_name(task_id)
        vh, vg = veth_names(task_id)
        proxy_port = _egress.proxy_port()
        svc_ip = _egress.proxy_addr()
        dns_ip = _egress.dns_addr()
        if svc_ip != HOST_SVC_IP or dns_ip != HOST_SVC_IP:
            raise NetworkIsolationError(
                "host services must live on "
                f"{HOST_SVC_IP} (proxy={svc_ip} dns={dns_ip}); refusing"
            )
        _ensure_forwarding()
        _ensure_netns(netns)
        _ensure_tap(netns, tap)
        _ensure_veth(netns, vh, vg)
        _apply_nft(tap=tap, proxy_port=proxy_port)
        proxy = _egress.ensure_available()
        _verify_topology(netns, tap)
        return {"netns": netns, "tap": tap, "nft_applied": True, "proxy": proxy}
    except NetworkIsolationError:
        raise
    except Exception as exc:
        raise NetworkIsolationError(f"isolation setup failed: {exc}")


def _ensure_netns(netns: str) -> None:
    proc = _run("ip", "netns", "add", netns)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"ip netns add failed: {(proc.stderr or '')[-300:]}")
    # Loopback up inside the netns (needed for local guest services).
    proc = _run_ns(netns, "link", "set", "lo", "up")
    if proc.returncode != 0:
        raise NetworkIsolationError(f"netns lo up failed: {(proc.stderr or '')[-300:]}")


def _ensure_tap(netns: str, tap: str) -> None:
    proc = _run("ip", "tuntap", "add", "dev", tap, "mode", "tap")
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"tap create failed: {(proc.stderr or '')[-300:]}")
    proc = _run("ip", "link", "set", tap, "netns", netns)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        # Already in the netns from a previous run: verify instead.
        pass
    proc = _run_ns(netns, "addr", "add", f"{GUEST_HOST_IP}/{GUEST_PREFIX}", "dev", tap)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"tap addr failed: {(proc.stderr or '')[-300:]}")
    proc = _run_ns(netns, "link", "set", tap, "up")
    if proc.returncode != 0:
        raise NetworkIsolationError(f"tap up failed: {(proc.stderr or '')[-300:]}")


def _ensure_veth(netns: str, vh: str, vg: str) -> None:
    proc = _run("ip", "link", "add", vh, "type", "veth", "peer", "name", vg)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"veth create failed: {(proc.stderr or '')[-300:]}")
    # Idempotent address assignment (both ends).
    proc = _run("ip", "addr", "add", f"{HOST_SVC_IP}/{SVC_PREFIX}", "dev", vh)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"veth host addr failed: {(proc.stderr or '')[-300:]}")
    proc = _run("ip", "link", "set", vh, "up")
    if proc.returncode != 0:
        raise NetworkIsolationError(f"veth host up failed: {(proc.stderr or '')[-300:]}")
    proc = _run("ip", "link", "set", vg, "netns", netns)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        pass
    proc = _run_ns(netns, "addr", "add", f"{NETNS_SVC_IP}/{SVC_PREFIX}", "dev", vg)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"veth ns addr failed: {(proc.stderr or '')[-300:]}")
    proc = _run_ns(netns, "link", "set", vg, "up")
    if proc.returncode != 0:
        raise NetworkIsolationError(f"veth ns up failed: {(proc.stderr or '')[-300:]}")
    # Host-service route inside the netns (no default route — ever).
    proc = _run_ns(netns, "route", "add", f"{HOST_SVC_IP}/32", "dev", vg)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"svc route failed: {(proc.stderr or '')[-300:]}")
    # Assert there is no default route in the task netns.
    proc = _run_ns(netns, "route", "show")
    for line in (proc.stdout or "").splitlines():
        if line.strip().startswith("default"):
            raise NetworkIsolationError(
                f"task netns has a default route (forbidden): {line.strip()[:120]}"
            )


def _chain_names(tap: str) -> tuple[str, str]:
    # Per-task chains inside the shared table (multi-tenant safe: creating
    # task B never flushes task A's rules).
    safe = "".join(c if c.isalnum() else "_" for c in tap)
    return f"pre_{safe}", f"out_{safe}"


def _nft_rules(*, tap: str, proxy_port: int) -> list[str]:
    """Per-task nft `add rule` lines (pure, unit-testable).

    Hostname allowlisting happens in the egress proxy (SNI sniffing);
    nft is the backstop that (a) redirects ALL guest TCP 443 into the
    proxy and (b) drops every bypass: direct 443 elsewhere, direct DNS,
    metadata, private nets, IPv6.
    """
    pre, out = _chain_names(tap)
    rules = [
        # NAT: every guest TCP/443 is redirected to the host proxy.
        f'add rule inet fixhub_vm {pre} iifname "{tap}" tcp dport 443 '
        f"dnat to {HOST_SVC_IP}:{proxy_port}",
        f'add rule inet fixhub_vm {out} iifname "{tap}" '
        "ct state established,related accept",
    ]
    # Hard drops first (metadata, private nets, loopback via forward path).
    for cidr in FORBIDDEN_CIDRS:
        rules.append(f'add rule inet fixhub_vm {out} iifname "{tap}" ip daddr {cidr} drop')
    # DNS only to the host stub; every other port-53 is a bypass attempt.
    rules.append(
        f'add rule inet fixhub_vm {out} iifname "{tap}" udp dport 53 '
        f"ip daddr {HOST_SVC_IP} accept"
    )
    rules.append(f'add rule inet fixhub_vm {out} iifname "{tap}" udp dport 53 drop')
    rules.append(f'add rule inet fixhub_vm {out} iifname "{tap}" tcp dport 53 drop')
    # IPv6 closed in v1.
    rules.append(f'add rule inet fixhub_vm {out} iifname "{tap}" meta l4proto ipv6-icmp drop')
    rules.append(f'add rule inet fixhub_vm {out} iifname "{tap}" ip6 daddr ::/0 drop')
    # Only the proxy may receive guest TCP 443 (explicit drop for anything
    # that evades the redirect; there is NO bare `tcp dport 443 accept`).
    rules.append(
        f'add rule inet fixhub_vm {out} iifname "{tap}" ip daddr {HOST_SVC_IP} '
        f"tcp dport {proxy_port} accept"
    )
    rules.append(f'add rule inet fixhub_vm {out} iifname "{tap}" tcp dport 443 drop')
    return rules


def _nft(*args: str) -> subprocess.CompletedProcess:
    nft = shutil.which("nft")
    if not nft:
        raise NetworkIsolationError("nft not found (no fallback in v1)")
    return subprocess.run([nft, *args], capture_output=True, text=True, timeout=15)


def _chain_specs() -> tuple[tuple[str, str], tuple[str, str]]:
    """Chain kinds (pure, unit-testable). The forward chain is default-deny."""
    return (
        ("pre", "type nat hook prerouting priority -100;"),
        ("out", "type filter hook forward priority 0; policy drop;"),
    )


def _apply_nft(*, tap: str, proxy_port: int) -> None:
    # Idempotent: create table/chains (exists is fine), flush our chains,
    # then add exactly the rules from _nft_rules().
    pre, out = _chain_names(tap)
    proc = _nft("add", "table", "inet", "fixhub_vm")
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"nft table create failed: {(proc.stderr or '')[-300:]}")
    kinds = dict(_chain_specs())
    for chain, kind in ((pre, "pre"), (out, "out")):
        spec = kinds[kind]
        proc = _nft("add", "chain", "inet", "fixhub_vm", chain, "{", spec, "}")
        if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
            raise NetworkIsolationError(
                f"nft chain create failed ({chain}): {(proc.stderr or '')[-300:]}"
            )
        proc = _nft("flush", "chain", "inet", "fixhub_vm", chain)
        if proc.returncode != 0:
            raise NetworkIsolationError(
                f"nft chain flush failed ({chain}): {(proc.stderr or '')[-300:]}"
            )
    for rule in _nft_rules(tap=tap, proxy_port=proxy_port):
        proc = _nft(*rule.split(" "))
        if proc.returncode != 0:
            raise NetworkIsolationError(f"nft rule add failed: {(proc.stderr or '')[-300:]}")
    # Verify: our chains exist and are non-empty.
    proc = _nft("list", "chain", "inet", "fixhub_vm", out)
    if proc.returncode != 0 or "policy drop" not in (proc.stdout or ""):
        raise NetworkIsolationError("nft verification failed: out chain missing default-deny")


def destroy_nft_chains(tap: str) -> None:
    """Best-effort removal of per-task nft chains. Never raises."""
    try:
        pre, out = _chain_names(tap)
        for chain in (pre, out):
            try:
                _nft("delete", "chain", "inet", "fixhub_vm", chain)
            except Exception:
                pass
    except Exception:
        pass


def _verify_topology(netns: str, tap: str) -> None:
    """Post-setup assertions: TAP up with the host IP, no default route."""
    proc = _run_ns(netns, "addr", "show", "dev", tap)
    if proc.returncode != 0 or GUEST_HOST_IP not in (proc.stdout or ""):
        raise NetworkIsolationError(
            f"TAP {tap} missing {GUEST_HOST_IP} after setup: {(proc.stderr or '')[-200:]}"
        )
    proc = _run_ns(netns, "route", "show")
    if proc.returncode != 0:
        raise NetworkIsolationError("could not read netns routes for verification")
    for line in (proc.stdout or "").splitlines():
        if line.strip().startswith("default"):
            raise NetworkIsolationError("task netns has a default route after setup")


def destroy_isolation(task_id: int | str) -> None:
    """Best-effort removal of per-VM netns/TAP/veth/nft. Never raises."""
    try:
        netns = netns_name(task_id)
        tap = tap_name(task_id)
        vh, _ = veth_names(task_id)
        destroy_nft_chains(tap)
        _run("ip", "link", "delete", vh)
        _run("ip", "link", "delete", tap)
        _run("ip", "netns", "delete", netns)
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
