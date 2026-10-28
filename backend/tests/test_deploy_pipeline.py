"""GitHub Actions -> SSM deployment pipeline: gating, safety, and behavior.

These tests lock down the automated staging deployment so it can never become
the weak link in the security model:

- the deploy job runs only for pushes (or an explicit dispatch) to the
  deployment branch — never for pull requests or other branches,
- deployment depends on every required CI job and cannot run concurrently
  with another deployment,
- third-party actions are pinned to verified full commit SHAs,
- the server-side deploy script preserves runtime state (.env, virtualenvs,
  databases, workspaces), never destructively cleans, never downgrades the
  database, validates the exact target SHA, and rolls back code on failure,
- the restart-selection plan only restarts services whose code/config really
  changed (and propagates the systemd Requires= netd dependency),
- the worker drain check fails closed when worker state cannot be determined,
- the health-check retry loop is bounded and eventually succeeds/fails,
- dependency installation works without pip inside the venvs (the server's
  uv-managed venvs do not seed it): uv first, then the venv's own pip, then
  a strictly venv-local ensurepip — never recreating a venv and failing
  loudly when no installer exists.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY_SH = REPO_ROOT / "infra" / "deploy" / "deploy.sh"
PLAN_PY = REPO_ROOT / "infra" / "deploy" / "plan.py"
DRAIN_PY = REPO_ROOT / "infra" / "deploy" / "drain.py"
DEPLOY_BRANCH = "rc/public-app-v1"
SHA_PIN_RE = re.compile(r"^[A-Za-z0-9_./-]+@[0-9a-f]{40}$")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def workflow() -> dict:
    import yaml

    text = WORKFLOW.read_text(encoding="utf-8")
    assert text, ".github/workflows/ci.yml must exist"
    data = yaml.safe_load(text)
    # YAML parses a bare `on:` key as boolean True.
    return data


@pytest.fixture(scope="module")
def deploy_sh() -> str:
    text = DEPLOY_SH.read_text(encoding="utf-8")
    assert text, "infra/deploy/deploy.sh must exist"
    return text


def _triggers(workflow: dict) -> dict:
    return workflow.get("on") or workflow.get(True) or {}


def _code_lines(text: str) -> str:
    """Deploy-script text with full-line comments removed.

    Forbidden-pattern assertions must judge executable code, not the header
    prose that documents why the pattern is forbidden.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _bash_ok() -> bool:
    """True when a *working* bash exists.

    On Windows, `bash` on PATH is often the WSL launcher stub, which exists
    but fails without an installed distribution; probe it once.
    """
    if shutil.which("bash") is None:
        return False
    probe = subprocess.run(
        ["bash", "-c", "echo ok"], capture_output=True, text=True
    )
    return probe.returncode == 0


def _deploy_job(workflow: dict) -> dict:
    return workflow["jobs"]["deploy"]


# ---------------------------------------------------------------------------
# Workflow: preserved CI + deploy gating
# ---------------------------------------------------------------------------


def test_ci_still_validates_pushes_and_pull_requests(workflow):
    triggers = _triggers(workflow)
    assert "push" in triggers, "existing push CI must be preserved"
    assert "pull_request" in triggers, "PR validation must be preserved"
    assert "workflow_dispatch" in triggers, "manual deploy must be available"


def test_deploy_gated_to_deploy_branch_only(workflow):
    condition = _deploy_job(workflow)["if"]
    assert f"refs/heads/{DEPLOY_BRANCH}" in condition
    assert "workflow_dispatch" in condition
    # Pull requests and other branches must never reach the server.
    assert "pull_request" not in condition
    assert condition.count(f"refs/heads/{DEPLOY_BRANCH}") >= 1


def test_deploy_depends_on_all_required_ci_jobs(workflow):
    needs = _deploy_job(workflow)["needs"]
    assert set(needs) == {"backend", "frontend"}


def test_deploy_concurrency_serializes_without_cancelling(workflow):
    concurrency = _deploy_job(workflow)["concurrency"]
    assert concurrency["group"], "deployments need a concurrency group"
    assert concurrency["cancel-in-progress"] is False, (
        "a newer push must wait, never cancel a deployment already touching "
        "the server"
    )


def test_workflow_permissions_are_minimal(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    deploy_permissions = _deploy_job(workflow)["permissions"]
    assert deploy_permissions == {"contents": "read", "id-token": "write"}


def test_deploy_uses_staging_environment(workflow):
    assert _deploy_job(workflow)["environment"] == "staging"


def test_all_actions_pinned_to_verified_shas(workflow):
    pinned: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses:
                pinned.append(uses)
    assert pinned, "workflow must use actions"
    for uses in pinned:
        assert SHA_PIN_RE.match(uses), f"action not SHA-pinned: {uses}"


def test_ssm_wiring_is_exact_and_has_no_hardcoded_identifiers(workflow):
    text = WORKFLOW.read_text(encoding="utf-8")
    for needle in (
        "aws ssm send-command",
        "aws ssm get-command-invocation",
        "AWS-RunShellScript",
        "${{ vars.EC2_INSTANCE_ID }}",
        "${{ vars.AWS_REGION }}",
        "${{ vars.AWS_ROLE_ARN }}",
        "${{ github.sha }}",
    ):
        assert needle in text, f"missing SSM wiring: {needle}"
    # Identifiers must come from repository variables, never be hardcoded.
    assert not re.search(r"\bi-[0-9a-f]{8,}\b", text), "hardcoded instance id"
    assert "arn:aws:" not in text, "hardcoded ARN"
    assert not re.search(r"\b\d{12}\b", text), "hardcoded AWS account id"


def test_bootstrap_deploys_the_exact_commit(workflow):
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "checkout --quiet -B" in text, "bootstrap must pin the checkout"
    assert 'deploy.sh" "$TARGET_SHA"' in text, (
        "bootstrap must exec the freshly checked-out deploy script"
    )


# ---------------------------------------------------------------------------
# deploy.sh: structural safety properties
# ---------------------------------------------------------------------------


def test_deploy_script_bash_syntax_is_valid(deploy_sh):
    if not _bash_ok():
        pytest.skip("no working bash on this machine")
    result = subprocess.run(
        ["bash", "-n", str(DEPLOY_SH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_deploy_script_never_destroys_state(deploy_sh):
    code = _code_lines(deploy_sh)
    # No destructive cleanup, ever.
    assert "git clean" not in code
    assert "rm -rf /" not in code
    # Deletion is legitimate in exactly two narrowly-scoped places inside the
    # service-tree sync: removing the temporary staging export directory, and
    # removing a single validated repo-relative path that git reports as
    # deleted between two validated commits. Any other rm is a regression.
    rm_lines = [
        line for line in code.splitlines() if re.search(r"(?<![\w-])rm\s", line)
    ]
    assert rm_lines, "service-tree sync deletion logic missing"
    for line in rm_lines:
        assert ('"$NET_SYNC_STAGING"' in line) or ('"$delete_path"' in line), (
            f"rm outside the scoped sync primitives: {line.strip()}"
        )
    # No automatic database downgrade, ever.
    assert "alembic downgrade" not in code
    assert "downgrade head" not in code
    # Runtime state is never overwritten or deleted: .env is only ever read
    # (sourced via the deploy env file) and the virtualenvs are only used via
    # their python/pip binaries.
    assert not re.search(r">\s*(\"?\$REPO_APP)?/?backend/\.env\b", code)


def test_deploy_script_has_required_safety_properties(deploy_sh):
    code = _code_lines(deploy_sh)
    for needle in (
        "set -euo pipefail",
        # Exact-SHA validation before anything else runs.
        "is not a 40-hex commit SHA",
        "merge-base --is-ancestor",
        # Service-tree sync: validated export from the canonical checkout,
        # never a git checkout of /opt/fixhub (which has no .git).
        "sync_net_tree",
        "git archive",
        "--no-renames",
        "_net_path_is_safe",
        "does not exist; provision it first",
        # Single explicit migration step, before restarts.
        "alembic upgrade head",
        # Worker drain with a documented timeout, never a kill.
        "drain.py",
        "DRAIN_TIMEOUT_S",
        "refusing to kill it",
        # Live security verification via the tested installer verifier.
        "install_api_security",
        "AUTH_COOKIE_SECURE=1",
        # Bounded health checks (local mandatory + public configurable).
        "--max-time",
        "PUBLIC_HEALTH_REQUIRED",
        # Rollback records and restores the previous SHA; no DB downgrade.
        '"previous_sha"',
        "rolling application code back to $PREV_SHA",
        # Non-zero exit on failure propagates to GitHub Actions.
        "deployment FAILED",
    ):
        assert needle in code, f"deploy.sh missing: {needle}"


def _run_health_check(ok_on_call: int, attempts: int, interval: int):
    """Source deploy.sh and run health_check with `curl` faked in-shell.

    A bash function named `curl` shadows the PATH lookup, so the fake needs
    no temporary executable (Windows exec-bit emulation makes that fragile).
    `FAKE_OK_ON_CALL` is the 1-based call number that returns HTTP 200; a
    value larger than `attempts` makes every probe fail.

    The call counter lives in a FILE on purpose: deploy.sh probes curl
    inside `$( ... )` command substitutions, and each of those is a
    subshell — a counter variable mutated inside the function never
    propagates back to the parent shell, so every probe would see the same
    initial value. A file survives across subshells.
    """
    import tempfile

    if not _bash_ok():
        pytest.skip("no working bash on this machine")
    with tempfile.TemporaryDirectory() as tmp:
        counter = Path(tmp) / "curl-calls"
        counter.write_text("0", encoding="utf-8")
        script = (
            f'source "{DEPLOY_SH.as_posix()}";\n'
            "curl() {\n"
            '  n=$(cat "$FAKE_COUNT_FILE" 2>/dev/null || echo 0)\n'
            "  n=$((n + 1))\n"
            '  echo "$n" > "$FAKE_COUNT_FILE"\n'
            '  if [ "$n" -ge "$FAKE_OK_ON_CALL" ]; then printf 200; else printf 000; fi\n'
            "}\n"
            # `|| rc=$?` guards the call: sourcing deploy.sh enables `set -e`,
            # which would otherwise abort this shell before rc can be echoed.
            f'rc=0; health_check "http://fake.invalid/health" {attempts} {interval} || rc=$?\n'
            'echo "rc=$rc"\n'
        )
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "FAKE_COUNT_FILE": counter.as_posix(),
                "FAKE_OK_ON_CALL": str(ok_on_call),
            },
            timeout=60,
        )
        # Read before the TemporaryDirectory context deletes the file.
        calls = counter.read_text(encoding="utf-8").strip()
        return result, calls


def test_health_check_retries_until_success():
    result, calls = _run_health_check(ok_on_call=3, attempts=5, interval=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rc=0" in result.stdout
    assert "attempt 3/5" in result.stdout, "should succeed on the 3rd probe"
    assert calls == "3", "exactly three probes must be made"


def test_health_check_fails_after_bounded_attempts():
    result, calls = _run_health_check(ok_on_call=99, attempts=3, interval=0)
    # health_check fails (rc=1 is echoed by the guard), but the test shell
    # itself must survive and exit cleanly — the failure is asserted here,
    # not by crashing the harness.
    assert "rc=1" in result.stdout, result.stdout + result.stderr
    assert "FAILED after 3 attempts" in result.stdout
    assert calls == "3", "must stop after the bounded attempts"


# ---------------------------------------------------------------------------
# deploy.sh: service-tree sync (/opt/fixhub has no .git)
# ---------------------------------------------------------------------------


def _run_sync_script(body: str, timeout: int = 120):
    """Run a bash snippet with deploy.sh sourced and paths isolated.

    deploy.sh assigns its default server paths at source time, so the test
    overrides REPO_APP/REPO_NET *after* sourcing (every function reads the
    globals at call time) and replaces git_as_owner with plain git — tests
    run unprivileged in throwaway temp directories and never touch the real
    server paths. Assertions happen inside the bash snippet (the temp dir is
    deleted when this helper returns), so each scenario prints PASS/FAIL
    markers that the test asserts on.
    """
    import tempfile

    if not _bash_ok():
        pytest.skip("no working bash on this machine")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).as_posix()
        prelude = (
            # deploy.sh enables `set -euo pipefail`; every potentially
            # failing statement in the body must be guarded.
            "set -u\n"
            f'ROOT="{root}"\n'
            f'source "{DEPLOY_SH.as_posix()}"\n'
            # deploy.sh assigns its default server paths at source time but
            # every function reads the globals at call time, so the isolated
            # paths must be overridden AFTER sourcing.
            'REPO_APP="$ROOT/canonical"\n'
            'REPO_NET="$ROOT/service-tree"\n'
            "git_as_owner() { local repo=\"$1\"; shift; git -C \"$repo\" \"$@\"; }\n"
            "make_repo() {\n"
            '  git init -q "$REPO_APP"\n'
            '  git -C "$REPO_APP" config user.email test@example.com\n'
            '  git -C "$REPO_APP" config user.name test\n'
            # Keep blob == worktree content byte-identical on Windows hosts.
            '  git -C "$REPO_APP" config core.autocrlf false\n'
            "}\n"
            "commit_all() {\n"
            '  git -C "$REPO_APP" add -A\n'
            '  git -C "$REPO_APP" commit -qm "$1"\n'
            '  git -C "$REPO_APP" rev-parse HEAD\n'
            "}\n"
        )
        script = prelude + body
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result


def test_net_sync_full_export_into_plain_directory():
    body = (
        "make_repo\n"
        'mkdir -p "$REPO_APP/backend/app" "$REPO_APP/infra/firecracker"\n'
        'echo v1 > "$REPO_APP/backend/app/main.py"\n'
        'echo helper-v1 > "$REPO_APP/infra/firecracker/fixhub-netd.py"\n'
        "A=$(commit_all a)\n"
        # The service tree is a plain directory with runtime state and NO .git.
        'mkdir -p "$REPO_NET/backend/app"\n'
        'echo venv-marker > "$REPO_NET/backend/.venv-marker"\n'
        'echo SECRET=1 > "$REPO_NET/backend/.env"\n'
        'mkdir -p "$REPO_NET/workspaces"\n'
        'echo runtime > "$REPO_NET/workspaces/ws.txt"\n'
        'sync_net_tree "" "$A" || echo "FAIL: full sync rc=$?"\n'
        'grep -q v1 "$REPO_NET/backend/app/main.py" '
        '&& echo "PASS: tracked file copied" || echo "FAIL: tracked file missing"\n'
        'grep -q helper-v1 "$REPO_NET/infra/firecracker/fixhub-netd.py" '
        '&& echo "PASS: helper source copied" || echo "FAIL: helper missing"\n'
        'grep -q venv-marker "$REPO_NET/backend/.venv-marker" '
        '&& echo "PASS: venv preserved" || echo "FAIL: venv lost"\n'
        'grep -q SECRET=1 "$REPO_NET/backend/.env" '
        '&& echo "PASS: env preserved" || echo "FAIL: env lost"\n'
        'grep -q runtime "$REPO_NET/workspaces/ws.txt" '
        '&& echo "PASS: workspaces preserved" || echo "FAIL: workspaces lost"\n'
    )
    result = _run_sync_script(body)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "full export applied" in out
    assert "PASS: tracked file copied" in out
    assert "PASS: venv preserved" in out
    assert "PASS: env preserved" in out
    assert "PASS: workspaces preserved" in out


def test_net_sync_applies_exactly_the_requested_diff():
    body = (
        "make_repo\n"
        'mkdir -p "$REPO_APP/backend/app" "$REPO_APP/infra/firecracker"\n'
        'echo v1 > "$REPO_APP/backend/app/main.py"\n'
        'echo helper-v1 > "$REPO_APP/infra/firecracker/fixhub-netd.py"\n'
        'echo obsolete > "$REPO_APP/obsolete.txt"\n'
        "A=$(commit_all a)\n"
        'mkdir -p "$REPO_NET/backend/app" "$REPO_NET/infra/firecracker"\n'
        'echo SECRET=1 > "$REPO_NET/backend/.env"\n'
        'sync_net_tree "" "$A" || echo "FAIL: seed full sync rc=$?"\n'
        # Commit B: modify a file, add a file, modify the helper, delete one.
        'echo v2 > "$REPO_APP/backend/app/main.py"\n'
        'echo new > "$REPO_APP/backend/app/egress.py"\n'
        'echo helper-v2 > "$REPO_APP/infra/firecracker/fixhub-netd.py"\n'
        'git -C "$REPO_APP" rm -q obsolete.txt\n'
        "B=$(commit_all b)\n"
        'sync_net_tree "$A" "$B" || echo "FAIL: incremental rc=$?"\n'
        'git -C "$REPO_APP" show "$B:backend/app/main.py" > "$ROOT/expected-main.txt"\n'
        'diff -q "$REPO_NET/backend/app/main.py" "$ROOT/expected-main.txt" >/dev/null '
        '&& echo "PASS: content matches commit B" || echo "FAIL: content mismatch"\n'
        'grep -q new "$REPO_NET/backend/app/egress.py" '
        '&& echo "PASS: added file synced" || echo "FAIL: added file missing"\n'
        'grep -q helper-v2 "$REPO_NET/infra/firecracker/fixhub-netd.py" '
        '&& echo "PASS: helper source refreshed" || echo "FAIL: helper stale"\n'
        '[ ! -e "$REPO_NET/obsolete.txt" ] '
        '&& echo "PASS: deleted file removed" || echo "FAIL: deleted file still present"\n'
        'grep -q SECRET=1 "$REPO_NET/backend/.env" '
        '&& echo "PASS: env still preserved" || echo "FAIL: env lost"\n'
    )
    result = _run_sync_script(body)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "copied 3, deleted 1" in out, out
    assert "PASS: content matches commit B" in out
    assert "PASS: helper source refreshed" in out
    assert "PASS: deleted file removed" in out


def test_net_sync_reverse_diff_restores_previous_tree():
    body = (
        "make_repo\n"
        'mkdir -p "$REPO_APP/backend/app"\n'
        'echo v1 > "$REPO_APP/backend/app/main.py"\n'
        'echo keep > "$REPO_APP/backend/app/stable.py"\n'
        "A=$(commit_all a)\n"
        'mkdir -p "$REPO_NET/backend/app"\n'
        'echo SECRET=1 > "$REPO_NET/backend/.env"\n'
        'sync_net_tree "" "$A" || echo "FAIL: seed full sync rc=$?"\n'
        'echo v2 > "$REPO_APP/backend/app/main.py"\n'
        'echo added > "$REPO_APP/backend/app/egress.py"\n'
        'git -C "$REPO_APP" rm -q backend/app/stable.py\n'
        "B=$(commit_all b)\n"
        'sync_net_tree "$A" "$B" || echo "FAIL: forward sync rc=$?"\n'
        # Rollback path: the reverse diff must restore the previous tree
        # without any .git under the service tree.
        'sync_net_tree "$B" "$A" || echo "FAIL: reverse sync rc=$?"\n'
        'grep -q v1 "$REPO_NET/backend/app/main.py" '
        '&& echo "PASS: modified restored" || echo "FAIL: modified not restored"\n'
        '[ ! -e "$REPO_NET/backend/app/egress.py" ] '
        '&& echo "PASS: added removed" || echo "FAIL: added still present"\n'
        'grep -q keep "$REPO_NET/backend/app/stable.py" '
        '&& echo "PASS: deleted restored" || echo "FAIL: deleted not restored"\n'
        'grep -q SECRET=1 "$REPO_NET/backend/.env" '
        '&& echo "PASS: env preserved through rollback" || echo "FAIL: env lost"\n'
    )
    result = _run_sync_script(body)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "PASS: modified restored" in out
    assert "PASS: added removed" in out
    assert "PASS: deleted restored" in out
    assert "PASS: env preserved through rollback" in out


def test_net_sync_fails_closed_on_bad_sha_and_missing_tree():
    body = (
        "make_repo\n"
        'mkdir -p "$REPO_APP/backend"\n'
        'echo v1 > "$REPO_APP/backend/app.py"\n'
        "A=$(commit_all a)\n"
        'mkdir -p "$REPO_NET"\n'
        "rc=0\n"
        'sync_net_tree "" "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" || rc=$?\n'
        '[ "$rc" = "1" ] && echo "PASS: bad sha rc=1" || echo "FAIL: bad sha rc=$rc"\n'
        'echo "$A" | grep -q "^[0-9a-f]\\{40\\}$" && echo "PASS: sha sanity" || echo "FAIL: sha sanity"\n'
        # A missing service tree is never created blindly.
        "SAVED_NET=$REPO_NET\n"
        'REPO_NET="$ROOT/does-not-exist"\n'
        "rc=0\n"
        'sync_net_tree "" "$A" || rc=$?\n'
        'REPO_NET="$SAVED_NET"\n'
        '[ "$rc" = "1" ] && echo "PASS: missing tree rc=1" || echo "FAIL: missing tree rc=$rc"\n'
        '[ ! -e "$ROOT/does-not-exist" ] '
        '&& echo "PASS: tree not created blindly" || echo "FAIL: tree was created"\n'
    )
    result = _run_sync_script(body)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "sync: git archive of deadbeef" in out
    assert "PASS: bad sha rc=1" in out
    assert "PASS: missing tree rc=1" in out
    assert "PASS: tree not created blindly" in out


def test_rollback_without_prev_state_is_not_reported_as_success():
    body = (
        "TARGET_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "PREV_SHA=\n"
        "rc=0\n"
        "rollback || rc=$?\n"
        '[ "$rc" = "1" ] && echo "PASS: rollback rc=1" || echo "FAIL: rollback rc=$rc"\n'
        'echo "$ROLLBACK_OK" | grep -q "^0$" && echo "PASS: not marked ok" || echo "FAIL: marked ok"\n'
    )
    result = _run_sync_script(body)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "no previously deployed SHA recorded; automatic rollback not possible" in out
    assert "has no .git and never needs one" in out
    assert "PASS: rollback rc=1" in out
    assert "PASS: not marked ok" in out
    assert "rollback verified" not in out


# ---------------------------------------------------------------------------
# deploy.sh: dependency installation without pip (uv-managed server venvs)
# ---------------------------------------------------------------------------

# The server's virtualenvs are uv-managed: `uv venv` does not seed pip, which
# is exactly what failed the first automated deploy. These tests exercise
# venv_install/sync_deps against a fake installer surface: a marker-driven
# fake venv interpreter, fake uv binaries, and stubs for sudo/stat/id/command
# so installer selection and ownership are observable without privileges and
# without touching the real EC2 host. Every test gets a fresh temp ROOT.
_DEP_PRELUDE = r'''
# deploy.sh enables `set -euo pipefail` when sourced; until then only -u is
# active. Every potentially failing statement after the source line must be
# guarded (same discipline as _run_sync_script).
set -u
ROOT="__ROOT__"
# Git Bash's command search only matches POSIX-form PATH entries: a
# drive-letter temp path (C:/...) would silently hide the fake tools from
# `command -v` and let a real uv.exe on the machine win. Normalize once so
# every path below is POSIX-form (cygpath exists only in MSYS2 bash; on
# Linux ROOT is already POSIX).
case "$ROOT" in
  [A-Za-z]:/*) ROOT="$(cygpath -u "$ROOT")" ;;
esac
# Pin the real tool paths BEFORE any stub shadows them.
REAL_STAT="$(command -v stat)"
REAL_ID="$(command -v id)"
FAKEBIN="$ROOT/fakebin"
mkdir -p "$FAKEBIN" "$ROOT/uv-fake"
export UV_LOG="$ROOT/uv.log" PIP_LOG="$ROOT/pip.log" SUDO_LOG="$ROOT/sudo.log" DECOY_LOG="$ROOT/decoy.log"
: > "$UV_LOG"; : > "$PIP_LOG"; : > "$SUDO_LOG"; : > "$DECOY_LOG"
FAKE_OWNER="${FAKE_OWNER:-ubuntu}"
HAS_UV="${HAS_UV:-0}"
HAS_PIP="${HAS_PIP:-0}"
HAS_ENSUREPIP="${HAS_ENSUREPIP:-0}"

# Fake uv. The override copy lives OUTSIDE PATH and is reached only through
# UV_BIN/UV_CANDIDATES; the PATH copy is installed only when HAS_UV=1.
# Distinct log prefixes make "which uv ran" observable.
cat > "$ROOT/uv-fake/uv" <<'UV_EOF'
#!/usr/bin/env bash
printf 'UV-OVERRIDE %s\n' "$*" >> "$UV_LOG"
exit 0
UV_EOF
chmod +x "$ROOT/uv-fake/uv"
if [ "$HAS_UV" = "1" ]; then
  cat > "$FAKEBIN/uv" <<'UV_EOF'
#!/usr/bin/env bash
printf 'UV %s\n' "$*" >> "$UV_LOG"
exit 0
UV_EOF
  chmod +x "$FAKEBIN/uv"
fi

# Decoy: any accidental PATH python3 usage is logged and fails.
cat > "$FAKEBIN/python3" <<'PY_EOF'
#!/usr/bin/env bash
printf 'DECOY %s\n' "$*" >> "$DECOY_LOG"
exit 1
PY_EOF
chmod +x "$FAKEBIN/python3"

# Fake venv interpreter: pip/ensurepip behavior is driven by marker files in
# the venv root (.has-pip, .has-ensurepip); `ensurepip --upgrade` seeds pip.
cat > "$FAKEBIN/venv-python" <<'PY_EOF'
#!/usr/bin/env bash
base="$(cd "$(dirname "$0")/.." && pwd)"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
  printf 'pip %s\n' "$*" >> "$PIP_LOG"
  if [ -f "$base/.has-pip" ]; then exit 0; fi
  echo "No module named pip" >&2
  exit 1
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "ensurepip" ]; then
  if [ "${3:-}" = "--version" ]; then
    if [ -f "$base/.has-ensurepip" ]; then exit 0; fi
    echo "ensurepip is disabled in this interpreter" >&2
    exit 1
  fi
  if [ "${3:-}" = "--upgrade" ]; then
    if [ -f "$base/.has-ensurepip" ]; then touch "$base/.has-pip"; exit 0; fi
    echo "ensurepip is disabled in this interpreter" >&2
    exit 1
  fi
fi
exit 0
PY_EOF
chmod +x "$FAKEBIN/venv-python"

# Stub the system surface the installer touches. The sudo stub parses
# `sudo [-H] [-u] USER CMD...` (deploy.sh only ever uses -H -u), logs the
# target user, and runs the command directly — tests are unprivileged and
# single-user, so "as owner" is observable without real privilege switching.
sudo() {
  while [ "${1:-}" = "-H" ] || [ "${1:-}" = "-u" ]; do
    shift
  done
  printf 'sudo -u %s\n' "$1" >> "$SUDO_LOG"
  shift
  "$@"
}
stat() {
  if [ "${1:-}" = "-c" ] && [ "${2:-}" = "%U" ]; then
    printf '%s\n' "$FAKE_OWNER"
  else
    "$REAL_STAT" "$@"
  fi
}
id() { "$REAL_ID" "$@"; }
command() {
  if [ "${1:-}" = "-v" ] && [ "${2:-}" = "uv" ] && [ "$HAS_UV" != "1" ]; then
    return 1
  fi
  builtin command "$@"
}

PATH="$FAKEBIN:$PATH"
export PATH
unset UV_BIN
UV_CANDIDATES="${UV_CANDIDATES-}"

source "__DEPLOY_SH__"

# deploy.sh assigns its default server paths at source time but every
# function reads the globals at call time, so the isolated paths must be
# overridden AFTER sourcing (same as _run_sync_script).
REPO_APP="$ROOT/canonical"
REPO_NET="$ROOT/service-tree"

make_venv() {
  mkdir -p "$1/bin"
  cp "$FAKEBIN/venv-python" "$1/bin/python"
  chmod +x "$1/bin/python"
  if [ "$HAS_PIP" = "1" ]; then touch "$1/.has-pip"; fi
  if [ "$HAS_ENSUREPIP" = "1" ]; then touch "$1/.has-ensurepip"; fi
  return 0
}

printf 'pinned==1.2.3\n' > "$ROOT/requirements.txt"
'''


def _run_dep_script(body: str, env_extra: dict[str, str] | None = None, timeout: int = 120):
    """Run a bash snippet with deploy.sh sourced and the installer surface faked.

    Like _run_sync_script, but adds a fakebin layer (see _DEP_PRELUDE).
    HAS_UV / HAS_PIP / HAS_ENSUREPIP / FAKE_OWNER / UV_CANDIDATES are passed
    through the environment; log files land under the per-test temp ROOT and
    are asserted inside the snippet via PASS/FAIL markers.
    """
    import tempfile

    if not _bash_ok():
        pytest.skip("no working bash on this machine")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).as_posix()
        prelude = _DEP_PRELUDE.replace("__ROOT__", root).replace(
            "__DEPLOY_SH__", DEPLOY_SH.as_posix()
        )
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", "-c", prelude + body],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )


def test_venv_install_uses_pip_when_present():
    body = (
        'make_venv "$ROOT/venvs/app"\n'
        "rc=0\n"
        'venv_install "$ROOT/venvs/app" "$ROOT/venvs/app/bin/python" '
        '"$ROOT/requirements.txt" app || rc=$?\n'
        '[ "$rc" = "0" ] && echo "PASS: rc=0" || echo "FAIL: rc=$rc"\n'
        'grep -q "install" "$PIP_LOG" && echo "PASS: pip install ran" '
        '|| echo "FAIL: no pip install"\n'
        'if [ -s "$UV_LOG" ]; then echo "FAIL: uv was used"; '
        'else echo "PASS: uv not used"; fi\n'
    )
    result = _run_dep_script(body, {"HAS_PIP": "1"})
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "using the venv's own pip" in out


def test_venv_install_uses_uv_when_pip_missing():
    body = (
        'make_venv "$ROOT/venvs/app"\n'
        "rc=0\n"
        'venv_install "$ROOT/venvs/app" "$ROOT/venvs/app/bin/python" '
        '"$ROOT/requirements.txt" app || rc=$?\n'
        '[ "$rc" = "0" ] && echo "PASS: rc=0" || echo "FAIL: rc=$rc"\n'
        'grep -qF -- "--python $ROOT/venvs/app/bin/python" "$UV_LOG" '
        '&& echo "PASS: uv targeted the venv interpreter" '
        '|| echo "FAIL: wrong interpreter"\n'
        'grep -qF -- "-r $ROOT/requirements.txt" "$UV_LOG" '
        '&& echo "PASS: uv used the pinned requirements" '
        '|| echo "FAIL: requirements not passed"\n'
        'if [ -s "$PIP_LOG" ]; then echo "FAIL: pip probed/used"; '
        'else echo "PASS: pip untouched (uv-first order)"; fi\n'
    )
    result = _run_dep_script(body, {"HAS_UV": "1"})
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "uv at " in out


def test_venv_install_uv_bin_override_wins():
    body = (
        'UV_BIN="$ROOT/uv-fake/uv"\n'
        'make_venv "$ROOT/venvs/app"\n'
        "rc=0\n"
        'venv_install "$ROOT/venvs/app" "$ROOT/venvs/app/bin/python" '
        '"$ROOT/requirements.txt" app || rc=$?\n'
        '[ "$rc" = "0" ] && echo "PASS: rc=0" || echo "FAIL: rc=$rc"\n'
        'grep -q "UV-OVERRIDE" "$UV_LOG" '
        '&& echo "PASS: UV_BIN override used" || echo "FAIL: override not used"\n'
        'if grep -q "^UV " "$UV_LOG"; then echo "FAIL: PATH uv took precedence"; '
        'else echo "PASS: PATH uv not used"; fi\n'
    )
    result = _run_dep_script(body, {"HAS_UV": "1"})
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out


def test_venv_install_ensurepip_fallback():
    body = (
        'make_venv "$ROOT/venvs/app"\n'
        "rc=0\n"
        'venv_install "$ROOT/venvs/app" "$ROOT/venvs/app/bin/python" '
        '"$ROOT/requirements.txt" app || rc=$?\n'
        '[ "$rc" = "0" ] && echo "PASS: rc=0" || echo "FAIL: rc=$rc"\n'
        '[ -f "$ROOT/venvs/app/.has-pip" ] '
        '&& echo "PASS: ensurepip seeded pip into this venv" '
        '|| echo "FAIL: no pip after ensurepip"\n'
        'grep -q "install" "$PIP_LOG" '
        '&& echo "PASS: pip install ran after bootstrap" '
        '|| echo "FAIL: no pip install"\n'
        'if [ -s "$UV_LOG" ]; then echo "FAIL: uv was used"; '
        'else echo "PASS: uv not used"; fi\n'
    )
    result = _run_dep_script(body, {"HAS_ENSUREPIP": "1"})
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "bootstrapping pip into this venv only" in out


def test_venv_install_fails_explicitly_without_installers():
    body = (
        'make_venv "$ROOT/venvs/app"\n'
        "rc=0\n"
        'venv_install "$ROOT/venvs/app" "$ROOT/venvs/app/bin/python" '
        '"$ROOT/requirements.txt" app || rc=$?\n'
        '[ "$rc" = "1" ] && echo "PASS: rc=1" || echo "FAIL: rc=$rc"\n'
        'if grep -q "install" "$PIP_LOG"; then echo "FAIL: something was installed"; '
        'else echo "PASS: nothing installed"; fi\n'
        'if [ -s "$UV_LOG" ]; then echo "FAIL: uv ran"; else echo "PASS: uv not used"; fi\n'
        '[ -f "$ROOT/venvs/app/.has-pip" ] && echo "FAIL: ensurepip ran" '
        '|| echo "PASS: venv untouched"\n'
    )
    result = _run_dep_script(body)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "no supported installer for" in out
    assert "venvs/app/bin/python" in out
    assert "uv: not found" in out
    assert "pip: 'python -m pip' is unavailable" in out
    assert "ensurepip: unsupported" in out
    assert "never recreates the venv" in out


def test_venv_install_runs_as_the_venv_owner():
    body = (
        'make_venv "$ROOT/venvs/app"\n'
        "rc=0\n"
        'venv_install "$ROOT/venvs/app" "$ROOT/venvs/app/bin/python" '
        '"$ROOT/requirements.txt" app || rc=$?\n'
        '[ "$rc" = "0" ] && echo "PASS: rc=0" || echo "FAIL: rc=$rc"\n'
        'grep -q "sudo -u ubuntu" "$SUDO_LOG" '
        '&& echo "PASS: ran as the venv owner" || echo "FAIL: owner not honored"\n'
        'if [ -s "$UV_LOG" ]; then echo "PASS: install ran"; '
        'else echo "FAIL: nothing ran"; fi\n'
    )
    result = _run_dep_script(body, {"HAS_UV": "1", "FAKE_OWNER": "ubuntu"})
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "installing as ubuntu" in out


def test_venv_install_targets_the_requested_interpreter():
    body = (
        'make_venv "$ROOT/venvs/app"\n'
        'make_venv "$ROOT/venvs/other"\n'
        "rc=0\n"
        'venv_install "$ROOT/venvs/app" "$ROOT/venvs/app/bin/python" '
        '"$ROOT/requirements.txt" app || rc=$?\n'
        '[ "$rc" = "0" ] && echo "PASS: rc=0" || echo "FAIL: rc=$rc"\n'
        'grep -qF -- "--python $ROOT/venvs/app/bin/python" "$UV_LOG" '
        '&& echo "PASS: requested interpreter used" || echo "FAIL: wrong interpreter"\n'
        'if grep -qF -- "--python $ROOT/venvs/other/bin/python" "$UV_LOG"; '
        'then echo "FAIL: other venv touched"; '
        'else echo "PASS: other interpreter untouched"; fi\n'
        'if [ -s "$DECOY_LOG" ]; then echo "FAIL: PATH python3 was used"; '
        'else echo "PASS: PATH python3 not used"; fi\n'
    )
    result = _run_dep_script(body, {"HAS_UV": "1"})
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out


def test_find_uv_probes_known_install_locations():
    # find_uv word-splits UV_CANDIDATES (server paths never contain spaces),
    # so the fake uv must sit at a space-free path: the snippet chdirs into
    # the (spaceful) temp ROOT and publishes the candidate as a relative
    # path — mirroring the server situation where root's PATH lacks uv but
    # the owner's ~/.local/bin has it.
    body = (
        'cd "$ROOT"\n'
        'mkdir -p uv-home/.local/bin\n'
        'cp "$ROOT/uv-fake/uv" "uv-home/.local/bin/uv"\n'
        'UV_CANDIDATES="uv-home/.local/bin/uv"\n'
        'make_venv "$ROOT/venvs/app"\n'
        "rc=0\n"
        'venv_install "$ROOT/venvs/app" "$ROOT/venvs/app/bin/python" '
        '"$ROOT/requirements.txt" app || rc=$?\n'
        '[ "$rc" = "0" ] && echo "PASS: rc=0" || echo "FAIL: rc=$rc"\n'
        'grep -qF -- "--python $ROOT/venvs/app/bin/python" "$UV_LOG" '
        '&& echo "PASS: candidate-list uv used" '
        '|| echo "FAIL: candidate list not probed"\n'
        'if [ -s "$PIP_LOG" ]; then echo "FAIL: pip probed/used"; '
        'else echo "PASS: pip untouched"; fi\n'
    )
    result = _run_dep_script(body)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "uv at uv-home/.local/bin/uv" in out


def test_sync_deps_installs_into_both_venvs():
    body = (
        'VENV_APP="$ROOT/venvs/app"\n'
        'VENV_APP_PY="$ROOT/venvs/app/bin/python"\n'
        'VENV_NET_PY="$REPO_NET/backend/.venv/bin/python"\n'
        'make_venv "$ROOT/venvs/app"\n'
        'make_venv "$REPO_NET/backend/.venv"\n'
        'mkdir -p "$REPO_APP/backend" "$REPO_NET/backend"\n'
        'cp "$ROOT/requirements.txt" "$REPO_APP/backend/requirements.txt"\n'
        'cp "$ROOT/requirements.txt" "$REPO_NET/backend/requirements.txt"\n'
        "SYNC_APP_DEPS=1\n"
        "SYNC_NET_DEPS=1\n"
        "rc=0\n"
        "sync_deps || rc=$?\n"
        '[ "$rc" = "0" ] && echo "PASS: rc=0" || echo "FAIL: rc=$rc"\n'
        'grep -qF -- "--python $ROOT/venvs/app/bin/python" "$UV_LOG" '
        '&& echo "PASS: app venv targeted" || echo "FAIL: app venv missing"\n'
        'grep -qF -- "--python $REPO_NET/backend/.venv/bin/python" "$UV_LOG" '
        '&& echo "PASS: net venv targeted" || echo "FAIL: net venv missing"\n'
    )
    result = _run_dep_script(body, {"HAS_UV": "1"})
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out
    assert "syncing app venv dependencies" in out
    assert "syncing netd/egress venv dependencies" in out


def test_sync_deps_failure_stops_deployment():
    body = (
        'VENV_APP="$ROOT/venvs/app"\n'
        'VENV_APP_PY="$ROOT/venvs/app/bin/python"\n'
        "SYNC_APP_DEPS=1\n"
        "SYNC_NET_DEPS=0\n"
        'make_venv "$ROOT/venvs/app"\n'
        "rc=0\n"
        "sync_deps || rc=$?\n"
        'echo "MARKER: sync_deps returned rc=$rc (die must exit, never return)"\n'
    )
    result = _run_dep_script(body)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "FATAL: app venv dependency sync failed" in out
    assert "no supported installer for" in out
    assert "MARKER:" not in out, "die must exit the deployment, never return"


def test_sync_deps_noop_when_plan_requests_no_sync():
    body = (
        "SYNC_APP_DEPS=0\n"
        "SYNC_NET_DEPS=0\n"
        "rc=0\n"
        "sync_deps || rc=$?\n"
        '[ "$rc" = "0" ] && echo "PASS: rc=0" || echo "FAIL: rc=$rc"\n'
        'if [ -s "$UV_LOG" ] || [ -s "$PIP_LOG" ] || [ -s "$SUDO_LOG" ]; then '
        'echo "FAIL: installer ran without a sync flag"; '
        'else echo "PASS: no installs requested"; fi\n'
    )
    result = _run_dep_script(body, {"HAS_UV": "1"})
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "FAIL" not in out, out


def test_dependency_install_does_not_assume_pip(deploy_sh):
    code = _code_lines(deploy_sh)
    # The unconditional invocations that failed the first automated deploy
    # must be gone from executable code.
    assert '"$VENV_APP_PY" -m pip install' not in code
    assert '"$VENV_NET_PY" -m pip install' not in code
    for needle in (
        "venv_install",
        "find_uv",
        "UV_BIN",
        "command -v uv",
        '--python "$venv_py"',
        "-m ensurepip",
        "venv_supports_ensurepip",
    ):
        assert needle in code, f"missing: {needle}"
    # Never a user-wide or system-wide workaround.
    assert "--user" not in code
    assert "apt-get install" not in code


def test_dependency_install_precedes_migrations_and_restarts(deploy_sh):
    code = _code_lines(deploy_sh)
    main_body = code.split("main() {", 1)[1]
    order = ("sync_deps", "install_systemd_config", "run_migrations", "restart_services")
    indexes = [main_body.index(step) for step in order]
    assert indexes == sorted(indexes), (
        f"deployment order violated: {list(zip(order, indexes))}"
    )


# ---------------------------------------------------------------------------
# plan.py: restart-selection matrix
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def plan_module():
    return _load_module(PLAN_PY, "deploy_plan")


OLD = "a" * 40


def _flags(module, changed, old=OLD):
    flags, notes = module.plan(list(changed), old)
    return flags, notes


def test_first_deploy_is_conservative_full_plan(plan_module):
    flags, notes = _flags(plan_module, [], old="")
    assert all(value == 1 for value in flags.values())
    assert notes, "the conservative choice must be explained"


def test_backend_change_restarts_api_and_worker_and_migrates(plan_module):
    flags, _ = _flags(plan_module, ["backend/app/main.py"])
    assert flags["RESTART_API"] == 1
    assert flags["RESTART_WORKER"] == 1
    assert flags["RUN_MIGRATIONS"] == 1
    assert flags["RESTART_NETD"] == 0
    assert flags["RESTART_EGRESS"] == 0
    assert flags["INSTALL_SYSTEMD_CONFIG"] == 0


def test_docs_only_change_restarts_nothing(plan_module):
    flags, notes = _flags(plan_module, ["docs/SETUP.md", "README.md"])
    assert all(value == 0 for value in flags.values())
    assert notes


def test_backend_test_only_change_restarts_nothing(plan_module):
    flags, _ = _flags(plan_module, ["backend/tests/test_new_case.py"])
    assert all(value == 0 for value in flags.values())


def test_requirements_change_syncs_app_venv_only(plan_module):
    flags, _ = _flags(plan_module, ["backend/requirements.txt"])
    assert flags["SYNC_APP_DEPS"] == 1
    assert flags["SYNC_NET_DEPS"] == 0, (
        "the root daemons' venv must not be synced without a scheduled "
        "restart of the daemons"
    )
    assert flags["RESTART_API"] == 1
    assert flags["RESTART_WORKER"] == 1


def test_sandbox_change_restarts_egress_and_app_services(plan_module):
    flags, _ = _flags(plan_module, ["backend/app/sandbox/egress.py"])
    assert flags["RESTART_EGRESS"] == 1
    assert flags["RESTART_API"] == 1
    assert flags["RESTART_WORKER"] == 1
    assert flags["RESTART_NETD"] == 0


def test_sandbox_plus_requirements_syncs_net_venv(plan_module):
    flags, _ = _flags(
        plan_module,
        ["backend/app/sandbox/egress.py", "backend/requirements.txt"],
    )
    assert flags["SYNC_NET_DEPS"] == 1
    assert flags["RESTART_EGRESS"] == 1


def test_netd_source_change_reinstalls_helper_and_propagates(plan_module):
    flags, _ = _flags(plan_module, ["infra/firecracker/fixhub-netd.py"])
    assert flags["REINSTALL_NETD_HELPER"] == 1
    assert flags["RESTART_NETD"] == 1
    # api/worker drop-ins Require= fixhub-netd; systemd stops them with it.
    assert flags["RESTART_API"] == 1
    assert flags["RESTART_WORKER"] == 1


def test_systemd_change_installs_config_and_restarts_api_worker(plan_module):
    flags, _ = _flags(
        plan_module, ["infra/systemd/fixhub-api.service.d-10-netd.conf"]
    )
    assert flags["INSTALL_SYSTEMD_CONFIG"] == 1
    assert flags["RESTART_API"] == 1
    assert flags["RESTART_WORKER"] == 1


def test_frontend_change_only_flags_frontend_build(plan_module):
    flags, _ = _flags(plan_module, ["frontend/src/App.tsx"])
    assert flags["BUILD_FRONTEND"] == 1
    assert flags["RESTART_API"] == 0


def test_plan_rejects_malformed_shas(plan_module):
    assert (
        plan_module.main(["--old", "", "--new", "deadbeef", "--format", "json"])
        == 2
    )
    assert (
        plan_module.main(["--old", "zz" * 20, "--new", "", "--format", "json"])
        == 2
    )


def test_plan_sh_format_is_sourceable(plan_module):
    result = subprocess.run(
        [
            sys.executable,
            str(PLAN_PY),
            "--format",
            "sh",
            "--old",
            "",
            "--new",
            "b" * 40,
        ],
        input="backend/app/main.py\n",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        if line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert key in plan_module.FLAGS, f"unexpected key: {key}"
        assert value in ("0", "1"), f"non-boolean flag: {line}"


# ---------------------------------------------------------------------------
# drain.py: worker drain fails closed
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def drain_module():
    return _load_module(DRAIN_PY, "deploy_drain")


class _FakeWorker:
    def __init__(self, job, name: str = "worker-1"):
        self._job = job
        self.name = name

    def get_current_job(self):
        return self._job


def test_drain_reports_idle_when_no_job_running(drain_module, monkeypatch):
    monkeypatch.setattr(
        drain_module, "_collect_workers", lambda: [_FakeWorker(None)]
    )
    assert drain_module.main([]) == 0


def test_drain_reports_busy_when_job_running(drain_module, monkeypatch):
    monkeypatch.setattr(
        drain_module, "_collect_workers", lambda: [_FakeWorker(object())]
    )
    assert drain_module.main([]) == 1


def test_drain_fails_closed_when_state_unknown(drain_module, monkeypatch):
    def boom():
        raise RuntimeError("redis unreachable")

    monkeypatch.setattr(drain_module, "_collect_workers", boom)
    assert drain_module.main([]) == 2, (
        "undeterminable worker state must fail closed, never restart blindly"
    )


def test_worker_is_idle_checks_every_worker(drain_module):
    assert drain_module.worker_is_idle(
        [_FakeWorker(None), _FakeWorker(None)]
    )
    assert not drain_module.worker_is_idle(
        [_FakeWorker(None), _FakeWorker(object())]
    )
