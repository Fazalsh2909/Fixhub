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

- Per-task netns `fixhub-t<id>` + TAP `ftap<id>`; nft default-deny forward chain
  `inet fixhub_vm out` (established-only + TCP 443 to proxy; hard drops for
  metadata 169.254.169.254/32, RFC1918, loopback, direct DNS, all IPv6).
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
