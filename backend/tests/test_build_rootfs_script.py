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
