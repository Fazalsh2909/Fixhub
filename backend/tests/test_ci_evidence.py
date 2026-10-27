"""Regression tests for the CI evidence pipeline.

Observed failure (run 40338550350): the `check_run` context had no workflow
fields and `failing_logs_excerpt()` returned step metadata instead of logs.
These tests pin the evidence fixes and the failing-revision pinning.
"""
import inspect
import subprocess

import pytest

from app.agent import context as _ctx


def test_metadata_only_excerpt_is_not_failure_evidence():
    metadata = (
        "JOB backend conclusion=failure\n"
        "  step Set up Python conclusion=success\n"
        "  step pip install -r requirements.txt conclusion=failure"
    )
    assert _ctx.has_failure_signal(metadata) is False
    assert _ctx.has_failure_signal("check backend failed") is False
    assert _ctx.has_failure_signal("workflow CI failed on main") is False
    assert _ctx.has_failure_signal("") is False

    assert _ctx.has_failure_signal(
        "JOB backend failure signal:\n"
        "ERROR: Could not find a version that satisfies the requirement ruff\n"
    )
    assert _ctx.has_failure_signal(
        "FAILED tests/test_x.py::test_y - assert 1 == 2\n"
    )
    assert _ctx.has_failure_signal("Would reformat: apps/api/app/main.py")


def test_ci_task_message_supplies_evidence_without_nagging():
    ctx = _ctx.TaskContext(
        repository="acme/r",
        trigger_type="ci",
        ci=_ctx.CIContext(
            workflow_name="CI",
            workflow_file=".github/workflows/ci.yml",
            workflow_content="name: CI\n",
            job="backend",
            failure_logs=(
                "ERROR: Could not find a version that satisfies the requirement "
                "ruff==0.1.0\n"
            ),
        ),
    )
    msg = _ctx.build_task_message(ctx)
    assert "Failure signal:" in msg
    assert "Could not find a version" in msg
    assert "Workflow content:" in msg
    # Evidence is supplied; the prompt does not forbid inspecting it.
    assert "Do NOT read the workflow file" not in msg


def test_ci_task_message_without_logs_asks_for_reproduction():
    ctx = _ctx.TaskContext(
        repository="acme/r",
        trigger_type="ci",
        ci=_ctx.CIContext(job="backend", workflow_content="name: CI\n"),
    )
    msg = _ctx.build_task_message(ctx)
    assert "reproduce the failing step" in msg
    assert "not captured" in msg


def test_failing_logs_excerpt_returns_real_log_text(monkeypatch):
    from app.github import client as _gh

    calls = {}

    def fake_tail(**kwargs):
        calls.update(kwargs)
        return "JOB backend failure signal:\nFAILED tests/test_x.py::test_y"

    monkeypatch.setattr(_gh, "failed_log_tail", fake_tail)
    out = _gh.failing_logs_excerpt(token="t", full_name="a/b", run_id="42")
    assert "FAILED tests/test_x.py::test_y" in out
    assert calls["run_id"] == "42"


def test_enrich_ci_logs_returns_real_failure_tail(monkeypatch):
    from app.github import app_auth, client as _gh
    from app.tasks import service as _svc

    monkeypatch.setattr(app_auth, "installation_token", lambda iid: "t")
    monkeypatch.setattr(
        _gh,
        "failed_log_tail",
        lambda **kw: "JOB backend failure signal:\nFAILED tests/test_x.py::test_y",
    )
    out = _svc._enrich_ci_logs(
        repository="a/b",
        installation_id="1",
        run_id="42",
        job_name="backend",
        check_url="",
    )
    assert "FAILED tests/test_x.py::test_y" in out
    assert _ctx.has_failure_signal(out)


@pytest.mark.parametrize("bad", ["", "   "])
def test_has_failure_signal_empty(bad):
    assert _ctx.has_failure_signal(bad) is False


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _repo_with_two_revisions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "ci.yml").write_text("name: CI\n# default branch\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "default")
    default_branch = _git(repo, "branch", "--show-current").stdout.strip()
    default_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "failing-branch")
    (repo / "ci.yml").write_text("name: CI\n# failing revision\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "failing")
    failing_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", default_branch)
    return repo, default_sha, failing_sha


def test_ci_workspace_pins_the_failing_revision(tmp_path):
    """The workspace must sit on the failing commit, not the default branch.

    Production trace 40361749612: the guest was provisioned from the default
    branch, so `.github/workflows/ci.yml` did not contain the step named in
    the CI evidence and every edit was rejected.
    """
    from app.tasks import service as _svc

    repo, default_sha, failing_sha = _repo_with_two_revisions(tmp_path)
    assert _svc._pin_ci_workspace(str(repo), failing_sha)["ok"] is True
    assert _svc._pin_ci_workspace(str(repo), failing_sha)["actual"] == failing_sha
    body = (repo / "ci.yml").read_text(encoding="utf-8")
    assert "failing revision" in body
    assert "default branch" not in body


def test_ci_workspace_blocks_an_unreachable_sha(tmp_path):
    """An unreachable failing SHA is a blocked task, never a default-branch
    fallback that silently changes the evidence."""
    from app.tasks import service as _svc

    repo, default_sha, _ = _repo_with_two_revisions(tmp_path)
    pin = _svc._pin_ci_workspace(str(repo), "0" * 40)
    assert pin["ok"] is False
    assert pin["reason"] == "ci_sha_unreachable"
    assert _svc._ws.head_sha(str(repo)) == default_sha


def test_ci_checkout_precedes_guest_provision():
    """Ordering guard: provision() streams the host tree into the guest, so
    the CI checkout must happen first (or the guest gets the wrong tree)."""
    from app.tasks import service as _svc

    src = inspect.getsource(_svc.run_task_inline)
    assert src.index("_pin_ci_workspace(") < src.index("_fc.provision(")
