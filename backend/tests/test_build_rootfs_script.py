"""Contract pins for infra/firecracker/build-rootfs.sh (no KVM required).

The rootfs build failed in the chroot with "cannot create /dev/null:
Permission denied" and "ca-certificates failed to preconfigure" because
nothing (dev/proc/sys) was mounted inside the chroot before apt-get ran.
These tests pin the rewritten script's contract ORDER and the guard
primitives, so a regression (relative agent path, missing mounts, sha
written before success, non-atomic promote...) cannot land silently.

They are text-contract tests: they run in the normal pytest suite on any
OS (including this Windows dev laptop and CI). The real functional
validation (actual debootstrap + cleanup-under-failure + atomic promote)
is infra/firecracker/test-build-rootfs.sh and must run as root on the
Linux/KVM host — CI does NOT build images.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "firecracker" / "build-rootfs.sh"


def _code() -> str:
    """Script text with full-line comments stripped (position analysis target)."""
    assert SCRIPT.is_file(), f"missing build script: {SCRIPT}"
    lines = []
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _pos(content: str, marker: str) -> int:
    idx = content.find(marker)
    assert idx >= 0, f"marker not found in build-rootfs.sh: {marker!r}"
    return idx


def test_script_exists_and_is_strict() -> None:
    raw = SCRIPT.read_text(encoding="utf-8")
    code = _code()
    assert raw.startswith("#!")
    assert "set -euo pipefail" in code


def test_paths_are_bash_source_resolved_and_absolute() -> None:
    code = _code()
    # repo root from the script location — works from any caller cwd
    assert "BASH_SOURCE[0]" in code
    assert "REPO_ROOT=" in code
    # output path canonicalized without requiring pre-existing dirs
    assert "realpath -m" in code
    # the old caller-cwd-relative agent copy must be gone (from actual code)
    assert "cp ../../" not in code
    idx = _pos(code, 'install -m 644 -- "$AGENT_SRC" "$ROOT/opt/fixhub/agent.py"')
    assert idx >= 0
    # image must not be promoted from a /tmp staging dir: it is built
    # INSIDE the output directory for a same-fs atomic rename
    assert 'IMG="$OUT_DIR/.base.build-' in code


def test_chroot_filesystems_mounted_before_apt() -> None:
    code = _code()
    dev = _pos(code, "--bind /dev ")
    pts = _pos(code, "--bind /dev/pts")
    proc = _pos(code, "mount -t proc")
    sysfs = _pos(code, "mount -t sysfs")
    gate = _pos(code, "stat -c '%t %T' /dev/null")
    fe = _pos(code, "DEBIAN_FRONTEND=noninteractive")
    apt = _pos(
        code, "apt-get install -y --no-install-recommends python3 git ripgrep ca-certificates"
    )
    assert dev < pts, "mount /dev before /dev/pts (pts lives under the dev bind)"
    assert pts < proc < sysfs, "mount order recorded for reverse unwinding"
    assert sysfs < gate < fe < apt, "mounts must precede the dev/null gate and apt-get"


def test_devnull_gate_writable_character_device() -> None:
    code = _code()
    assert "stat -c '%t %T' /dev/null" in code
    assert '"1 3"' in code, "null device must be verified as major/minor 1:3"
    assert "echo probe > /dev/null" in code
    assert "writable character device" in code


def test_noninteractive_and_package_set_preserved() -> None:
    code = _code()
    # every chroot apt invocation is noninteractive
    assert 'chroot "$ROOT" apt-get' not in code, "bare chroot apt-get found"
    assert code.count('chroot "$ROOT" env DEBIAN_FRONTEND=noninteractive') >= 2
    # required image contents (requirement: keep toolchain + certs)
    for pkg in ("python3", "git", "ripgrep", "ca-certificates"):
        assert pkg in code
    # existing DNS contract: guest stub is the host-resolver 10.200.0.1
    assert "nameserver 10.200.0.1" in code
    # build-time DNS comes from the host resolver; the guest stub is written
    # AFTER the chroot package work
    assert _pos(code, "install -m 644 -- /etc/resolv.conf") < _pos(
        code, "printf 'nameserver 10.200.0.1\\n' | sudo tee"
    )


def test_debootstrap_and_minbase_unchanged() -> None:
    code = _code()
    assert "debootstrap --variant=minbase trixie" in code
    assert "http://deb.debian.org/debian" in code


def test_must_be_linux_host() -> None:
    code = _code()
    assert "uname -s" in code
    assert "requires a Linux/KVM host" in code


def test_trap_cleanup_unwinds_reverse_and_removes_temp_state() -> None:
    code = _code()
    assert "trap cleanup EXIT" in code
    assert "trap '_RC=130; cleanup' INT" in code
    assert "trap '_RC=143; cleanup' TERM" in code
    # reverse-order unmount: the same down-counting loop in unmount_all
    # and in the cleanup residual check
    assert code.count("i=${#MOUNTS[@]}-1") == 2
    # lazy-unmount fallback + loud residual check
    assert "sudo umount -l" in code
    assert "mount still alive" in code
    # cleanup removes temp state WITH sudo (root-owned debootstrap tree)
    assert 'sudo rm -rf -- "$WORK" "$IMG"' in code


def test_cleanup_exit_status_not_masked() -> None:
    """Regression: cleanup() must not mask a failing run's exit status.

    The broken handler was `local rc=${_RC:-$?}` with _RC initialized to
    0: since the variable is set and non-empty, it always won the
    expansion, so every failure (die, set -e, the FIXHUB_ROOTFS_FAIL_AFTER
    injected-failure hooks) exited 0 and looked like success.
    Contract: capture $? first; only a nonzero _RC (the INT/TERM traps
    record 130/143) may override it, and cleanup must finally exit "$rc".
    """
    code = _code()
    assert "${_RC:-$?}" not in code, "the status-masking pattern is back"
    head = _pos(code, "cleanup() {")
    capture = _pos(code, "local rc=$?")
    disarm = code.find("trap - EXIT INT TERM")
    assert disarm >= 0, "cleanup() must disarm the signal handlers before unwinding"
    override = code.find('rc="$_RC"')
    assert override >= 0, "nonzero _RC (INT/TERM 130/143) must override the natural status"
    int_trap = _pos(code, "trap '_RC=130; cleanup' INT")
    term_trap = _pos(code, "trap '_RC=143; cleanup' TERM")
    final = _pos(code, 'exit "$rc"')
    assert head < capture, "$? must be captured first inside cleanup()"
    assert capture < disarm, "$? must be captured before any cleanup command runs"
    assert capture < override < disarm, (
        "the nonzero-_RC override must come after the capture, before unwinding"
    )
    assert capture < int_trap < term_trap, "INT/TERM handlers must still record their codes"
    assert disarm < final, 'cleanup() must end with exit "$rc"'


def test_kvm_harness_proves_failures_nonzero_and_image_preserved() -> None:
    """Injected-failure semantics are pinned where they can only be
    PROVEN: the KVM harness (CI cannot build images). Locked so the
    coverage cannot be gutted silently:
    - a forced-failure build MUST be required to exit nonzero
    - the preserve scenario must checksum-compare the previous image
      before/after the failure and reject any modification.
    """
    harness = ROOT / "infra" / "firecracker" / "test-build-rootfs.sh"
    assert harness.is_file(), f"missing KVM harness: {harness}"
    text = harness.read_text(encoding="utf-8")
    assert 'fail "forced-failure build unexpectedly succeeded"' in text, (
        "injected failures must be REQUIRED to exit nonzero"
    )
    assert "forced failure exits non-zero" in text
    assert "previous image preserved byte-identical after failure" in text
    assert "previous image was MODIFIED by a failed build" in text
    assert 'sha256sum "$out"' in text, "preservation must be checked by checksum"


def test_kvm_harness_distinguishes_injected_failures_and_prints_log() -> None:
    """Regression: 'rc=1 but no TEST-INJECTED FAILURE at stage=mount'.

    A forced failure is only meaningful when the builder REACHED its
    injected hook. Everything before the hook (host apt-get, debootstrap —
    which exits 1 on download failure — and die paths) can fail with rc=1
    too; the harness previously treated "rc!=0" as done and never printed
    the log, so an unrelated host failure masqueraded as a hook abort.
    The harness must reject marker-less rc!=0 runs as UNRELATED failures
    (never weaken the marker requirement) and must print the exact
    builder log of the failing run for diagnosis.
    """
    harness = ROOT / "infra" / "firecracker" / "test-build-rootfs.sh"
    assert harness.is_file(), f"missing KVM harness: {harness}"
    text = harness.read_text(encoding="utf-8")
    # helper enforcing BOTH conditions, used by both forced scenarios
    helper = text.find("assert_forced_failure() {")
    assert helper >= 0, "assert_forced_failure helper must exist"
    assert "TEST-INJECTED FAILURE at stage=$stage" in text, (
        "the injected-stage marker must still be REQUIRED"
    )
    assert text.count('assert_forced_failure "$rc" mount') == 1
    assert text.count('assert_forced_failure "$rc" copy') == 1, (
        "preserve must also assert its expected stage=copy marker"
    )
    # unrelated failures are failed under their own name, not conflated
    assert "unrelated build failure" in text
    # the failing run's builder log is printed for diagnosis
    assert text.count("dump_b_log() {") == 1
    # per-invocation logs so a dump points at the exact run (the old
    # single shared B_LOG was truncated by the next invocation)
    for log in (
        "builder-cleanup.log",
        "builder-preserve-fail.log",
        "builder-preserve-success.log",
        "builder-success.log",
    ):
        assert log in text
    # left-behind-state checks stay mandatory in every scenario
    assert text.count("no_leftover_mounts") >= 3
    assert text.count("no_leftover_work_dirs") >= 3


def test_preserve_success_run_does_not_inherit_injected_rc_and_is_traced() -> None:
    """Regression (host-observed on d326330): two silent failures.

    (1) The preserve success-run false-failed: `cmd > log 2>&1 || rc=$?`
    assigns rc ONLY on failure, so a SUCCESSFUL success run inherited the
    injected run's nonzero rc and the scenario printed "successful build
    failed (rc=1)" while the builder's own log ended with promoted/done.
    The rc reset must sit between the two preserve invocations.
    (2) The forced stage=copy run died with rc=1 and NO output at all —
    not even the builder's cleanup-trap "FAILED" line — so static reading
    cannot name which of the silent pre-hook commands (install -d, agent
    install, resolv tee, chmod) was the last one executed. Every builder
    invocation therefore runs under `bash -x` with a file:line PS4 so the
    dumped log tail names the exact failing command + line.
    """
    harness = ROOT / "infra" / "firecracker" / "test-build-rootfs.sh"
    assert harness.is_file(), f"missing KVM harness: {harness}"
    text = harness.read_text(encoding="utf-8")
    forced = text.find('sudo env FIXHUB_ROOTFS_FAIL_AFTER=copy')
    mid = text.find("local mid=$TOTAL_FAIL")
    # the rc reset must exist and must be immediately before the
    # preserve-success invocation (never re-dropped)
    reset = text.find('    rc=0\n    B_LOG="$SCRATCH/builder-preserve-success.log"')
    assert -1 not in (forced, mid, reset), "preserve stale-rc structure broken"
    assert mid < reset < text.find('bash -x "$BUILDER"', reset), (
        "rc=0 must be reset AFTER the injected run and BEFORE the success run"
    )
    # all four builder invocations are command-traced
    assert text.count('bash -x "$BUILDER"') == 4
    # the trace format must be passed EXPLICITLY through sudo env args
    # (env_reset strips exported PS4 — see the dedicated Phase A pin)
    assert text.count('PS4="$XPS4"') == 4
    assert "${BASH_SOURCE##*/}:${LINENO}: " in text, "PS4 must carry file:line"


# The TEMP-DIAG breadcrumbs (the _diag calls bracketing the post-apt
# stretch and each _fail_after) were Phase A diagnostics introduced
# WITHOUT pins and were removed in the Phase C root-cause commit without
# breaking any pin — exactly as designed. The keeper diagnostics
# (startup env-reach note, cleanup exit-record, PS4-through-sudo-env,
# log persistence, sudo-status line) remain pinned below.


def test_builder_announces_fail_after_env_reach_on_startup() -> None:
    """Phase A keeper: every builder log DIRECTLY answers whether the
    test-only fail-hook env var reached the build.

    sudo's env_reset strips caller exports, so "did
    FIXHUB_ROOTFS_FAIL_AFTER arrive?" was previously unanswerable from
    the log. The startup note prints the value (empty = never arrived)
    and must appear BEFORE any state-changing command, so even a very
    early death carries the answer.
    """
    code = _code()
    announce = _pos(code, 'note "fail-hook: FIXHUB_ROOTFS_FAIL_AFTER=')
    first_state_change = _pos(code, "sudo apt-get update")
    assert announce < first_state_change, (
        "the env-reach note must print before the build work begins"
    )
    assert "FIXHUB_ROOTFS_FAIL_AFTER:-}" in code, "the note must tolerate an unset var"


def test_builder_cleanup_logs_resolved_exit_status() -> None:
    """Phase A keeper: cleanup() writes an exit-record line with its
    RESOLVED status (natural rc after the nonzero-_RC override).

    Host evidence: a forced stage=copy run exited rc=1 WITHOUT the trap's
    "[rootfs] FAILED (exit 1)" banner, while "no leftover mounts" passed
    — i.e. the cleanup trap demonstrably ran yet produced no visible
    output. The exit record, printed FIRST in the unwind (after disarm),
    is the earliest presence marker of the trap in such a truncated run
    and distinguishes sudo-death from builder-death.
    Contract: the record sits INSIDE the existing cleanup() after the
    $? capture / _RC override / trap-disarm sequence it must not replace.
    """
    code = _code()
    head = _pos(code, "cleanup() {")
    disarm = code.find("trap - EXIT INT TERM")
    final = _pos(code, 'exit "$rc"')
    assert "trap cleanup EXIT" in code, "the EXIT trap contract must be untouched"
    record = _pos(code, 'note "diag: exit-recorder rc=$rc _RC=${_RC:-0}"')
    assert disarm < record < final, (
        "exit record must come after trap disarm (nothing may run before the "
        "$? capture) and before cleanup ends"
    )
    assert head < disarm, "cleanup() structurally intact"


def test_lock_open_never_repoints_shell_stderr() -> None:
    """ROOT-CAUSE regression (host-identified via the persisted archives).

    `exec redirs` with ONLY redirections applies them PERMANENTLY to the
    current shell. The old form
        exec 9>"$LOCK" 2>/dev/null || echo "...running unlocked" >&2
    opened the lock fine EVERY build — and in doing so re-bound the
    builder shell's OWN stderr to /dev/null for the rest of the run.
    From that line onward every stderr write vanished: the injected-
    failure markers ("rc=1 WITHOUT the stage=copy/mount marker"),
    die() texts, the cleanup "[rootfs] FAILED" banner and all bash -x
    traces (six EC2 rounds worth of phantom "unrelated build failure").
    The stderr redirection must never sit beside an `exec 9>` stdin/lock
    open again; open-failure fallback stays the `|| echo ... >&2` WARN.
    """
    code = _code()
    assert 'exec 9>"$LOCK"' in code, "the lock-open line must remain an exec fd 9"
    assert not re.search(r"exec 9>[^\n]*2>/dev/null", code), (
        "an stderr redirection beside exec 9 re-binds the SHELL's own "
        "stderr permanently — the root cause of the marker-less rc=1"
    )
    assert "cannot open lock $LOCK; running unlocked" in code, (
        "the unlocked-WARN fallback must stay"
    )


def test_harness_passes_tracing_ps4_through_sudo_env() -> None:
    """Phase A keeper: the line-numbered PS4 must survive sudo env_reset.

    sudo strips exported environment variables (env_reset); an `export
    PS4` in the harness never reached the builder bash, so the archived
    logs carried NO `+` trace lines despite f7a63cf's export. PS4 must be
    passed as an explicit `env PS4=...` argument on EVERY invocation —
    explicit env arguments survive.
    """
    harness = ROOT / "infra" / "firecracker" / "test-build-rootfs.sh"
    assert harness.is_file(), f"missing KVM harness: {harness}"
    text = harness.read_text(encoding="utf-8")
    assert "XPS4='+ ${BASH_SOURCE##*/}:${LINENO}: '" in text, (
        "the trace format must carry file:line"
    )
    assert text.count('PS4="$XPS4"') == 4, "every invocation passes PS4 explicitly"
    assert text.count("sudo bash -x") == 0, "no invocation may skip the env args"
    assert text.count('bash -x "$BUILDER"') == 4


def test_harness_persists_full_builder_logs_restricted() -> None:
    """Phase A keeper: full builder logs survive the harness exit.

    Scratch — with its logs — is deleted on harness exit, and dump_b_log
    prints only a 150-line tail: past host evidence has been cut off
    above the interesting part. Every COMPLETED log must be archived
    WHOLE under /var/tmp/fixhub-rootfs-logs BEFORE the scratch dies, with
    restricted permissions (dir 0700, files 600: root-owned host
    diagnostics, command traces, never world-readable), and the archive
    path must be printed by dump_b_log so a host paste can include it.
    """
    harness = ROOT / "infra" / "firecracker" / "test-build-rootfs.sh"
    assert harness.is_file(), f"missing KVM harness: {harness}"
    text = harness.read_text(encoding="utf-8")
    assert 'PERSIST_DIR="/var/tmp/fixhub-rootfs-logs"' in text
    assert text.count("install -d -m 700") == 1
    assert text.count("install -m 600") == 1
    # one archive per invocation: 4 builder runs across the scenarios
    assert text.count('persist_b_log "') == 4
    # the dump names the persisted full copy
    assert "persisted full copy" in text


def test_harness_records_outer_sudo_status_in_every_archived_log() -> None:
    """Phase A keeper: the OUTER sudo's exit status is recorded in the
    archived log.

    sudo normally forwards the child builder's status, but returns 1 on
    sudo's OWN errors (auth, I/O, signal handling). The appended
    "outer sudo exit status: N" line — written after each invocation,
    BEFORE the archive — makes a confusing rc=1 with a healthy-looking
    builder tail decidable from the archived log alone.
    """
    harness = ROOT / "infra" / "firecracker" / "test-build-rootfs.sh"
    assert harness.is_file(), f"missing KVM harness: {harness}"
    text = harness.read_text(encoding="utf-8")
    assert "record_sudo_rc() {" in text
    assert "outer sudo exit status" in text
    assert text.count('record_sudo_rc "$rc"') == 4


def test_unmount_before_image_copy_and_leak_guard() -> None:
    code = _code()
    unmount = _pos(code, "unmount_all || die")
    still_mounted = _pos(code, "mount still alive after unmount")
    cmp_guard = _pos(code, 'cmp -s "$WORK/pseudo.before" "$WORK/pseudo.after"')
    dd = _pos(code, 'dd if=/dev/zero of="$IMG"')
    copy = _pos(code, 'cp -a "$ROOT/." "$MNT/"')
    assert unmount < still_mounted < cmp_guard < dd < copy, (
        "all chroot mounts must be unmounted + leak-checked before the tree"
        " is copied into the image"
    )
    # explicit guard text: host devfs can never reach the image
    assert "must never reach the image" in code
    assert "would leak into the image" in code
    # the image mounts are recorded on the same stack (rw build + ro validation)
    assert code.count("MOUNTS+=(") >= 6


def test_image_promoted_only_after_validation() -> None:
    code = _code()
    fsck = _pos(code, "e2fsck -f -n")
    ro_mount = _pos(code, "mount -o loop,ro")
    python3 = _pos(code, '"$MNT/usr/bin/python3"')
    git = _pos(code, '"$MNT/usr/bin/git"')
    rg = _pos(code, '"$MNT/usr/bin/rg"')
    cab = _pos(code, 'ca-certificates.crt" ]]')
    agent_cmp = _pos(code, 'cmp -s -- "$AGENT_SRC" "$MNT/opt/fixhub/agent.py"')
    manifest = _pos(code, 'grep -q \'"manifest"\' "$MNT/opt/fixhub/agent.py"')
    promote = _pos(code, 'sudo mv -f -- "$IMG" "$OUT"')
    sha = _pos(code, 'sha256sum "$OUT" | sudo tee "$OUT.sha256"')
    verify = _pos(code, 'sha256sum -c -- "$OUT.sha256"')
    assert fsck < ro_mount, "filesystem check before the read-only mount"
    for pos in (python3, git, rg, cab, agent_cmp, manifest):
        assert ro_mount < pos < promote, "image content validated before promote"
    assert promote < sha < verify, "sidecar written + verified only after promote"
    # no sidecar may ever be written before the validation pass
    assert code.rfind("sha256sum") > code.rfind("e2fsck -f -n")


def test_fail_hooks_are_noop_without_env() -> None:
    code = _code()
    assert "FIXHUB_ROOTFS_FAIL_AFTER:-}" in code, "hook must default to empty"
    for stage in ("mount", "verify", "apt", "copy"):
        assert f"_fail_after {stage}" in code
    # hook order matches the pipeline stages
    m = _pos(code, "_fail_after mount")
    v = _pos(code, "_fail_after verify")
    a = _pos(code, "_fail_after apt")
    c = _pos(code, "_fail_after copy")
    assert m < v < a < c
    assert c < _pos(code, "unmount_all || die")


def test_lock_guards_concurrent_builds() -> None:
    code = _code()
    assert "flock -n 9" in code
    assert ".base.lock" in code


def test_manifest_op_baked_agent_required() -> None:
    # the rootfs rebuild exists to deliver the #70/#71 guest agent; the
    # script must refuse to bake an agent without the manifest op
    code = _code()
    assert code.count('grep -q \'"manifest"\'') == 2
    assert "lacks the manifest op" in code


# ---------------------------------------------------------------------------
# Guest BOOTABILITY contract (the real-boot incident of 2026-10-29).
#
# The first real production Firecracker boot failed with
# "guest agent never answered: Firecracker vsock bridge closed before
# CONNECT acknowledgement". Host evidence: the rebuild completed green
# (promoted + done + sidecar) yet the image had NO /sbin/init, NO inittab,
# NO rc.local, NO systemd unit and zero references to agent.py — and
# firecracker.py's boot_args had never carried root= nor init= (git grep
# across every commit). debootstrap --variant=minbase is not a bootable
# system, so the guest could never start the agent on vsock port 5000.
# The pins below bind BOTH halves of the boot chain together because CI
# can never boot a VM to test them (static-only suite, no KVM).


def test_builder_bakes_a_bootable_guest_pid1() -> None:
    """The image must contain an executable /sbin/init shell script that
    mounts the pseudofs and execs the guest agent (debootstrap minbase
    ships NO init). Raw-text assertions: the heredoc body is invisible to
    _code()'s comment stripping."""
    raw = SCRIPT.read_text(encoding="utf-8")
    write = _pos(raw, 'sudo tee "$ROOT/sbin/init" >/dev/null')
    body_start = raw.find("#!/bin/sh\n# FixHub guest PID1")
    assert body_start > write >= 0, "PID1 heredoc must follow the tee write"
    body = raw[body_start:raw.find("\nEOF", body_start)]
    assert "mount -t proc proc /proc" in body, "PID1 must mount /proc"
    assert "mount -t sysfs sysfs /sys" in body, "PID1 must mount /sys"
    assert "exec /usr/bin/python3 -u /opt/fixhub/agent.py" in body, (
        "PID1 must exec the baked agent (it binds AF_VSOCK:5000 and loops "
        "forever — a never-exiting PID1 by design)"
    )
    chmod = _pos(raw, 'sudo chmod 755 "$ROOT/sbin/init"')
    assert chmod > body_start, "PID1 must be made executable after the write"
    # the glue belongs to the guest-content stage: written BEFORE the
    # stage=copy hook and the image assembly
    copy_hook = _pos(raw, "_fail_after copy")
    mnt = _pos(raw, "sudo mount -o loop \"$IMG\"")
    assert copy_hook < mnt, "PID1 glue must be written before the ext4 copy"


def test_image_validation_requires_bootable_guest() -> None:
    """The pre-promote validation battery must prove the boot chain, not
    just agent content: sh + mount present, python3 executable, and an
    executable /sbin/init shell script whose body execs the agent."""
    code = _code()
    agent_cmp = _pos(code, 'sudo cmp -s -- "$AGENT_SRC" "$MNT/opt/fixhub/agent.py"')
    manifest = _pos(code, "\'\"manifest\"\' \"$MNT/opt/fixhub/agent.py\"")
    checks = [
        _pos(code, '[[ -e "$MNT/bin/sh" ]]'),
        _pos(code, '[[ -e "$MNT/bin/mount" ]]'),
        _pos(code, '[[ -x "$MNT/usr/bin/python3" ]]'),
        _pos(code, '[[ -x "$MNT/sbin/init" ]]'),
        _pos(code, '"$MNT/sbin/init")" == "#!/bin/sh"'),
        _pos(code, "| die \"image validation: /sbin/init does not exec the guest agent\""),
    ]
    for chk in checks:
        assert chk > manifest, "bootability checks must ride behind the manifest check"
        assert chk > agent_cmp, "bootability must be checked after agent content"
    # and ALL of it happens on the staging image BEFORE the atomic promote
    promote = _pos(code, 'sudo mv -f -- "$IMG" "$OUT"')
    assert promote > max(checks), "no image may be promoted before bootability passes"


def test_boot_args_mount_root_and_start_the_baked_pid1() -> None:
    """firecracker.py boot_args must mount the virtio root and exec the
    /sbin/init the builder bakes — the two halves of one contract, pinned
    together because neither is exercisable in CI (no KVM)."""
    fc = ROOT / "backend" / "app" / "sandbox" / "firecracker.py"
    text = fc.read_text(encoding="utf-8")
    boot = _pos(text, "console=ttyS0")
    root = _pos(text, "root=/dev/vda rw")
    init = _pos(text, "init=/sbin/init")
    ip = _pos(text, "255.255.255.252::eth0:off")
    assert boot < root < init < ip, (
        "boot_args order: console … root=/dev/vda rw init=/sbin/init ip=… "
        "— init= is a kernel param (consumed); a stray ip= token reaching "
        "PID1 as argv is harmless (the PID1 script ignores $@)"
    )
    # coupling: the exact PID1 path named by the kernel args is the file
    # the builder bakes with the agent exec
    glue = _code()
    _pos(glue, 'sudo tee "$ROOT/sbin/init" >/dev/null')
    _pos(glue, 'sudo chmod 755 "$ROOT/sbin/init"')
