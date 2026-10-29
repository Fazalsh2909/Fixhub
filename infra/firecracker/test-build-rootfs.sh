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
# Each builder invocation gets its OWN scratch log (builder-cleanup.log,
# builder-preserve-fail.log, builder-preserve-success.log,
# builder-success.log) and every failed scenario part prints that log's
# tail to stderr (scratch — with the logs — is deleted on harness exit).
# A forced failure only counts as the injected scenario when the builder
# printed its "TEST-INJECTED FAILURE at stage=<stage>" marker; rc!=0
# without the marker is reported as an UNRELATED build failure so the two
# causes can never be conflated.
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

# Print this part's builder log when the part added any failure — the
# scratch (and the log with it) is deleted when the harness exits, so a
# failing run MUST show the evidence while it still exists.
dump_b_log() { # FAIL_BEFORE LABEL LOG_PATH
    if (( TOTAL_FAIL > $1 )); then
        echo "" >&2
        echo "  ---- builder log, '$2' (last 150 lines of $3):" >&2
        tail -n 150 -- "$3" >&2 2>/dev/null \
            || echo "  (builder log not found: $3)" >&2
    fi
    return 0
}

# A forced failure only PASSES as the injected scenario when the builder
# actually reported its injected stage. rc!=0 WITHOUT the marker is an
# unrelated build failure (host packaging, debootstrap download, ... —
# debootstrap exits 1 on download failure, as does every die() path) and
# is failed separately so the two causes can never be conflated.
assert_forced_failure() { # RC STAGE
    local rc="$1" stage="$2"
    if (( rc == 0 )); then
        fail "forced-failure build unexpectedly succeeded"
        return 1
    fi
    ok "forced failure exits non-zero (rc=$rc)"
    if grep -q "TEST-INJECTED FAILURE at stage=$stage" "$B_LOG"; then
        ok "injected hook confirmed: stage=$stage"
    else
        fail "rc=$rc WITHOUT the injected stage=$stage marker: unrelated build failure, not the injected hook"
    fi
    return 0
}

# --------------------------------------------------------------------------
scenario_cleanup() {
    note "scenario: cleanup (forced failure at mount stage)"
    local out="$SCRATCH/base-cleanup.ext4" rc=0 before=$TOTAL_FAIL
    B_LOG="$SCRATCH/builder-cleanup.log"
    sudo env FIXHUB_ROOTFS_FAIL_AFTER=mount \
        bash "$BUILDER" "$out" 1024 > "$B_LOG" 2>&1 || rc=$?
    assert_forced_failure "$rc" mount
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
    dump_b_log "$before" "cleanup (fail at mount)" "$B_LOG"
    return 0
}

# --------------------------------------------------------------------------
scenario_preserve() {
    note "scenario: preserve (previous image survives failure, then is replaced)"
    local out="$SCRATCH/base-preserve.ext4" rc=0 before=$TOTAL_FAIL
    # a known "previous" image (random bytes — never a real fs)
    sudo dd if=/dev/urandom of="$out" bs=1K count=64 status=none
    local pre_sha
    pre_sha="$(sudo sha256sum "$out" | cut -d' ' -f1)"

    B_LOG="$SCRATCH/builder-preserve-fail.log"
    sudo env FIXHUB_ROOTFS_FAIL_AFTER=copy \
        bash "$BUILDER" "$out" 1024 > "$B_LOG" 2>&1 || rc=$?
    assert_forced_failure "$rc" copy
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
    dump_b_log "$before" "preserve (fail at copy)" "$B_LOG"

    # success pass: the very same path must now be atomically replaced
    local mid=$TOTAL_FAIL
    B_LOG="$SCRATCH/builder-preserve-success.log"
    sudo bash "$BUILDER" "$out" 1024 > "$B_LOG" 2>&1 || rc=$?
    if (( rc == 0 )); then
        ok "successful build replaces the preserved image (rc=0)"
    else
        fail "successful build failed (rc=$rc)"
        dump_b_log "$mid" "preserve (success run)" "$B_LOG"
        return 0
    fi
    post_sha="$(sudo sha256sum "$out" | cut -d' ' -f1)"
    [[ "$post_sha" != "$pre_sha" ]] && ok "image content replaced" \
        || fail "successful build did not replace the image"
    if (cd "$SCRATCH" && sudo sha256sum -c -- "base-preserve.ext4.sha256" >/dev/null 2>&1); then
        ok "sidecar validates against the promoted image"
    else
        fail "sidecar does not match the promoted image"
    fi
    dump_b_log "$mid" "preserve (success run)" "$B_LOG"
    return 0
}

# --------------------------------------------------------------------------
scenario_success() {
    note "scenario: success (real build, full validation battery)"
    local out="$SCRATCH/base-success.ext4" rc=0 before=$TOTAL_FAIL
    B_LOG="$SCRATCH/builder-success.log"
    sudo bash "$BUILDER" "$out" 1024 > "$B_LOG" 2>&1 || rc=$?
    if (( rc != 0 )); then
        fail "real build failed (rc=$rc)"
        dump_b_log "$before" "success (real build)" "$B_LOG"
        return 0
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
    dump_b_log "$before" "success (real build)" "$B_LOG"
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
