# Phase 5 — Default-deny egress (host-enforced)

Upstream fact: Firecracker performs **no** network filtering — packets go from
the guest virtio-net straight to the TAP device. All policy below is enforced
on the host; environment variables and prompt instructions are not controls.

## Enforcement points (`backend/app/sandbox/net.py`)

1. **Per-VM netns + TAP**: `fixhub-t<id>` / `ftap<id>`. No shared L2 across
   tenants beyond the filtered uplink. Created before boot; deleted on destroy.
   A veth pair links each netns to the root ns (root `10.200.0.1` <->
   netns `10.200.0.2`); forwarding is enabled in BOTH namespaces.
2. **nft backstop** (`inet fixhub_vm`, per-task chains, forward policy drop).
   nft runs in the default namespace, so rules match the **veth-host**
   device (`vh<id>`) — guest packets arrive there, never under the TAP
   name (the TAP exists only inside the task netns; matching it would
   silently match nothing):
   - NAT redirect: all TCP 443 arriving on the veth-host device is DNATed
     to the host proxy (`10.200.0.1:8443`);
   - accept established/related only;
   - hard drop `169.254.169.254/32` (metadata), `10/8`, `172.16/12`,
     `192.168/16`, `127/8` on the forward path;
   - drop direct DNS (`udp/tcp dport 53`) — guests use the host stub
     (`10.200.0.1:53`) only;
   - drop all IPv6 in v1;
   - accept TCP toward the proxy port only (there is NO bare
     `tcp dport 443 accept`); hostname policy lives in the proxy via SNI
     sniffing (nft alone cannot do SNI policy).
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
17 asserts sibling-VM addresses/ports are unreachable. nft counters provide
host-side evidence of drops. Rule-inspection unit tests cover the
veth-match contract, but packet filtering itself is proven only by these
real-guest runs on the KVM host.
