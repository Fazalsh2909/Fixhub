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
3. **Addressed veth over per-task /31 link subnets**: each pair gets a
   unique `/31` from `10.202.0.0/16` (RFC 3021, separate pool from the
   guest `/30`s) — host-side end takes the even address, netns-side end
   the odd one. ARP resolves naturally inside each point-to-point `/31`
   (exactly two hosts; no proxy ARP, no floods cross tasks). The netns
   holds link-scoped `10.200.0.1/32 dev vg` — so the permanent neigh
   entry for the service IP is its actual resolution (a `via` form would
   resolve the next hop instead and leave that entry decorative) — plus
   a second permanent neigh entry for the `/31` host peer (the default
   route's real next hop). No dynamic neighbor resolution happens in
   the netns. The root ns holds a host route `<guest-net>/30 dev vh`
   plus a permanent neigh entry for the guest IP. `addr replace` heals
   stale addresses from older layouts, and verification requires each
   end to hold exactly its own `/31` plus both permanent entries.
4. **Netns default route (transport, not permission)**: the task netns
   holds exactly one default route, `default via <link-host> dev vg`,
   so guest traffic for public destinations (guest default via the TAP
   gateway, from the kernel cmdline) can travel TAP → veth-guest →
   veth-host. The route grants no internet access by itself: every
   packet still faces the root-ns hooks below, and setup refuses boot
   unless the default is exactly the intended one.
5. **nft on the hooks packets actually traverse** (`inet fixhub_vm`,
   per-task chains `pre_*`/`out_*`/`in_*`). nft runs in the default
   namespace, so rules match the **veth-host** device (`vh<id>`) —
   guest packets arrive there, never under the TAP name (the TAP exists
   only inside the task netns; matching it would silently match
   nothing). Prerouting DNAT to the *local* service address delivers
   to the INPUT hook, never forward — so enforcement is split where
   the packets go:
   - NAT redirect (`pre_*`): all TCP 443 arriving on the veth-host
     device is marked (`0x1`, proving it passed the redirect) and
     DNATed to the host proxy (`10.200.0.1:8443`);
   - INPUT chain (`in_*`, no drop policy — a base-chain policy would hit
     all host input; every rule is `iifname`-qualified instead): accept
     only flows proven to be the approved proxy connection — persistent
     conntrack proof (`ct status dnat`) or stateless per-packet proof
     (`meta mark`, re-applied in prerouting to every guest→proxy packet
     since the guest socket stays bound to public-IP:443) — both bound
     to the exact proxy dst+port and placed before the drops (post-DNAT
     dst is `10.200.0.1`, which would otherwise match the `10/8` drop;
     unmarked direct-to-proxy-port connections still fall through to the
     drops), plus DNS to the host stub — then the same hard drops, ending
     in an explicit catch-all drop so guest traffic reaches NO other
     host-local service. No generic `established` accept: a stale entry
     authorizes nothing;
   - FORWARD chain (`out_*`, no drop policy — a base-chain policy applies
     to the whole hook and would drop unrelated host-forwarded traffic;
     per-task default-deny comes from the explicit interface-scoped
     catch-all instead): zero accepts, then hard drops —
     `169.254.169.254/32` (metadata), `10/8` (covers sibling `10.201/16`
     subnets and the `10.202/16` link pool), `172.16/12`, `192.168/16`,
     `127/8`; drop direct DNS (`udp/tcp dport 53`) — guests use the host
     stub only; drop all IPv6 in v1; drop residual TCP 443. Nothing is
     legitimately forwarded (443/DNS go local via DNAT; proxy/stub
     replies are locally generated and traverse OUTPUT, never these
     chains), so no state exception exists for a stale entry to exploit.
   - No generic `established,related` accept on either hook by design.
     Stale-conntrack hygiene (`conntrack -D` for the guest IP on setup
     and teardown) remains, but only to avoid dead-tuple collisions for
     legitimate NEW flows — the boundary does not depend on it. Cleanup
     reports success/failure (missing tool or nonzero exit logs a warning)
     and never blocks boot: the rules above are secure with or without
     stale entries, and the firewall is never weakened to accommodate a
     missing tool.
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
VMs at once and asserts distinct guest + link subnets, guest default
routes, host-side `/31` addresses, mutual approved egress, no
cross-talk, survivor-intact destroy (forward + input chains), and
idempotent recreation. nft counters provide host-side evidence of drops.
Rule-inspection unit tests cover the veth-match, mark-order, catch-all,
no-policy, and no-bare-established contracts, but packet filtering itself
is proven only by these real-guest runs on the KVM host — a mocked test
never counts as filtering evidence.
