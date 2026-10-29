#!/usr/bin/env bash
# End-to-end validation of build-rootfs.sh — run as root on the Linux/KVM
# host (sudo bash TEST-BUILD-ROOTFS.SH). This is the REAL functional test
# the CI cannot run (no KVM/no debootstrap in CI).
#
# Scenarios:
#   cleanup   — force-fail mid-build proves: non-zero exit, zero leftover
#               mounts, zero leftover work trees/images/sidecars
#   preserve  — a failed build leaves the previous image byte-identical
#               (and sidecarless); a following success atomically replaces
#               both image and sidecar
#   success   — a real build: image passes e2fsck, validates on a
#               read-only mount (agent incl. manifest op, python3/git/rg,
#               CA bundle, DNS stub) and the sidecar verifies
#
# The live /srv/firecracker/rootfs/base.ext4 is NEVER touched: the builder
# is invoked against scratch paths only. Failure injection uses the
# builder's test-only FIXHUB_ROOTFS_FAIL_AFTER hooks. A full run takes
# ~5–10 minutes (debootstrap + chroot apt per build).
#
# Usage: sudo bash infra/firecracker/test-build-rootfs.sh [cleanup|preserve|success|all]

set -u  # NOT -e: scenario rc values are asserted explicitly

die() { echo "[rootfs-test] ERROR: $*" >&2; exit 2; }
note() { echo "[rootfs-test] $*"; }
ok() { echo "  ok: $*"; }

if [[ "$(id -u)" -ne 0 ]]; then
    die "run as root: sudo bash infra/firecracker/test-build-rootfs.sh"
fi
if [[ "$(uname -s)" != "Linux" ]]; then
    die "requires a Linux/KVM host (got: $(uname -s))"
fi

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$ROOT_DIR/../.." && pwd -P)"
BUILDER="$ROOT_DIR/build-rootfs.sh"
[[ -x "$BUILDER" ]] || die "missing $BUILDER (or not executable)"

TOTAL_FAIL=0

# Scratch area on the same filesystem layout the builder expects
# (loop devices + root-owned files). Never the production path.
SCRATCH="$(mktemp -d /srv/firecracker/rootfs/.test-build.XXXXXX 2>/dev/null)" \
    || SCRATCH="$(mktemp -d /var/tmp/fixhub-rootfs-test.XXXXXX)"
export SCRATCH
trap 'sudo rm -rf -- "$SCRATCH"' EXIT INT TERM

# Builder logs land here; every scenario asserts on its content below.
B_LOG="$SCRATCH/builder.log"

fail() { TOTAL_FAIL=$((TOTAL_FAIL + 1)); echo "  FAIL: $*" >&2; }

no_leftover_mounts() {
    if grep -E 'fixhub-rootfs|\.test-build\.' /proc/mounts >/dev/null 2>&1; then
        grep -E 'fixhub-rootfs|\.test-build\.' /proc/mounts >&2
        fail "leftover mount(s) after failed build (see above)"
    else
        ok "no leftover mounts"
    fi
}

no_leftover_work_dirs() {
    local n
    n="$(compgen -G '/tmp/fixhub-rootfs.*' | wc -l)"
    n=$((n + $(compgen -G '/var/tmp/fixhub-rootfs.*' | wc -l)))
    if (( n > 0 )); then
        fail "leftover work tree(s): $n (build-rootfs.sh cleanup failed)"
    else
        ok "no leftover work trees"
    fi
}

# --------------------------------------------------------------------------
scenario_cleanup() {
    note "scenario: cleanup (forced failure at mount stage)"
    local out="$SCRATCH/base-cleanup.ext4" rc=0
    sudo env FIXHUB_ROOTFS_FAIL_AFTER=mount \
        bash "$BUILDER" "$out" 1024 > "$B_LOG" 2>&1 || rc=$?
    if (( rc == 0 )); then
        fail "forced-failure build unexpectedly succeeded"
    else
        ok "forced failure exits non-zero (rc=$rc)"
    fi
    [[ -e "$out" ]] && fail "output image exists after a failed build: $out" \
        || ok "no output image written"
    [[ -e "$out.sha256" ]] && fail "$out.sha256 written by a failed build" \
        || ok "no sidecar written"
    # no partial staging image left in the output dir
    if compgen -G "$SCRATCH/.base.build-*" >/dev/null; then
        fail "leftover staging image: $(compgen -G "$SCRATCH/.base.build-*")"
    else
        ok "no leftover staging image"
    fi
    no_leftover_mounts
    no_leftover_work_dirs
    if grep -q "TEST-INJECTED FAILURE at stage=mount" "$B_LOG"; then
        ok "builder reports the injected stage"
    else
        fail "builder did not report the injected failure stage"
    fi
    return 0
}

# --------------------------------------------------------------------------
scenario_preserve() {
    note "scenario: preserve (previous image survives failure, then is replaced)"
    local out="$SCRATCH/base-preserve.ext4" rc=0
    # a known "previous" image (random bytes — never a real fs)
    sudo dd if=/dev/urandom of="$out" bs=1K count=64 status=none
    local pre_sha
    pre_sha="$(sudo sha256sum "$out" | cut -d' ' -f1)"

    sudo env FIXHUB_ROOTFS_FAIL_AFTER=copy \
        bash "$BUILDER" "$out" 1024 > "$B_LOG" 2>&1 || rc=$?
    if (( rc == 0 )); then fail "forced-failure build unexpectedly succeeded"; else ok "forced failure exits non-zero (rc=$rc)"; fi
    local post_sha
    post_sha="$(sudo sha256sum "$out" | cut -d' ' -f1)"
    if [[ "$post_sha" == "$pre_sha" ]]; then
        ok "previous image preserved byte-identical after failure"
    else
        fail "previous image was MODIFIED by a failed build"
    fi
    [[ -e "$out.sha256" ]] && fail "sidecar appeared without a successful build" \
        || ok "no sidecar before success"
    no_leftover_mounts
    no_leftover_work_dirs

    # success pass: the very same path must now be atomically replaced
    rc=0
    sudo bash "$BUILDER" "$out" 1024 > "$B_LOG" 2>&1 || rc=$?
    if (( rc == 0 )); then
        ok "successful build replaces the preserved image (rc=0)"
    else
        fail "successful build failed (rc=$rc)"; tail -30 "$B_LOG" >&2; return 0
    fi
    post_sha="$(sudo sha256sum "$out" | cut -d' ' -f1)"
    [[ "$post_sha" != "$pre_sha" ]] && ok "image content replaced" \
        || fail "successful build did not replace the image"
    if (cd "$SCRATCH" && sudo sha256sum -c -- "base-preserve.ext4.sha256" >/dev/null 2>&1); then
        ok "sidecar validates against the promoted image"
    else
        fail "sidecar does not match the promoted image"
    fi
    return 0
}

# --------------------------------------------------------------------------
scenario_success() {
    note "scenario: success (real build, full validation battery)"
    local out="$SCRATCH/base-success.ext4" rc=0
    sudo bash "$BUILDER" "$out" 1024 > "$B_LOG" 2>&1 || rc=$?
    if (( rc != 0 )); then
        fail "real build failed (rc=$rc)"; tail -40 "$B_LOG" >&2; return 0
    fi
    ok "builder exit 0"
    [[ -s "$out" ]] && ok "image present ($(du -m "$out" | cut -f1) MB)" \
        || fail "image missing/empty"
    if [[ -f "$out.sha256" ]]; then
        if (cd "$SCRATCH" && sudo sha256sum -c -- "base-success.ext4.sha256" >/dev/null 2>&1); then
            ok "sidecar verifies"
        else
            fail "sidecar checksum mismatch"
        fi
    else
        fail "sidecar missing"
    fi
    # filesystem integrity, independently of the builder
    if sudo e2fsck -f -n "$out" >/dev/null 2>&1; then
        ok "independent e2fsck -f -n clean"
    else
        fail "independent e2fsck rejected the image"
    fi
    # content: read-only mount + the same battery the builder claims
    local mnt="$SCRATCH/vmnt"
    sudo mkdir -p "$mnt"
    if sudo mount -o loop,ro "$out" "$mnt" 2>/dev/null; then
        [[ -s "$mnt/usr/bin/python3" ]] && ok "python3 present" || fail "python3 missing"
        [[ -s "$mnt/usr/bin/git" ]] && ok "git present" || fail "git missing"
        [[ -s "$mnt/usr/bin/rg" ]] && ok "ripgrep present" || fail "ripgrep missing"
        [[ -s "$mnt/etc/ssl/certs/ca-certificates.crt" ]] && ok "CA bundle present" || fail "CA bundle missing"
        if sudo cmp -s -- "$REPO_ROOT/backend/app/sandbox/guest/agent.py" "$mnt/opt/fixhub/agent.py"; then
            ok "agent.py byte-identical to repo source"
        else
            fail "agent.py differs from repo source"
        fi
        if sudo grep -q '"manifest"' "$mnt/opt/fixhub/agent.py"; then
            ok "baked agent supports the manifest op"
        else
            fail "baked agent lacks manifest op"
        fi
        if sudo grep -q '^nameserver 10\.200\.0\.1$' "$mnt/etc/resolv.conf"; then
            ok "guest DNS stub Present"
        else
            fail "guest DNS stub missing"
        fi
        sudo umount "$mnt" 2>/dev/null || sudo umount -l "$mnt" 2>/dev/null
    else
        fail "cannot read-only mount the image for content validation"
    fi
    no_leftover_mounts
    no_leftover_work_dirs
    return 0
}

# --------------------------------------------------------------------------
SCEN="${1:-all}"
case "$SCEN" in
    cleanup)  scenario_cleanup ;;
    preserve) scenario_preserve ;;
    success)  scenario_success ;;
    all)      scenario_cleanup; scenario_preserve; scenario_success ;;
    *) die "usage: $0 [cleanup|preserve|success|all]" ;;
esac

if (( TOTAL_FAIL == 0 )); then
    echo ""
    note "ALL CHECKS PASSED ($SCEN)"
    exit 0
fi
echo ""
note "FAILURES: $TOTAL_FAIL"
exit 1
