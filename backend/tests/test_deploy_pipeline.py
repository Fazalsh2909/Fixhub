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
- the health-check retry loop is bounded and eventually succeeds/fails.
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
    assert "rm -rf" not in code
    assert "rm -fr" not in code
    assert "rm " not in code, "deploy script should not delete anything"
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
