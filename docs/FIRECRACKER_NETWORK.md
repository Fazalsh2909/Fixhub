# Phase 5 — Default-deny egress (host-enforced)

Upstream fact: Firecracker performs **no** network filtering — packets go from
the guest virtio-net straight to the TAP device. All policy below is enforced
on the host; environment variables and prompt instructions are not controls.

## Enforcement points (`backend/app/sandbox/net.py`)

1. **Service address on loopback**: `10.200.0.1/32` lives exactly once on
   `lo` (created idempotently, never torn down per-task). The egress proxy
   (`:8443`) and DNS stub (`:53`) bind it there. No per-task interface
   ever carries it, so concurrent VMs cannot duplicate it.
2. **Per-VM netns + TAP + unique guest subnet**: `fixhub-t<id>` /
   `ft<id>` with a dedicated `/30` from `10.201.0.0/16` (gateway on the
   TAP, guest via per-task kernel cmdline). Identical guest IPs across
   tasks would make host return routes ambiguous and conntrack tuples
   collide, so sharing is refused: a live-subnet collision fails boot.
   There is NO shared bridge and NO shared L2 at all. Interface names
   use full task ids (overlong names refuse instead of truncating into a
   collision). Created before boot; deleted on destroy.
3. **Unnumbered veth + static adjacency**: neither veth end has an IP.
   The netns holds a link route `10.200.0.1/32 dev vg` plus a permanent
   neigh entry for the service IP; the root ns holds a host route
   `<guest-net>/30 dev vh` plus a permanent neigh entry for the guest
   IP. No ARP floods cross tasks; no addresses to collide.
4. **nft backstop** (`inet fixhub_vm`, per-task chains, forward policy drop).
   nft runs in the default namespace, so rules match the **veth-host**
   device (`vh<id>`) — guest packets arrive there, never under the TAP
   name (the TAP exists only inside the task netns; matching it would
   silently match nothing):
   - NAT redirect: all TCP 443 arriving on the veth-host device is marked
     (`0x1`, proving it passed the redirect) and DNATed to the host
     proxy (`10.200.0.1:8443`);
   - accept established/related, plus marked packets toward the proxy
     (before the drops — post-DNAT dst is `10.200.0.1`, which would
     otherwise match the `10/8` drop; unmarked direct-to-proxy-port
     connections still fall through to the drops);
   - hard drop `169.254.169.254/32` (metadata), `10/8`, `172.16/12`,
     `192.168/16`, `127/8` on the forward path;
   - drop direct DNS (`udp/tcp dport 53`) — guests use the host stub
     (`10.200.0.1:53`) only;
   - drop all IPv6 in v1; drop residual TCP 443 (no bare accept anywhere).
3. **Egress proxy + stub resolver** (operator-provided, allowlist =
   `FC_EGRESS_ALLOWLIST`): all guest TCP 443 is routed via the host proxy,
   which allowlists SNI/hostnames (GitHub + package registries) and logs every
   connection. Direct `CONNECT` bypasses have no route (nft drops them).
4. **Rate limiters**: Firecracker API `rx/tx_rate_limiter` (10 MB/s, 1000 ops/s
   defaults) + jailer `--cgroup` caps + `tc qdisc` available for floods.

## What is allowed vs blocked (v1)

- Allowed (via proxy): `github.com`, `api.github.com`,
  `codeload.github.com`, `objects.githubusercontent.com`, `pypi.org`,
  `files.pythonhosted.org`, `registry.npmjs.org` (+ operator additions;
  LLM provider APIs are **host-only**, never guest-reachable).
- Blocked: metadata endpoint, private nets, host services, sibling-VM IPs,
  direct DNS (`8.8.8.8`), DNS tunnelling shapes (rate-capped + logged),
  unapproved hosts, all IPv6.

## Testing

Real-VM probes 6–7 in `test_sandbox_firecracker.py` assert forbidden targets
drop/timeout and approved `git ls-remote` + `pip download` succeed; probe
17 asserts sibling-VM addresses/ports are unreachable; probe 19 boots two
VMs at once and asserts distinct subnets, mutual approved egress, no
cross-talk, survivor-intact destroy, and idempotent recreation. nft
counters provide host-side evidence of drops. Rule-inspection unit tests
cover the veth-match and mark contracts, but packet filtering itself is
proven only by these real-guest runs on the KVM host.
