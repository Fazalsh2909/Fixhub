#!/usr/bin/env bash
# Phase 5 M1: build the immutable base rootfs (ext4) for task microVMs.
# Run ON the Linux/KVM host (not the Windows laptop).
# Output: /srv/firecracker/rootfs/base.ext4 (+ sha256).
set -euo pipefail
OUT="${1:-/srv/firecracker/rootfs/base.ext4}"
SIZE_MB="${2:-2048}"
WORK="$(mktemp -d)"
echo "[rootfs] work=$WORK out=$OUT size=${SIZE_MB}M"
sudo mkdir -p "$(dirname "$OUT")"
# Minimal Debian trixie debootstrap (python3, git, rg, ca-certs for egress).
sudo apt-get update
sudo apt-get install -y debootstrap e2fsprogs python3
sudo debootstrap --variant=minbase trixie "$WORK/root" http://deb.debian.org/debian
sudo chroot "$WORK/root" apt-get install -y python3 git ripgrep ca-certificates
# Guest agent (stdlib only) + workspace dir.
sudo mkdir -p "$WORK/root/opt/fixhub" "$WORK/root/workspace"
sudo cp ../../backend/app/sandbox/guest/agent.py "$WORK/root/opt/fixhub/agent.py"
# Network: no DHCP client; kernel cmdline configures eth0 (see firecracker.py).
# DNS stub only (host resolver); no fallback nameservers baked in.
echo "nameserver 10.200.0.1" | sudo tee "$WORK/root/etc/resolv.conf" >/dev/null
# ext4 image.
dd if=/dev/zero of="$WORK/img.ext4" bs=1M count="$SIZE_MB"
mkfs.ext4 -q "$WORK/img.ext4"
sudo mkdir -p "$WORK/mnt"
sudo mount -o loop "$WORK/img.ext4" "$WORK/mnt"
sudo cp -a "$WORK/root/." "$WORK/mnt/"
sudo umount "$WORK/mnt"
sudo mv "$WORK/img.ext4" "$OUT"
sudo chmod 644 "$OUT"
sha256sum "$OUT" | sudo tee "$OUT.sha256"
echo "[rootfs] done: $OUT"
rm -rf "$WORK"
