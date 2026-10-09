# Phase 5 — Default-deny egress (host-enforced)

Upstream fact: Firecracker performs **no** network filtering — packets go from
the guest virtio-net straight to the TAP device. All policy below is enforced
on the host; environment variables and prompt instructions are not controls.

## Enforcement points (`backend/app/sandbox/net.py`)

1. **Per-VM netns + TAP**: `fixhub-t<id>` / `ftap<id>`. No shared L2 across
   tenants beyond the filtered uplink. Created before boot; deleted on destroy.
2. **nft backstop** (`inet fixhub_vm out`, policy drop):
   - accept established/related only;
   - hard drop `169.254.169.254/32` (metadata), `10/8`, `172.16/12`,
     `192.168/16`, `127/8` on the forward path;
   - drop direct DNS (`udp/tcp dport 53`) — guests use the host stub
     (`10.200.0.1:53`) only;
   - drop all IPv6 in v1;
   - accept `tcp dport 443` toward the uplink **only because** the next layer
     filters hostnames (nft alone cannot do SNI policy).
3. **Egress proxy + stub resolver** (operator-provided, allowlist =
   `FC_EGRESS_ALLOWLIST`): all guest TCP 443 is routed via the host proxy,
   which allowlists SNI/hostnames (GitHub + package registries) and logs every
   connection. Direct `CONNECT` bypasses have no route (nft drops them).
4. **Rate limiters**: Firecracker API `rx/tx_rate_limiter` (10 MB/s, 1000 ops/s
   defaults) + jailer `--cgroup` caps + `tc qdisc` available for floods.

## What is allowed vs blocked (v1)

- Allowed (via proxy): `api.github.com`, `codeload.github.com`,
  `objects.githubusercontent.com`, `pypi.org`, `files.pythonhosted.org`,
  `registry.npmjs.org` (+ operator additions; LLM provider APIs are
  **host-only**, never guest-reachable).
- Blocked: metadata endpoint, private nets, host services, sibling-VM IPs,
  direct DNS (`8.8.8.8`), DNS tunnelling shapes (rate-capped + logged),
  unapproved hosts, all IPv6.

## Testing

Real-VM probes 6–7 in `test_sandbox_firecracker.py` assert forbidden targets
drop/timeout and approved `git ls-remote` + `pip download` succeed. nft
counters provide host-side evidence of drops.
