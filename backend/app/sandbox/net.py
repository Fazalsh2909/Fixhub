"""Phase 5: per-VM network isolation enforced on the HOST.

Design (upstream Firecracker guidance: the VMM performs NO filtering;
the host must):
- each task VM gets its own netns + TAP device (no shared L2 across
  tenants — there is NO shared bridge at all);
- each task gets its OWN guest link-net from 10.201.0.0/16 carved into
  /30s (gateway .+1 on the TAP, guest .+2 via kernel cmdline). Identical
  guest IPs across tasks would make host return routes ambiguous (ECMP
  splits replies) and conntrack tuples collide, so per-task subnets are
  mandatory for concurrency — never a shared guest net;
- the host service address 10.200.0.1/32 lives ONCE on loopback. The
  host egress proxy (TCP 8443) and DNS stub (UDP 53) bind it there
  (see egress.py). No per-task interface ever carries it, so duplicate
  IP assignments are impossible by construction;
- a veth pair links each netns to the root ns over a per-task /31 link
  subnet from 10.202.0.0/16 (RFC 3021, no broadcast consumed): the
  host-side end takes the even address, the netns-side end the odd one.
  Link subnets are separate from the guest /30 pool and deterministic
  per task, so concurrent VMs never share (or duplicate) link
  addresses. ARP resolves naturally inside each /31 (exactly two
  hosts); no proxy ARP, no floods cross tasks. Static permanent neigh
  entries pin the service IP and the guest IP both ways;
- the task netns holds a DEFAULT route via its own link address
  (`default via <link-host> dev <veth-guest>`). This route is transport
  only, NOT permission: it lets guest packets for public destinations
  travel TAP -> veth-guest -> veth-host (root ns), where nftables
  decides. Without it, approved egress could never leave the netns;
  with it, every packet still faces the hooks below;
- packet path: guest -> TAP (inside the task netns) -> veth-guest ->
  veth-host (root ns) -> prerouting DNAT (TCP 443 -> host proxy, marked)
  -> routing decision. DNAT to the LOCAL service address delivers to
  the INPUT hook, never forward — so policy lives on BOTH hooks:
  a per-task INPUT chain guards host-local delivery (marked proxy
  packets + stub DNS only, catch-all drop for any other guest
  access to host-local services), and the per-task FORWARD chain
  (default-deny) guards onward-routed traffic (direct DNS bypass,
  metadata, private nets, IPv6, residual 443). nftables rules match
  the VETH-HOST interface in the DEFAULT namespace — the TAP name
  exists only inside the task netns, so host-namespace rules matching
  the TAP would never see a packet. Hostname policy lives in the proxy
  via SNI sniffing — nft alone cannot do SNI policy, and the mark proves
  the packet actually passed the redirect instead of connecting to the
  proxy port directly;
- IPv4 forwarding is enabled in BOTH the default ns and the
  task netns (tap->veth-guest forwarding happens inside the netns);
- hard drops (both hooks): cloud metadata 169.254.169.254/32, RFC1918
  (covers sibling 10.201/16 subnets and the 10.202/16 link pool),
  loopback, direct DNS bypass (any port-53 not to the stub), all IPv6;
- per-task host route <guest-net>/30 dev <veth-host> carries
  conntracked return traffic; the loopback service address and other
  tasks' chains/routes are never touched by teardown.

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

# Host service address (egress proxy + DNS stub bind here). Lives ONCE as
# a /32 on loopback — never on a per-task interface, so concurrent VMs
# cannot duplicate it. Guest resolv.conf points here (rootfs-baked).
HOST_SVC_IP = "10.200.0.1"
# Per-task guest link-nets carved from 10.201.0.0/16 into /30s (16384
# subnets). The kernel cmdline in firecracker.py is templated per task
# from guest_addrs(); identical guest IPs across tasks would make host
# return routes ambiguous and conntrack tuples collide.
GUEST_NET_BASE_HI = 10
GUEST_NET_BASE_MID = 201
GUEST_PREFIX = 30
GUEST_SUBNETS = 16384
# Per-task veth link-nets carved from 10.202.0.0/16 into /31s (32768
# subnets, RFC 3021: both addresses usable, no broadcast). Separate pool
# from the guest /30s: the host-side veth takes the even address, the
# netns-side veth the odd one. Deterministic per task; ARP stays inside
# each point-to-point /31 (no proxy ARP anywhere).
LINK_NET_BASE_HI = 10
LINK_NET_BASE_MID = 202
LINK_PREFIX = 31
LINK_SUBNETS = 32768
# nft packet mark proving a packet passed the TCP-443 redirect (set in
# prerouting, checked in forward before the drops). Direct connections to
# the proxy port carry no mark and hit the drops instead.
REDIRECT_MARK = "0x1"


def guest_addrs(task_id: int | str) -> dict:
    """Per-task guest link-net addresses (pure, unit-testable).

    Returns {net, gw, vm, prefix}: TAP takes gw/prefix in the task netns,
    the guest takes vm via the kernel cmdline. Deterministic per task;
    collisions across live tasks are rejected by _assert_subnet_free().
    """
    idx = int(task_id) % GUEST_SUBNETS
    hi = (idx // 64) % 256
    lo = (idx % 64) * 4
    return {
        "net": f"{GUEST_NET_BASE_HI}.{GUEST_NET_BASE_MID}.{hi}.{lo}",
        "gw": f"{GUEST_NET_BASE_HI}.{GUEST_NET_BASE_MID}.{hi}.{lo + 1}",
        "vm": f"{GUEST_NET_BASE_HI}.{GUEST_NET_BASE_MID}.{hi}.{lo + 2}",
        "prefix": GUEST_PREFIX,
    }


def link_addrs(task_id: int | str) -> dict:
    """Per-task veth link-net addresses (pure, unit-testable).

    Returns {net, host, ns, prefix}: the root-ns veth end takes `host`,
    the netns veth end takes `ns`. The pool (10.202/16 /31s) never
    overlaps the guest pool (10.201/16 /30s); every task's pair is
    unique, so concurrent veths never duplicate an address.
    """
    idx = int(task_id) % LINK_SUBNETS
    off = idx * 2
    hi = off // 256
    lo = off % 256
    return {
        "net": f"{LINK_NET_BASE_HI}.{LINK_NET_BASE_MID}.{hi}.{lo}",
        "host": f"{LINK_NET_BASE_HI}.{LINK_NET_BASE_MID}.{hi}.{lo}",
        "ns": f"{LINK_NET_BASE_HI}.{LINK_NET_BASE_MID}.{hi}.{lo + 1}",
        "prefix": LINK_PREFIX,
    }


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


def _ifname(prefix: str, task_id: int | str) -> str:
    """Full task-id interface name. Truncated (%N) names would collide
    across tasks (1 vs 100001) and let one task steal another's device."""
    name = f"{prefix}{int(task_id)}"
    if len(name) > 15:
        raise NetworkIsolationError(
            f"interface name {name!r} exceeds the 15-char Linux limit; "
            "refusing (no truncated aliases)"
        )
    return name


def tap_name(task_id: int | str) -> str:
    return _ifname("ft", task_id)


def veth_names(task_id: int | str) -> tuple[str, str]:
    tid = int(task_id)
    return _ifname("vh", tid), _ifname("vg", tid)


def _ensure_forwarding(netns: str | None = None) -> None:
    """Enable IPv4 forwarding in the default ns and (when given) a task netns.

    Guest packets are forwarded tap -> veth-guest INSIDE the task netns, so
    the netns needs forwarding just like the root namespace does. Raises
    NetworkIsolationError when either one cannot be enabled (fail closed).
    """
    try:
        proc = _run("sysctl", "-w", "net.ipv4.ip_forward=1")
    except OSError as exc:
        raise NetworkIsolationError(f"sysctl unavailable for forwarding: {exc}")
    if proc.returncode != 0:
        raise NetworkIsolationError(
            f"could not enable IPv4 forwarding: {(proc.stderr or '')[-200:]}"
        )
    if netns:
        try:
            proc = _run("ip", "netns", "exec", netns,
                        "sysctl", "-w", "net.ipv4.ip_forward=1")
        except OSError as exc:
            raise NetworkIsolationError(
                f"sysctl unavailable inside netns {netns}: {exc}"
            )
        if proc.returncode != 0:
            raise NetworkIsolationError(
                f"could not enable IPv4 forwarding in {netns}: "
                f"{(proc.stderr or '')[-200:]}"
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
        task_id = int(task_id)
        netns = netns_name(task_id)
        tap = tap_name(task_id)
        vh, vg = veth_names(task_id)
        addrs = guest_addrs(task_id)
        link = link_addrs(task_id)
        proxy_port = _egress.proxy_port()
        svc_ip = _egress.proxy_addr()
        dns_ip = _egress.dns_addr()
        if svc_ip != HOST_SVC_IP or dns_ip != HOST_SVC_IP:
            raise NetworkIsolationError(
                "host services must live on "
                f"{HOST_SVC_IP} (proxy={svc_ip} dns={dns_ip}); refusing"
            )
        _ensure_loopback()
        _ensure_netns(netns)
        _ensure_forwarding(netns)
        _ensure_tap(netns, tap, addrs["gw"])
        _assert_subnet_free(task_id, addrs["net"])
        _ensure_veth(netns, vh, vg, addrs, link)
        # nft runs in the default namespace and MUST match the veth-host
        # interface (vh), which is the interface guest traffic actually
        # arrives on there. Matching the TAP name here would silently
        # match nothing (the TAP lives in the task netns).
        _apply_nft(tap=tap, proxy_port=proxy_port, iface=vh)
        proxy = _egress.ensure_available()
        _verify_topology(netns, tap, vh, vg, addrs, link)
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


def _ensure_loopback() -> None:
    """Host 10.200.0.1/32 on loopback (exactly once, shared by all tasks).

    Never removed by per-task teardown. Idempotent: an existing address is
    verified, not duplicated.
    """
    proc = _run("ip", "addr", "show", "dev", "lo")
    if proc.returncode != 0:
        raise NetworkIsolationError("could not read loopback addresses")
    # Exact /32 token match: a substring check would accept 10.200.0.10
    # (or any longer address with this prefix) as the service address.
    if f"{HOST_SVC_IP}/32" not in (proc.stdout or ""):
        proc = _run("ip", "addr", "add", f"{HOST_SVC_IP}/32", "dev", "lo")
        if proc.returncode != 0:
            raise NetworkIsolationError(
                f"loopback service address failed: {(proc.stderr or '')[-300:]}"
            )


def _mac_of(dev: str) -> str:
    """MAC address of a default-namespace device (for static neigh entries)."""
    proc = _run("ip", "-o", "link", "show", "dev", dev)
    if proc.returncode != 0:
        raise NetworkIsolationError(f"could not read MAC of {dev}")
    for token in (proc.stdout or "").split():
        if len(token) == 17 and token.count(":") == 5:
            return token.lower()
    raise NetworkIsolationError(f"no MAC found for {dev}")


def _mac_of_ns(netns: str, dev: str) -> str:
    proc = _run_ns(netns, "-o", "link", "show", "dev", dev)
    if proc.returncode != 0:
        raise NetworkIsolationError(f"could not read MAC of {dev} in {netns}")
    for token in (proc.stdout or "").split():
        if len(token) == 17 and token.count(":") == 5:
            return token.lower()
    raise NetworkIsolationError(f"no MAC found for {dev} in {netns}")


def _live_task_nets(exclude: int | None = None) -> dict[int, str]:
    """Map live task id -> guest subnet by inspecting existing task netns.

    Pure layout derivation (no packet inspection): a netns named
    fixhub-t<N> owns guest_addrs(N)["net"]. Used for collision detection.
    """
    found: dict[int, str] = {}
    try:
        proc = _run("ip", "netns", "list")
    except OSError as exc:
        raise NetworkIsolationError(f"could not list netns: {exc}")
    if proc.returncode != 0:
        raise NetworkIsolationError("could not list netns")
    for line in (proc.stdout or "").splitlines():
        name = line.split()[0] if line.split() else ""
        if not name.startswith("fixhub-t"):
            continue
        suffix = name.split("fixhub-t", 1)[1].split()[0]
        if not suffix.isdigit():
            continue
        tid = int(suffix)
        if exclude is not None and tid == exclude:
            continue
        try:
            found[tid] = guest_addrs(tid)["net"]
        except (TypeError, ValueError):
            continue
    return found


def _assert_subnet_free(task_id: int, net: str) -> None:
    """Fail closed when another live task netns owns the same guest subnet.

    Deterministic derivation makes collisions practically impossible, but
    two live tasks sharing a subnet would break return routing AND leak
    traffic across tenants — so verify, never assume.
    """
    for other_id, other_net in _live_task_nets(exclude=task_id).items():
        if other_net == net:
            raise NetworkIsolationError(
                f"guest subnet {net} already owned by live task {other_id}; "
                "refusing boot (would break return routing)"
            )


def _ensure_tap(netns: str, tap: str, gw_ip: str) -> None:
    proc = _run("ip", "tuntap", "add", "dev", tap, "mode", "tap")
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"tap create failed: {(proc.stderr or '')[-300:]}")
    proc = _run("ip", "link", "set", tap, "netns", netns)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        # Already in the netns from a previous run: verify instead.
        pass
    proc = _run_ns(netns, "addr", "add", f"{gw_ip}/{GUEST_PREFIX}", "dev", tap)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"tap addr failed: {(proc.stderr or '')[-300:]}")
    proc = _run_ns(netns, "link", "set", tap, "up")
    if proc.returncode != 0:
        raise NetworkIsolationError(f"tap up failed: {(proc.stderr or '')[-300:]}")


def _ensure_veth(
    netns: str, vh: str, vg: str, addrs: dict, link: dict,
) -> None:
    """Addressed veth pair (/31) + static adjacency + per-task routes.

    Each end carries exactly its own /31 address (unique per task — never
    duplicated across concurrent VMs):
    - netns side: <ns-addr>/31 on vg; service route HOST_SVC_IP/32 via the
      host link address + permanent neigh for the service IP (no ARP
      floods cross tasks); DEFAULT route via the host link address so
      guest traffic for public destinations can travel TAP -> vg -> vh.
      The default route is transport only: root-ns nft decides on both
      hooks (input for DNATed-to-local, forward for onward), so it
      grants no internet permission by itself;
    - root side: <host-addr>/31 on vh; host route <guest-net>/30 dev vh
      + permanent neigh for the guest IP (conntracked return traffic).
    `addr replace` (not `add`) heals stale addresses from older layouts.
    """
    net = f"{addrs['net']}/{addrs['prefix']}"
    vm_ip = addrs["vm"]
    host_ip, ns_ip = link["host"], link["ns"]
    prefix = link["prefix"]
    proc = _run("ip", "link", "add", vh, "type", "veth", "peer", "name", vg)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"veth create failed: {(proc.stderr or '')[-300:]}")
    proc = _run("ip", "addr", "replace", f"{host_ip}/{prefix}", "dev", vh)
    if proc.returncode != 0:
        raise NetworkIsolationError(f"veth host addr failed: {(proc.stderr or '')[-300:]}")
    proc = _run("ip", "link", "set", vh, "up")
    if proc.returncode != 0:
        raise NetworkIsolationError(f"veth host up failed: {(proc.stderr or '')[-300:]}")
    proc = _run("ip", "link", "set", vg, "netns", netns)
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        pass
    proc = _run_ns(netns, "addr", "replace", f"{ns_ip}/{prefix}", "dev", vg)
    if proc.returncode != 0:
        raise NetworkIsolationError(f"veth ns addr failed: {(proc.stderr or '')[-300:]}")
    proc = _run_ns(netns, "link", "set", vg, "up")
    if proc.returncode != 0:
        raise NetworkIsolationError(f"veth ns up failed: {(proc.stderr or '')[-300:]}")
    vh_mac = _mac_of(vh)
    vg_mac = _mac_of_ns(netns, vg)
    # Netns side: service address via the host link address + static ARP.
    proc = _run_ns(
        netns, "route", "replace", f"{HOST_SVC_IP}/32", "via", host_ip,
        "dev", vg,
    )
    if proc.returncode != 0:
        raise NetworkIsolationError(f"svc route failed: {(proc.stderr or '')[-300:]}")
    proc = _run_ns(
        netns, "neigh", "replace", HOST_SVC_IP, "lladdr", vh_mac,
        "dev", vg, "nud", "permanent",
    )
    if proc.returncode != 0:
        raise NetworkIsolationError(f"svc neigh failed: {(proc.stderr or '')[-300:]}")
    # Netns side: default via the host link address (transport to the root
    # ns for public destinations; nft on both root-ns hooks still decides).
    proc = _run_ns(netns, "route", "replace", "default", "via", host_ip, "dev", vg)
    if proc.returncode != 0:
        raise NetworkIsolationError(f"netns default route failed: {(proc.stderr or '')[-300:]}")
    # Root side: guest net via the host end + static ARP for the guest IP.
    proc = _run("ip", "route", "replace", net, "dev", vh)
    if proc.returncode != 0:
        raise NetworkIsolationError(
            f"guest route failed: {(proc.stderr or '')[-300:]}"
        )
    proc = _run(
        "ip", "neigh", "replace", vm_ip, "lladdr", vg_mac,
        "dev", vh, "nud", "permanent",
    )
    if proc.returncode != 0:
        raise NetworkIsolationError(
            f"guest neigh failed: {(proc.stderr or '')[-300:]}"
        )
    # Assert the netns default route is exactly the intended one (one
    # default, via our link address, on our device — never wide open).
    proc = _run_ns(netns, "route", "show")
    if proc.returncode != 0:
        raise NetworkIsolationError("could not read netns routes after setup")
    defaults = [
        line.strip()
        for line in (proc.stdout or "").splitlines()
        if line.strip().startswith("default")
    ]
    if len(defaults) != 1 or host_ip not in defaults[0] or vg not in defaults[0]:
        raise NetworkIsolationError(
            f"task netns default route is not the intended one: {defaults[:2]}"
        )


def _teardown_targets(task_id: int | str) -> dict:
    """Pure per-task teardown inventory (unit-testable scoping).

    Only this task's netns/TAP/veth/nft chains/host route/neigh entries —
    never shared state (loopback service address, other tasks' chains).
    """
    tid = int(task_id)
    addrs = guest_addrs(tid)
    vh, _vg = veth_names(tid)
    pre, out, in_chain = _chain_names(tap_name(tid))
    return {
        "netns": netns_name(tid),
        "tap": tap_name(tid),
        "vh": vh,
        "net": f"{addrs['net']}/{addrs['prefix']}",
        "vm_ip": addrs["vm"],
        "chains": (pre, out, in_chain),
    }


def _chain_names(tap: str) -> tuple[str, str, str]:
    # Per-task chains inside the shared table (multi-tenant safe: creating
    # task B never flushes task A's rules). `in_*` guards host-local
    # delivery (input hook); `out_*` guards onward routing (forward hook).
    safe = "".join(c if c.isalnum() else "_" for c in tap)
    return f"pre_{safe}", f"out_{safe}", f"in_{safe}"


def _require_iface(iface: str) -> None:
    if not iface:
        raise NetworkIsolationError(
            "nft match interface is required (veth-host device in the "
            "default namespace); refusing to emit rules that match nothing"
        )


def _nft_rules(*, tap: str, proxy_port: int, iface: str = "") -> list[str]:
    """Per-task prerouting + forward `add rule` lines (pure, unit-testable).

    `iface` is the interface these rules match in the namespace where nft
    runs (the default namespace): the VETH-HOST device. Chain names stay
    derived from `tap` (stable per-task id; creating task B never flushes
    task A's chains). Hostname allowlisting happens in the egress proxy
    (SNI sniffing); nft is the backstop that (a) redirects ALL guest TCP
    443 into the proxy and (b) drops every onward bypass: direct 443
    elsewhere, direct DNS, metadata, private nets, IPv6.

    NOTE: packets DNATed to the LOCAL service address traverse the INPUT
    hook, never forward — their accepts live in _nft_input_rules(). This
    forward chain carries no local-destination accepts by design (they
    would be dead rules mistaken for enforcement).
    """
    _require_iface(iface)
    pre, out, _in = _chain_names(tap)
    rules = [
        # NAT: every guest TCP/443 arriving on the veth-host device is
        # redirected to the host proxy AND marked. The mark proves the
        # packet passed the redirect: a guest connecting DIRECTLY to the
        # proxy port carries no mark and hits the drops (input catch-all).
        f'add rule inet fixhub_vm {pre} iifname "{iface}" tcp dport 443 '
        f"meta mark set {REDIRECT_MARK} "
        f"dnat to {HOST_SVC_IP}:{proxy_port}",
        f'add rule inet fixhub_vm {out} iifname "{iface}" '
        "ct state established,related accept",
    ]
    # Hard drops on the onward path (metadata, private nets incl. sibling
    # 10.201/16 subnets and the 10.202/16 link pool, loopback).
    for cidr in FORBIDDEN_CIDRS:
        rules.append(f'add rule inet fixhub_vm {out} iifname "{iface}" ip daddr {cidr} drop')
    # No local-destination accepts here: stub/proxy traffic is delivered
    # locally (input hook). Every other port-53 is a bypass attempt.
    rules.append(f'add rule inet fixhub_vm {out} iifname "{iface}" udp dport 53 drop')
    rules.append(f'add rule inet fixhub_vm {out} iifname "{iface}" tcp dport 53 drop')
    # IPv6 closed in v1.
    rules.append(f'add rule inet fixhub_vm {out} iifname "{iface}" meta l4proto ipv6-icmp drop')
    rules.append(f'add rule inet fixhub_vm {out} iifname "{iface}" ip6 daddr ::/0 drop')
    # Residual 443 that evaded the redirect is dropped; there is NO bare
    # `tcp dport 443 accept` and NO unmarked proxy-port accept anywhere.
    rules.append(f'add rule inet fixhub_vm {out} iifname "{iface}" tcp dport 443 drop')
    return rules


def _nft_input_rules(*, tap: str, proxy_port: int, iface: str = "") -> list[str]:
    """Per-task INPUT-hook `add rule` lines (pure, unit-testable).

    Prerouting DNAT rewrites guest TCP 443 to the LOCAL service address,
    so redirected packets are delivered locally and traverse INPUT, never
    FORWARD. This chain is where the intended proxy/DNS policy is
    enforced; it ends in an explicit catch-all drop so guest-originated
    traffic can reach NO other host-local service. Every rule is
    qualified by `iifname` (the task's veth-host device) so other hosts'
    and other tasks' input traffic is unaffected — the chain itself
    carries no `policy drop` for the same reason.
    """
    _require_iface(iface)
    _pre, _out, in_chain = _chain_names(tap)
    rules = [
        f'add rule inet fixhub_vm {in_chain} iifname "{iface}" '
        "ct state established,related accept",
        # Redirected packets are accepted toward the proxy BEFORE the
        # drops (their dst 10.200.0.1/32 would otherwise match the 10/8
        # drop — the mark is what distinguishes them from bypasses).
        # A direct connection to the proxy port carries no mark and falls
        # through to the drops + catch-all below.
        f'add rule inet fixhub_vm {in_chain} iifname "{iface}" '
        f"meta mark {REDIRECT_MARK} "
        f"ip daddr {HOST_SVC_IP} tcp dport {proxy_port} accept",
        # DNS only to the host stub.
        f'add rule inet fixhub_vm {in_chain} iifname "{iface}" udp dport 53 '
        f"ip daddr {HOST_SVC_IP} accept",
    ]
    # Same hard drops as the forward path (a local destination inside a
    # forbidden range is still forbidden).
    for cidr in FORBIDDEN_CIDRS:
        rules.append(
            f'add rule inet fixhub_vm {in_chain} iifname "{iface}" ip daddr {cidr} drop'
        )
    rules.append(f'add rule inet fixhub_vm {in_chain} iifname "{iface}" udp dport 53 drop')
    rules.append(f'add rule inet fixhub_vm {in_chain} iifname "{iface}" tcp dport 53 drop')
    rules.append(
        f'add rule inet fixhub_vm {in_chain} iifname "{iface}" meta l4proto ipv6-icmp drop'
    )
    rules.append(f'add rule inet fixhub_vm {in_chain} iifname "{iface}" ip6 daddr ::/0 drop')
    rules.append(f'add rule inet fixhub_vm {in_chain} iifname "{iface}" tcp dport 443 drop')
    # Catch-all LAST: guest traffic to any other host-local service drops.
    rules.append(f'add rule inet fixhub_vm {in_chain} iifname "{iface}" drop')
    return rules


def _nft(*args: str) -> subprocess.CompletedProcess:
    nft = shutil.which("nft")
    if not nft:
        raise NetworkIsolationError("nft not found (no fallback in v1)")
    return subprocess.run([nft, *args], capture_output=True, text=True, timeout=15)


def _chain_specs() -> tuple[tuple[str, str], tuple[str, str], tuple[str, str]]:
    """Chain kinds (pure, unit-testable).

    The forward chain is default-deny. The input chain carries NO policy:
    a base-chain drop policy on input would apply to ALL host input
    traffic — per-task scoping comes from the `iifname` qualifier on
    every rule plus the explicit catch-all drop instead.
    """
    return (
        ("pre", "type nat hook prerouting priority -100;"),
        ("out", "type filter hook forward priority 0; policy drop;"),
        ("in", "type filter hook input priority 0;"),
    )


def _apply_nft(*, tap: str, proxy_port: int, iface: str) -> None:
    # Idempotent: create table/chains (exists is fine), flush ONLY our
    # per-task chains (never another task's), then add exactly the rules
    # from _nft_rules() (prerouting + forward) and _nft_input_rules()
    # (input). `iface` is the veth-host device visible here.
    if not iface:
        raise NetworkIsolationError("nft match interface is required; refusing")
    pre, out, in_chain = _chain_names(tap)
    proc = _nft("add", "table", "inet", "fixhub_vm")
    if proc.returncode != 0 and "exists" not in (proc.stderr or "").lower():
        raise NetworkIsolationError(f"nft table create failed: {(proc.stderr or '')[-300:]}")
    kinds = dict(_chain_specs())
    for chain, kind in ((pre, "pre"), (out, "out"), (in_chain, "in")):
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
    for rule in _nft_rules(tap=tap, proxy_port=proxy_port, iface=iface):
        proc = _nft(*rule.split(" "))
        if proc.returncode != 0:
            raise NetworkIsolationError(f"nft rule add failed: {(proc.stderr or '')[-300:]}")
    for rule in _nft_input_rules(tap=tap, proxy_port=proxy_port, iface=iface):
        proc = _nft(*rule.split(" "))
        if proc.returncode != 0:
            raise NetworkIsolationError(f"nft input rule add failed: {(proc.stderr or '')[-300:]}")
    # Verify forward chain: default-deny and matches the veth-host iface.
    proc = _nft("list", "chain", "inet", "fixhub_vm", out)
    body = proc.stdout or ""
    if proc.returncode != 0 or "policy drop" not in body:
        raise NetworkIsolationError("nft verification failed: out chain missing default-deny")
    if f'iifname "{iface}"' not in body:
        raise NetworkIsolationError(
            f"nft verification failed: out chain does not match {iface}"
        )
    # Verify input chain: marked proxy accept BEFORE the drops plus the
    # catch-all drop (a missing mark rule would silently drop ALL
    # approved egress after the redirect; a missing catch-all would
    # leave arbitrary host-local services reachable from the guest).
    proc = _nft("list", "chain", "inet", "fixhub_vm", in_chain)
    in_body = proc.stdout or ""
    if proc.returncode != 0:
        raise NetworkIsolationError("nft verification failed: in chain missing")
    if f"meta mark {REDIRECT_MARK}" not in in_body:
        raise NetworkIsolationError(
            "nft verification failed: input marked redirect accept missing"
        )
    if f'iifname "{iface}" drop' not in in_body:
        raise NetworkIsolationError(
            "nft verification failed: input catch-all drop missing"
        )
    if "policy drop" in in_body:
        raise NetworkIsolationError(
            "nft verification failed: input chain must not carry a drop policy"
        )
    proc = _nft("list", "chain", "inet", "fixhub_vm", pre)
    pre_body = proc.stdout or ""
    if proc.returncode != 0 or "dnat to" not in pre_body:
        raise NetworkIsolationError("nft verification failed: redirect chain broken")


def destroy_nft_chains(tap: str) -> None:
    """Best-effort removal of per-task nft chains. Never raises."""
    try:
        for chain in _chain_names(tap):
            try:
                _nft("delete", "chain", "inet", "fixhub_vm", chain)
            except Exception:
                pass
    except Exception:
        pass


def _verify_topology(
    netns: str, tap: str, vh: str, vg: str, addrs: dict, link: dict,
) -> None:
    """Post-setup assertions for the full packet path.

    - TAP up with the task gateway IP inside the task netns;
    - loopback carries the service IP (exactly once, shared);
    - each veth end carries EXACTLY its own /31 link address (a missing
      address breaks the path; any extra address is a duplicate-IP
      regression across concurrent VMs);
    - static neigh entries + per-task host route present;
    - forwarding on in BOTH namespaces;
    - the task netns default route is exactly `default via <link-host>
      dev <vg>` (transport to the root ns for public destinations —
      permission still comes only from the input/forward hooks).
    """
    gw_ip, vm_ip = addrs["gw"], addrs["vm"]
    net = f"{addrs['net']}/{addrs['prefix']}"
    host_ip, ns_ip = link["host"], link["ns"]
    prefix = link["prefix"]
    proc = _run_ns(netns, "addr", "show", "dev", tap)
    if proc.returncode != 0 or gw_ip not in (proc.stdout or ""):
        raise NetworkIsolationError(
            f"TAP {tap} missing {gw_ip} after setup: {(proc.stderr or '')[-200:]}"
        )
    proc = _run("ip", "addr", "show", "dev", "lo")
    if proc.returncode != 0 or f"{HOST_SVC_IP}/32" not in (proc.stdout or ""):
        raise NetworkIsolationError("loopback missing the service address")
    proc = _run("ip", "-o", "addr", "show", "dev", vh)
    if proc.returncode != 0:
        raise NetworkIsolationError(f"could not read addresses of {vh}")
    if f"inet {host_ip}/{prefix} " not in ((proc.stdout or "") + " "):
        raise NetworkIsolationError(
            f"{vh} missing its /31 link address {host_ip}/{prefix}"
        )
    if (proc.stdout or "").count("inet ") != 1:
        raise NetworkIsolationError(
            f"{vh} carries extra addresses (must hold only its /31)"
        )
    proc = _run_ns(netns, "-o", "addr", "show", "dev", vg)
    if proc.returncode != 0:
        raise NetworkIsolationError(f"could not read addresses of {vg} in {netns}")
    if f"inet {ns_ip}/{prefix} " not in ((proc.stdout or "") + " "):
        raise NetworkIsolationError(
            f"{vg} missing its /31 link address {ns_ip}/{prefix} in {netns}"
        )
    if (proc.stdout or "").count("inet ") != 1:
        raise NetworkIsolationError(
            f"{vg} carries extra addresses in {netns} (must hold only its /31)"
        )
    proc = _run("ip", "neigh", "show", vm_ip, "dev", vh)
    if proc.returncode != 0 or "PERMANENT" not in (proc.stdout or "").upper():
        raise NetworkIsolationError(f"static neigh for {vm_ip} missing on {vh}")
    proc = _run_ns(netns, "neigh", "show", HOST_SVC_IP, "dev", vg)
    if proc.returncode != 0 or "PERMANENT" not in (proc.stdout or "").upper():
        raise NetworkIsolationError(
            f"static neigh for {HOST_SVC_IP} missing on {vg} in {netns}"
        )
    proc = _run("ip", "route", "show", net, "dev", vh)
    if proc.returncode != 0 or vh not in (proc.stdout or ""):
        raise NetworkIsolationError(f"host route {net} via {vh} missing")
    for where, cmd in (
        ("default namespace", ("sysctl", "-n", "net.ipv4.ip_forward")),
        (f"netns {netns}", ("ip", "netns", "exec", netns,
                             "sysctl", "-n", "net.ipv4.ip_forward")),
    ):
        try:
            proc = _run(*cmd)
        except OSError as exc:
            raise NetworkIsolationError(f"forwarding unreadable ({where}): {exc}")
        if proc.returncode != 0 or (proc.stdout or "").strip() != "1":
            raise NetworkIsolationError(
                f"IPv4 forwarding off ({where}); refusing boot"
            )
    proc = _run_ns(netns, "route", "show")
    if proc.returncode != 0:
        raise NetworkIsolationError("could not read netns routes for verification")
    defaults = [
        line.strip()
        for line in (proc.stdout or "").splitlines()
        if line.strip().startswith("default")
    ]
    if len(defaults) != 1 or host_ip not in defaults[0] or vg not in defaults[0]:
        raise NetworkIsolationError(
            f"task netns default route is not the intended one: {defaults[:2]}"
        )


def destroy_isolation(task_id: int | str) -> None:
    """Best-effort removal of per-VM netns/TAP/veth/nft/route. Never raises.

    Scope is strictly per-task (see _teardown_targets): the loopback
    service address and other tasks' chains/routes are never touched.
    """
    try:
        targets = _teardown_targets(task_id)
        destroy_nft_chains(targets["tap"])
        _run("ip", "route", "del", targets["net"], "dev", targets["vh"])
        _run("ip", "neigh", "del", targets["vm_ip"], "dev", targets["vh"])
        _run("ip", "link", "delete", targets["vh"])
        _run("ip", "link", "delete", targets["tap"])
        _run("ip", "netns", "delete", targets["netns"])
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
