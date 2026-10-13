#!/usr/bin/env bash
# Phase 5 M1: one-time host bootstrap for Firecracker (Ubuntu 24.04, x86_64, KVM).
# Must run as root ON the Linux/KVM host. Idempotent where practical.
set -euo pipefail
FC_VERSION="${FC_VERSION:-v1.16.1}"
ARCH="$(uname -m)"
echo "[host] arch=$ARCH fc=$FC_VERSION"
[ "$ARCH" = "x86_64" ] || [ "$ARCH" = "aarch64" ] || { echo "unsupported arch: $ARCH"; exit 1; }
[ -e /dev/kvm ] || { echo "FAIL: /dev/kvm missing (need KVM + bare metal or nested virt)"; exit 1; }
apt-get update
apt-get install -y iproute2 nftables conntrack acl e2fsprogs curl ca-certificates python3
# Binaries (pinned, checksummed by release process; verify manually on first install).
install -m 755 "firecracker-${FC_VERSION}-${ARCH}" /usr/local/bin/firecracker
install -m 755 "jailer-${FC_VERSION}-${ARCH}" /usr/local/bin/jailer
/usr/local/bin/firecracker --version
# Layout (root-owned, non-world-writable — jailer trust requirement).
mkdir -p /srv/firecracker/kernel /srv/firecracker/rootfs /srv/firecracker/jails
chmod 755 /srv/firecracker /srv/firecracker/kernel /srv/firecracker/rootfs
chmod 700 /srv/firecracker/jails
# Per-VM service user range note: jailer --uid/--gid uses 100000+task_id.
echo "[host] done. Next: fetch kernel (see docs/FIRECRACKER_HOST.md) + build-rootfs.sh"
echo "[host] check: [ -r /dev/kvm ] && [ -w /dev/kvm ] && echo KVM-OK || echo KVM-FAIL"

# --- Phase 5 privilege delegation (fixhub-netd + egress service) ---
# The API runs as User=fixhub with NoNewPrivileges=true and cannot create
# netns/TAP/nft rules or spawn the jailer. Those ops go through the
# root-owned fixhub-netd daemon over /run/fixhub-netd.sock (typed
# {op, task_id} requests only). setuid/sudo/file-caps do NOT work under
# NoNewPrivileges and must not be used. Idempotent: safe to re-run.
REPO_DIR="${REPO_DIR:-/opt/fixhub}"
echo "[netd] repo=$REPO_DIR"
id fixhub >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin fixhub
# The jail base must remain root-owned and non-writable, but the unprivileged
# API must be able to traverse it to stage files inside task-specific jail dirs.
# Mode 0700 blocks that traversal even when the task directory is chowned to fixhub.
chown root:fixhub /srv/firecracker/jails
chmod 750 /srv/firecracker/jails
# Daemon (root-owned, non-writable by fixhub — the privilege boundary).
install -o root -g root -m 755 "$REPO_DIR/infra/firecracker/fixhub-netd.py" /usr/local/sbin/fixhub-netd
# Units (repo paths are /opt/fixhub/...; rewrite when REPO_DIR differs).
for unit in fixhub-netd.service fixhub-egress.service; do
  sed "s|/opt/fixhub/backend|$REPO_DIR/backend|g" "$REPO_DIR/infra/systemd/$unit" \
    > "/etc/systemd/system/$unit"
  chmod 644 "/etc/systemd/system/$unit"
done
mkdir -p /etc/systemd/system/fixhub-api.service.d /etc/systemd/system/fixhub-worker.service.d
cp "$REPO_DIR/infra/systemd/fixhub-api.service.d-10-netd.conf" \
  /etc/systemd/system/fixhub-api.service.d/10-netd.conf
cp "$REPO_DIR/infra/systemd/fixhub-api.service.d-10-netd.conf" \
  /etc/systemd/system/fixhub-worker.service.d/10-netd.conf
chmod 644 /etc/systemd/system/fixhub-api.service.d/10-netd.conf \
  /etc/systemd/system/fixhub-worker.service.d/10-netd.conf
systemctl daemon-reload
systemctl enable --now fixhub-egress.service
systemctl enable --now fixhub-netd.service
# Verify: socket exists with root:fixhub 770, helper not writable by fixhub,
# both services active, delegation reachable as the fixhub user.
echo "[netd] verify:"
ls -l /run/fixhub-netd.sock /usr/local/sbin/fixhub-netd
[ ! -w /usr/local/sbin/fixhub-netd ] || echo "WARN: helper writable? check ownership"
sudo -u fixhub test -S /run/fixhub-netd.sock && echo "socket visible to fixhub: OK"
systemctl is-active fixhub-netd.service fixhub-egress.service
echo "[netd] done. Restart API/worker: systemctl restart fixhub-api fixhub-worker"
