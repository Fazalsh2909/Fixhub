#!/usr/bin/env bash
# Build the immutable base rootfs (ext4) for FixHub Firecracker task VMs.
# Run ON the Linux/KVM host (not the Windows laptop).
# Output: /srv/firecracker/rootfs/base.ext4 (+ .sha256 sidecar).
#
# Incident fix (chroot build failure: "cannot create /dev/null: Permission
# denied" + "ca-certificates failed to preconfigure"): the previous version
# chrooted onto a bare debootstrap tree with NO /dev, /dev/pts, /proc or
# /sys mounted, so package postinsts had no /dev/null and debconf's
# preconfigure had no proc. It also copied the guest agent through a
# caller-cwd-relative `../../backend/...` path, ran its final cleanup as an
# unprivileged user over root-owned files (build trees silently left
# behind), and promoted the image with a cross-filesystem mv that could
# leave base.ext4 partial-corrupt on interruption.
#
# This version's contract (pinned by backend/tests/test_build_rootfs_script.py
# and exercisable end-to-end on a KVM host via ./test-build-rootfs.sh):
#   1. Works from ANY caller cwd (BASH_SOURCE-based repo root + realpath).
#   2. /dev (bind), /dev/pts (bind), /proc, /sys mounted in the chroot
#      BEFORE any apt-get; unwound in reverse order afterwards.
#   3. DEBIAN_FRONTEND=noninteractive for package installation.
#   4. /dev/null inside the chroot verified as a WRITABLE CHAR DEVICE
#      (1:3) before any package touches it.
#   5. Trap-based cleanup on EXIT/INT/TERM: reverse unmounts (lazy
#      fallback, loud error if anything stays mounted) + sudo rm -rf of
#      ALL temporary state — even mid-failure.
#   6. Host mounts are fully unmounted and asserted gone BEFORE the tree
#      is copied into the image; the pre-mount listing of chroot
#      dev/proc/sys/run is re-compared post-unmount so host device files
#      can never leak into the guest image.
#   7. The previous base.ext4 is replaced ATOMICALLY (same-fs rename of
#      .base.build-<pid>) only AFTER the new image passes e2fsck -f and a
#      read-only mount validation. A failed/interrupted build leaves the
#      previous image byte-identical.
#   8. The .sha256 sidecar is generated only after a successful promote
#      and is verified by recomputation.
# Guest image content unchanged: python3, git, ripgrep, ca-certificates,
# the FixHub guest agent, and the 10.200.0.1 DNS stub (host resolver).
#
# Test-only hooks (NEVER set in production): FIXHUB_ROOTFS_FAIL_AFTER=
# mount|verify|apt|copy makes the named stage abort deterministically —
# used by test-build-rootfs.sh to prove cleanup and previous-image
# preservation on every failure path.

set -euo pipefail

die() { echo "[rootfs] ERROR: $*" >&2; exit "${2:-1}"; }
note() { echo "[rootfs] $*"; }

# Deterministic abort point for the functional test harness (no-op unless
# the test-only env var is set).
_fail_after() {
    if [[ "${FIXHUB_ROOTFS_FAIL_AFTER:-}" == "$1" ]]; then
        echo "[rootfs] TEST-INJECTED FAILURE at stage=$1 (FIXHUB_ROOTFS_FAIL_AFTER)" >&2
        exit 1
    fi
}

# --- TEMP-DIAG breadcrumbs (Phase C removal candidate) ---------------------
# The forced stage=copy run died with rc=1 writing NOTHING after the
# chroot apt triggers — not even the cleanup trap's "[rootfs] FAILED"
# banner — so the last executed command was unlinkable from any output.
# Each breadcrumb names the command that runs NEXT: the last breadcrumb
# in a truncated log marks the dying command even when the process died
# with zero output. _DIAG_N increments per step and is echoed by the
# cleanup() exit record.
_diag() {
    _DIAG_N=$((_DIAG_N + 1))
    note "diag: step=${_DIAG_N} next: $*"
}

# --- Paths: always absolute, never caller-cwd relative --------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
AGENT_SRC="$REPO_ROOT/backend/app/sandbox/guest/agent.py"
[[ -f "$AGENT_SRC" ]] || die "guest agent source not found: $AGENT_SRC"
# Guard against re-baking the pre-#70/#71 guest agent: the manifest op must
# exist in the source we are about to bake (rootfs rebuild contract).
sudo grep -q '"manifest"' "$AGENT_SRC" \
    || die "$AGENT_SRC lacks the manifest op — stale checkout? (see docs/FIRECRACKER_HOST.md section 6)"

OUT_ARG="${1:-/srv/firecracker/rootfs/base.ext4}"
SIZE_MB="${2:-2048}"
[[ "$SIZE_MB" =~ ^[0-9]+$ ]] || die "SIZE_MB must be an integer, got: '$SIZE_MB'"
(( SIZE_MB >= 512 )) || die "SIZE_MB too small for minbase+toolchain: $SIZE_MB"

OUT="$(sudo realpath -m -- "$OUT_ARG")"
OUT_DIR="$(dirname -- "$OUT")"
sudo mkdir -p -- "$OUT_DIR"

# The image is assembled INSIDE the output directory and promoted by
# same-filesystem rename — a cross-fs mv is a copy that can leave the
# final image partial-corrupt if interrupted.
IMG="$OUT_DIR/.base.build-$$"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/fixhub-rootfs.XXXXXX")"
ROOT="$WORK/root"
MNT="$WORK/mnt"
mkdir -p "$ROOT" "$MNT"
note "work=$WORK out=$OUT size=${SIZE_MB}M img=$IMG"
# Traceability (keeper): every log answers whether the test-only fail
# hook actually reached this build — sudo's env_reset strips caller
# exports, so an unset value here is direct evidence the var never
# arrived (the harness must then pass it as an explicit `env` argument).
note "fail-hook: FIXHUB_ROOTFS_FAIL_AFTER='${FIXHUB_ROOTFS_FAIL_AFTER:-}'"

# --- Single-writer lock (best effort: a permission failure only warns) ----
LOCK="$OUT_DIR/.base.lock"
exec 9>"$LOCK" 2>/dev/null || echo "[rootfs] WARN: cannot open lock $LOCK; running unlocked" >&2
if [[ -e /proc/self/fd/9 ]]; then
    flock -n 9 || die "another rootfs build is in progress (lock: $LOCK)"
fi

# --- Cleanup contract: unwind mounts in REVERSE order, remove temp state --
_RC=0
_DIAG_N=0   # TEMP-DIAG breadcrumb counter, echoed by the cleanup exit record
MOUNTS=()

unmount_all() {
    local ret=0 i m
    for (( i=${#MOUNTS[@]}-1; i>=0; i-- )); do
        m="${MOUNTS[i]}"
        if mountpoint -q "$m" 2>/dev/null; then
            sudo umount "$m" 2>/dev/null \
                || { sleep 1; sudo umount "$m" 2>/dev/null; } \
                || { echo "[rootfs] busy mount, forcing lazy unmount: $m" >&2
                     sudo umount -l "$m" 2>/dev/null; } \
                || ret=1
        fi
    done
    return "$ret"
}

cleanup() {
    # Capture the natural exit status FIRST: nothing may run before this,
    # or $? is already clobbered. The old handler consulted _RC (always
    # initialized to 0) BEFORE the natural status, which downgraded every
    # failure to a silent exit 0.
    local rc=$?
    # The INT/TERM traps have recorded the signal status (130/143) in
    # _RC; a nonzero _RC is the more specific truth and overrides the
    # natural one. A zero _RC must never override a nonzero status.
    if (( ${_RC:-0} != 0 )); then
        rc="$_RC"
    fi
    trap - EXIT INT TERM
    set +e
    # Exit record (keeper): the trap's OWN resolved status — plus, while
    # the TEMP-DIAG breadcrumbs live, the last breadcrumb step — so a
    # trap that dies mid-unwind still tells the log what cleanup believed
    # the status to be (host-observed: the "[rootfs] FAILED" banner was
    # absent while the chroot mounts were cleanly unmounted).
    note "diag: exit-recorder rc=$rc _RC=${_RC:-0} step=${_DIAG_N:-0}"
    unmount_all || echo "[rootfs] WARN: could not unmount every mount cleanly" >&2
    local i
    for (( i=${#MOUNTS[@]}-1; i>=0; i-- )); do
        if mountpoint -q "${MOUNTS[i]}" 2>/dev/null; then
            echo "[rootfs] ERROR: mount still alive after cleanup: ${MOUNTS[i]}" >&2
        fi
    done
    sudo rm -rf -- "$WORK" "$IMG"
    if (( rc != 0 )); then
        echo "[rootfs] FAILED (exit $rc) — previous image untouched: $OUT" >&2
    fi
    exit "$rc"
}
trap cleanup EXIT
trap '_RC=130; cleanup' INT
trap '_RC=143; cleanup' TERM

[[ "$(uname -s)" == "Linux" ]] \
    || die "the rootfs build requires a Linux/KVM host (got: $(uname -s)); Firecracker does not run here"

# --- Host build dependencies (idempotent) ---------------------------------
sudo apt-get update
sudo apt-get install -y debootstrap e2fsprogs python3

# --- Minimal Debian trixie debootstrap -------------------------------------
sudo debootstrap --variant=minbase trixie "$ROOT" http://deb.debian.org/debian

# devpts needs its mount-point dir to exist inside the bare tree.
sudo mkdir -p -- "$ROOT/dev/pts" "$ROOT/proc" "$ROOT/sys" "$ROOT/run"

# --- Pseudofs leak baseline: snapshot BEFORE any host mount ----------------
# chroot dev/proc/sys/run contents before anything is mounted. After the
# mount cycle the same listing must come back IDENTICAL — debootstrap's own
# static device nodes (e.g. /dev/null, /dev/console) stay, and NOTHING from
# the host's live devfs (sda, pts entries, shm, ...) can end up in the copy.
_pseudo_list() {
    local base
    for base in dev proc sys run; do
        [[ -d "$ROOT/$base" ]] || continue
        sudo find "$ROOT/$base" -mindepth 1 -printf "$base %y %P\n" 2>/dev/null | sort
    done
}
_pseudo_list > "$WORK/pseudo.before"

# --- Mount the chroot filesystems (recorded for reverse unwinding) ---------
sudo mount --bind /dev     "$ROOT/dev";     MOUNTS+=("$ROOT/dev")
sudo mount --bind /dev/pts "$ROOT/dev/pts"; MOUNTS+=("$ROOT/dev/pts")
sudo mount -t proc  proc   "$ROOT/proc";    MOUNTS+=("$ROOT/proc")
sudo mount -t sysfs sysfs  "$ROOT/sys";     MOUNTS+=("$ROOT/sys")
_diag "before-hook=mount"
_fail_after mount

# --- Build-time DNS: the chroot apt uses the HOST resolver (devpkg networking
# runs on the host netns); the GUEST 10.200.0.1 stub is baked in afterwards.
sudo install -m 644 -- /etc/resolv.conf "$ROOT/etc/resolv.conf" \
    || die "cannot seed build-time DNS from /etc/resolv.conf"

# --- /dev/null gate: packages must find a writable character device -------
# This is the exact failure that killed the previous build: postinst
# redirects hit a missing (or non-device) /dev/null inside the chroot.
_dn_mode="$(sudo chroot "$ROOT" stat -c '%t %T' /dev/null 2>/dev/null || true)"
if [[ "$_dn_mode" != "1 3" ]]; then
    die "/dev/null inside the chroot is not the null character device (wanted major 1 minor 3, stat said: ${_dn_mode:-absent}) — did the /dev mount fail?"
fi
if ! sudo chroot "$ROOT" sh -c 'echo probe > /dev/null' 2>/dev/null; then
    die "/dev/null inside the chroot is not a writable character device for this identity"
fi
_diag "before-hook=verify"
_fail_after verify

# --- Packages (same set as before, now able to actually configure) --------
sudo chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true \
    apt-get update
sudo chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true \
    apt-get install -y --no-install-recommends python3 git ripgrep ca-certificates
_diag "before-hook=apt"
_fail_after apt

# --- Guest agent + workspace dir (explicit final mode, no temp-file fudge) -
# TEMP-DIAG breadcrumbs bracket each SILENT command in this pre-hook
# stretch: the last breadcrumb printed names the victim when the process
# dies without any error output.
_diag "install -d opt/fixhub + workspace"
sudo install -d -m 755 -- "$ROOT/opt/fixhub" "$ROOT/workspace"
_diag "install agent.py"
sudo install -m 644 -- "$AGENT_SRC" "$ROOT/opt/fixhub/agent.py"

# Guest runtime DNS (unchanged contract: kernel cmdline configures eth0;
# 10.200.0.1 is the host-resolver stub on the Firecracker tap subnet).
_diag "tee guest resolv.conf stub"
printf 'nameserver 10.200.0.1\n' | sudo tee "$ROOT/etc/resolv.conf" >/dev/null
_diag "chmod guest resolv.conf"
sudo chmod 644 "$ROOT/etc/resolv.conf"
_diag "stage=copy hook"
_fail_after copy
# TEMP-DIAG: reaching this breadcrumb proves the copy stopover fully passed.
_diag "next: unmount chroot before the image copy"

# --- Unmount EVERYTHING before the image copy ------------------------------
unmount_all || die "could not unmount the chroot filesystems cleanly"
for _m in "$ROOT/dev" "$ROOT/dev/pts" "$ROOT/proc" "$ROOT/sys"; do
    if mountpoint -q "$_m" 2>/dev/null; then
        die "mount still alive after unmount: $_m — the host fs must never reach the image"
    fi
done
_pseudo_list > "$WORK/pseudo.after"
if ! cmp -s "$WORK/pseudo.before" "$WORK/pseudo.after"; then
    diff "$WORK/pseudo.before" "$WORK/pseudo.after" | head -20 >&2 || true
    die "chroot dev/proc/sys/run listing changed across the mount cycle — host device files would leak into the image"
fi

# --- Assemble the ext4 image ----------------------------------------------
note "creating ext4 image (${SIZE_MB}M)"
sudo dd if=/dev/zero of="$IMG" bs=1M count="$SIZE_MB" status=none
sudo mkfs.ext4 -q -F "$IMG"
sudo mount -o loop "$IMG" "$MNT"; MOUNTS+=("$MNT")
sudo cp -a "$ROOT/." "$MNT/"
sudo sync
sudo umount "$MNT" 2>/dev/null \
    || { sleep 1; sudo umount "$MNT" 2>/dev/null; } \
    || { echo "[rootfs] busy image mount, forcing lazy unmount" >&2; sudo umount -l "$MNT" 2>/dev/null; }

# --- Validate the image BEFORE it may replace base.ext4 -------------------
sudo e2fsck -f -n "$IMG" > "$WORK/e2fsck.log" 2>&1 \
    || { tail -20 "$WORK/e2fsck.log" >&2; die "e2fsck rejected the new image"; }
sudo mount -o loop,ro "$IMG" "$MNT"; MOUNTS+=("$MNT")
[[ -s "$MNT/usr/bin/python3" ]] || die "image validation: python3 missing"
[[ -s "$MNT/usr/bin/git"     ]] || die "image validation: git missing"
[[ -s "$MNT/usr/bin/rg"      ]] || die "image validation: ripgrep missing"
[[ -s "$MNT/etc/ssl/certs/ca-certificates.crt" ]] \
    || die "image validation: CA certificate bundle missing"
sudo cmp -s -- "$AGENT_SRC" "$MNT/opt/fixhub/agent.py" \
    || die "image validation: baked agent.py differs from the repo source"
sudo grep -q '"manifest"' "$MNT/opt/fixhub/agent.py" \
    || die "image validation: baked agent lacks the manifest op"
sudo grep -q '^nameserver 10\.200\.0\.1$' "$MNT/etc/resolv.conf" \
    || die "image validation: guest DNS stub missing"
sudo umount "$MNT" 2>/dev/null \
    || { sleep 1; sudo umount "$MNT" 2>/dev/null; } \
    || { echo "[rootfs] busy validation mount, forcing lazy unmount" >&2; sudo umount -l "$MNT" 2>/dev/null; }
sudo sync

# --- Atomic promote (same-filesystem rename; previous image untouched) -----
sudo mv -f -- "$IMG" "$OUT"
sudo chmod 644 "$OUT"
note "promoted: $OUT (previous image replaced atomically)"

# --- Sidecar: only after a successful promote, then verified ---------------
sudo sha256sum "$OUT" | sudo tee "$OUT.sha256" >/dev/null
sudo chmod 644 "$OUT.sha256"
sudo sha256sum -c -- "$OUT.sha256" >/dev/null \
    || die "sha256 sidecar failed verification"
note "done: $OUT (+ $OUT.sha256)"
exit 0
