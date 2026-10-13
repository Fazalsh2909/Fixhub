# Phase 5 — Firecracker host setup (Linux/KVM only)

Firecracker does **not** run on Windows. All real-VM work happens on a
dedicated Linux/KVM host (bare metal preferred; EC2 `.metal` or GCE `n2` with
`--enable-nested-virtualization` otherwise). The Windows laptop keeps editing +
fast unit tests; `FIXHUB_FIRECRACKER_TEST=1` runs only on the KVM host/CI.

## 1. Host requirements (verified against upstream docs)

- Linux x86_64 (or aarch64 with matching guest), host kernel 5.10 / 6.1 / 6.18,
  KVM module loaded, `/dev/kvm` present and rw for the operator.
- `firecracker` + `jailer` binaries: same pinned release, musl static
  (default toolchain). Place at `/usr/local/bin/` (override via
  `FC_BINARY` / `JAILER_BINARY`).
- Packages: `iproute2`, `nft`, `acl`, `e2fsprogs`, `curl`, `python3`.
- Layout: `/srv/firecracker/{kernel,rootfs,jails}` — root-owned,
  `jails/` mode 0700 (jailer treats its inputs as trusted; parents must not be
  world-writable).
- Check: `tools/devtool checkenv` upstream, or
  `[ -r /dev/kvm ] && [ -w /dev/kvm ] && echo KVM-OK`.

## 2. Artifacts

- Kernel: uncompressed `vmlinux` for the host arch, with
  `CONFIG_VIRTIO_BLK, CONFIG_KVM_GUEST, CONFIG_ACPI, CONFIG_PCI`
  (+ `CONFIG_VIRTIO_MMIO_CMDLINE_DEVICES` serial). Firecracker CI release
  kernels are the recommended source. Path: `FC_KERNEL_IMAGE`.
- Rootfs: minimal ext4 with python3, git, ripgrep, ca-certificates, and
  `backend/app/sandbox/guest/agent.py` at `/opt/fixhub/agent.py`.
  Build: `sudo bash infra/firecracker/build-rootfs.sh`. Path: `FC_ROOTFS_IMAGE`.
- Record checksums (`*.sha256`) with each image build; production boots refuse
  when files are missing or empty.

## 3. Network (host-enforced, see docs/FIRECRACKER_NETWORK.md)

- Per-task netns `fixhub-t<id>` + TAP `ft<id>` + addressed `/31` veth link;
  per-task nft chains in `inet fixhub_vm` with interface-scoped rules and
  explicit catch-all drops (no base-chain drop policies — those would hit
  unrelated host traffic): prerouting DNATs TCP 443 to the proxy, the
  INPUT chain admits marked proxy + stub DNS only, the FORWARD chain
  drops metadata 169.254.169.254/32, RFC1918, loopback, direct DNS, IPv6.
- Host stub resolver + egress proxy own hostname policy
  (`FC_EGRESS_ALLOWLIST`). Guests have no direct route.

## 4. Windows dev workflow

1. Develop on the laptop with `SANDBOX_BACKEND=host` (default).
2. `pytest backend/tests/test_sandbox_backend.py` runs everywhere (no KVM).
3. Real-VM suite runs on the KVM host or KVM CI only (both flags, fail-closed
   — missing KVM/binaries/images/net isolation FAILS, never skips, never mocks):
   `FIXHUB_FIRECRACKER_TEST=1 FC_REQUIRE_KVM=1 python -m pytest backend/tests/test_sandbox_firecracker.py -q`.
4. Provisioning code under the `firecracker` backend fails closed on Windows
   with a clear message (never host fallback).

## 5. Privilege delegation (fixhub-netd) — required on the EC2 host

The API/worker runs as `User=fixhub` with `NoNewPrivileges=true` and cannot
create netns/TAP/veth devices, nftables rules, or spawn the jailer
(`sudo -u fixhub ip netns add …` fails on `mkdir /run/netns`). setuid
helpers, sudo rules, and file capabilities do NOT work under
`NoNewPrivileges` — the kernel blocks all of them. Delegation goes through
the root-owned `fixhub-netd` daemon instead (typed `{op, task_id}` requests
over `/run/fixhub-netd.sock`; interface names, IPs, routes, nft chains, and
jailer argv are derived server-side, never from the client).

It activates only when the API/worker environment sets
`FC_NETD_SOCKET=/run/fixhub-netd.sock` (and the process is not root).
Laptop dev and pytest (env unset) keep direct execution unchanged.

### 5.1 Install / upgrade (as root on the host, idempotent)

```bash
sudo bash infra/firecracker/provision-host.sh
# with a non-default checkout path:
sudo REPO_DIR=/srv/fixhub bash infra/firecracker/provision-host.sh
```

This installs `conntrack`, creates the `fixhub` user if missing, installs
`/usr/local/sbin/fixhub-netd` (root-owned, mode 0755 — writable only by
root), installs `fixhub-netd.service` + `fixhub-egress.service`, drops
`10-netd.conf` for the API/worker units (adds only
`FC_NETD_SOCKET=/run/fixhub-netd.sock` + ordering; keeps `User=fixhub` and
`NoNewPrivileges=true`), then enables and starts everything.

### 5.2 Restart FixHub

```bash
sudo systemctl restart fixhub-api fixhub-worker
sudo systemctl status fixhub-netd fixhub-egress fixhub-api fixhub-worker --no-pager
```

### 5.3 Verify (expected outputs)

```bash
id fixhub
ls -l /run/fixhub-netd.sock /usr/local/sbin/fixhub-netd
# socket: srwxrwx--- root fixhub | helper: -rwxr-xr-x root root
stat -c '%a %U %G' /usr/local/sbin/fixhub-netd  # 755 root root
sudo systemctl is-active fixhub-netd fixhub-egress  # active / active
sudo journalctl -u fixhub-netd -u fixhub-egress -n 50 --no-pager
# delegation reachable as the service user (no VM boot, no root):
sudo -u fixhub FC_NETD_SOCKET=/run/fixhub-netd.sock \
  python3 -c "from app.sandbox import netd_client as c; print(c.probe())"
# full isolation dry-run for one task id (creates + verifies, then remove):
sudo -u fixhub FC_NETD_SOCKET=/run/fixhub-netd.sock \
  python3 -c "from app.sandbox import net as n; print(n.ensure_isolation(91099))"
sudo nft list chain inet fixhub_vm out_ft91099 | tail -3  # ends in the catch-all drop
sudo -u fixhub FC_NETD_SOCKET=/run/fixhub-netd.sock \
  python3 -c "from app.sandbox import net as n; n.destroy_isolation(91099); print('torn down')"
# real sandbox task (KVM host only):
FIXHUB_FIRECRACKER_TEST=1 FC_REQUIRE_KVM=1 \
  python -m pytest backend/tests/test_sandbox_firecracker.py -q
```

### 5.4 Logs

```bash
sudo journalctl -u fixhub-api -u fixhub-worker -n 200 --no-pager
sudo journalctl -u fixhub-netd -n 200 --no-pager        # every op: op/task/peer/ok
sudo journalctl -u fixhub-egress -n 100 --no-pager
```

### 5.5 Rollback (removes delegation, restores direct-as-root operation)

```bash
sudo systemctl disable --now fixhub-netd.service fixhub-egress.service
sudo rm -f /usr/local/sbin/fixhub-netd \
  /etc/systemd/system/fixhub-netd.service \
  /etc/systemd/system/fixhub-egress.service \
  /etc/systemd/system/fixhub-api.service.d/10-netd.conf \
  /etc/systemd/system/fixhub-worker.service.d/10-netd.conf
sudo systemctl daemon-reload
# then unset FC_NETD_SOCKET and run the API/worker as root again (not recommended),
# or keep the host on direct execution only for manual debugging.
```
