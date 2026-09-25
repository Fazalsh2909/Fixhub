"""Simple-flow tests: issue → agent → diff → branch/commit → PR.

No LLM, no Docker, no verification gates. The fake agent edits files
directly in the provided worktree (same contract as mini-SWE-agent:
work in /work, FixHub publishes).
"""

import subprocess
import uuid
from pathlib import Path

import pytest

from app.agent.issue_worker import run_issue
from app.db import SessionLocal, init_db
from app.models import Patch, PullRequest, Repository, Task, TaskEvent


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    assert p.returncode == 0, f"git {' '.join(args)} failed: {p.stderr[-500:]}"
    return p.stdout.strip()


@pytest.fixture()
def base_repo(tmp_path: Path) -> Path:
    base = tmp_path / "base"
    base.mkdir()
    (base / "calc.py").write_text(
        "def total(a, b):\n    return a + b + 1\n", encoding="utf-8"
    )
    (base / "tests").mkdir()
    (base / "tests" / "test_total.py").write_text(
        "from calc import total\n\ndef test_total():\n    assert total(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "t@t.t")
    _git(base, "config", "user.name", "t")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "init")
    return base


def _make_run(base: Path, title="total() off by one", issue_number=1):
    init_db()
    db = SessionLocal()
    name = f"test/simple-{uuid.uuid4().hex[:8]}"
    branch = _git(base, "branch", "--show-current") or "main"
    repo = Repository(
        full_name=name,
        connected=True,
        local_path=str(base),
        default_branch=branch,
        installation_id="",  # local-only: no GitHub push, honest local commit
    )
    db.add(repo)
    db.flush()
    task = Task(
        repo_id=repo.id, issue_number=issue_number, title=title, state="RUNNING"
    )
    db.add(task)
    db.commit()
    task_id, repo_id = task.id, repo.id
    base_sha = _git(base, "rev-parse", "HEAD")
    db.close()
    return task_id, repo_id, name, base_sha


def _cleanup(task_id: int, repo_name: str):
    from app.repo.workspaces import remove_workspace, task_workspace_dir

    try:
        remove_workspace(task_workspace_dir(task_id))
    except Exception:
        pass
    db = SessionLocal()
    try:
        db.query(TaskEvent).filter_by(task_id=task_id).delete(synchronize_session=False)
        db.query(Patch).filter_by(task_id=task_id).delete(synchronize_session=False)
        db.query(PullRequest).filter_by(task_id=task_id).delete(
            synchronize_session=False
        )
        db.query(Task).filter_by(id=task_id).delete(synchronize_session=False)
        db.query(Repository).filter_by(full_name=repo_name).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


class _FakeStep:
    def __init__(self, command, returncode=0):
        self.step = 1
        self.command = command
        self.returncode = returncode
        self.output_tail = ""


class _FakeResult:
    def __init__(self, diff, changed, steps):
        self.exit_status = "Submitted"
        self.diff = diff
        self.changed_files = changed
        self.steps = steps


def _fixing_agent(workdir: Path, prompt: str):
    assert "total() off by one" in prompt
    assert "FixHub will publish your changes" in prompt
    (workdir / "calc.py").write_text(
        "def total(a, b):\n    return a + b\n", encoding="utf-8"
    )
    from app.repo.workspaces import git_diff_all

    diff = git_diff_all(workdir)
    return _FakeResult(diff, ["calc.py"], [_FakeStep("python -m pytest -q", 0)])


def test_issue_to_fix_to_pr_end_to_end(base_repo: Path):
    """MAIN TEST: agent edits code → diff → branch → commit → PR record."""
    from app.agent.issue_worker import run_issue

    task_id, _, repo_name, base_sha = _make_run(base_repo)
    try:
        out = run_issue(task_id, _agent_runner=_fixing_agent)
        assert out["state"] == "COMPLETED", out
        assert "calc.py" in out["files_changed"]
        assert out["commit_sha"]
        assert out["branch"].startswith("fix/issue-1")

        # Branch + commit exist in the isolated workspace.
        from app.repo.workspaces import task_workspace_dir

        ws = task_workspace_dir(task_id)
        branches = _git(ws, "branch", "--list", out["branch"])
        assert out["branch"] in branches
        msg = _git(ws, "log", "-1", "--format=%s", out["branch"])
        assert "total() off by one" in msg

        # PR record exists with the real commit SHA.
        db = SessionLocal()
        try:
            pr = db.query(PullRequest).filter_by(task_id=task_id).first()
            assert pr is not None and pr.commit_sha == out["commit_sha"]
            task = db.query(Task).filter_by(id=task_id).first()
            assert task.state == "COMPLETED"
            stages = {
                e.stage for e in db.query(TaskEvent).filter_by(task_id=task_id).all()
            }
            assert "AGENT" in stages and "DIFF" in stages
            # No chain-of-thought stored in events.
            blob = " ".join(
                e.message for e in db.query(TaskEvent).filter_by(task_id=task_id).all()
            )
            assert "PRIVATE" not in blob
        finally:
            db.close()

        # Default branch NOT mutated: base still has the bug.
        assert _git(base_repo, "rev-parse", "HEAD") == base_sha
        assert "a + b + 1" in (base_repo / "calc.py").read_text()
        assert "a + b + 1" not in (ws / "calc.py").read_text()
    finally:
        _cleanup(task_id, repo_name)


def test_insufficient_info_submission_exits_fast_without_pr(base_repo: Path):
    """Thin issue: agent submits INSUFFICIENT_INFO → COMPLETED, no PR."""
    from app.agent.issue_worker import run_issue

    def _seam(workdir: Path, prompt: str):
        assert "INSUFFICIENT_INFO" in prompt
        r = _FakeResult("", [], [])
        r.submission = "INSUFFICIENT_INFO: no actionable description"
        return r

    task_id, _, repo_name, _ = _make_run(base_repo)
    try:
        out = run_issue(task_id, _agent_runner=_seam)
        assert out["state"] == "COMPLETED"
        assert "insufficient" in out["note"].lower()
        db = SessionLocal()
        try:
            assert db.query(PullRequest).filter_by(task_id=task_id).count() == 0
        finally:
            db.close()
    finally:
        _cleanup(task_id, repo_name)


def test_limits_exceeded_without_edits_explains_itself(base_repo: Path):
    from app.agent.issue_worker import run_issue

    def _wandering_agent(workdir: Path, prompt: str):
        r = _FakeResult("", [], [_FakeStep("ls /work", 0)])
        r.exit_status = "LimitsExceeded"
        return r

    task_id, _, repo_name, _ = _make_run(base_repo)
    try:
        out = run_issue(task_id, _agent_runner=_wandering_agent)
        assert out["state"] == "COMPLETED"
        assert "step limit" in out["note"]
    finally:
        _cleanup(task_id, repo_name)


def _make_installed_run(base: Path, issue_number=11):
    """Run with a GitHub installation attached (ask-back eligible)."""
    init_db()
    db = SessionLocal()
    name = f"test/installed-{uuid.uuid4().hex[:8]}"
    branch = _git(base, "branch", "--show-current") or "main"
    repo = Repository(
        full_name=name,
        connected=True,
        local_path=str(base),
        default_branch=branch,
        installation_id="inst-123",
    )
    db.add(repo)
    db.flush()
    task = Task(
        repo_id=repo.id,
        issue_number=issue_number,
        title="mystery bug",
        state="RUNNING",
    )
    db.add(task)
    db.commit()
    task_id = task.id
    db.close()
    return task_id, name


def test_ask_back_posts_clarifying_comment_once(monkeypatch, base_repo: Path):
    """No-diff run with installation → one ask-back comment, COMPLETED."""
    import app.agent.issue_worker as iw

    posted: list[dict] = []
    # No network in tests: skip enrichment, stub token + comment POST.
    monkeypatch.setattr(iw, "_enrich_context", lambda db, task, repo, body: "")
    import app.github.publisher as pub

    monkeypatch.setattr(
        "app.github.app_auth.get_installation_token", lambda inst: "tok"
    )
    monkeypatch.setattr(
        pub,
        "post_issue_comment",
        lambda token, full, num, body: posted.append(
            {"token": token, "full": full, "num": num, "body": body}
        )
        or {"html_url": "http://x/comment/1"},
    )

    def _silent_agent(workdir: Path, prompt: str):
        return _FakeResult("", [], [])

    task_id, repo_name = _make_installed_run(base_repo)
    try:
        out = run_issue(task_id, _agent_runner=_silent_agent)
        assert out["state"] == "COMPLETED"
        assert len(posted) == 1
        assert posted[0]["num"] == 11
        assert "fixhub-clarify" in posted[0]["body"]
        assert "PRIVATE" not in posted[0]["body"]
        assert out.get("comment_url") == "http://x/comment/1"
        db = SessionLocal()
        try:
            stages = [
                e.stage for e in db.query(TaskEvent).filter_by(task_id=task_id).all()
            ]
            assert stages.count("COMMENT_POSTED") == 1
        finally:
            db.close()
    finally:
        _cleanup(task_id, repo_name)


def test_ask_back_skipped_without_installation(base_repo: Path):
    """No installation → no comment possible, plain COMPLETED."""

    def _silent_agent(workdir: Path, prompt: str):
        return _FakeResult("", [], [])

    task_id, _, repo_name, _ = _make_run(base_repo)
    try:
        out = run_issue(task_id, _agent_runner=_silent_agent)
        assert out["state"] == "COMPLETED"
        assert "comment_url" not in out
    finally:
        _cleanup(task_id, repo_name)


def test_ask_back_failure_still_completes(monkeypatch, base_repo: Path):
    """Comment POST failing must not fail the run."""
    import app.agent.issue_worker as iw
    import app.github.publisher as pub

    monkeypatch.setattr(iw, "_enrich_context", lambda db, task, repo, body: "")
    monkeypatch.setattr(
        "app.github.app_auth.get_installation_token", lambda inst: "tok"
    )

    def _boom(token, full, num, body):
        raise RuntimeError("github down")

    monkeypatch.setattr(pub, "post_issue_comment", _boom)

    def _silent_agent(workdir: Path, prompt: str):
        return _FakeResult("", [], [])

    task_id, repo_name = _make_installed_run(base_repo)
    try:
        out = run_issue(task_id, _agent_runner=_silent_agent)
        assert out["state"] == "COMPLETED"
    finally:
        _cleanup(task_id, repo_name)


def test_no_diff_means_completed_without_pr(base_repo: Path):
    from app.agent.issue_worker import run_issue

    def _lazy_agent(workdir: Path, prompt: str):
        return _FakeResult("", [], [])

    task_id, _, repo_name, _ = _make_run(base_repo)
    try:
        out = run_issue(task_id, _agent_runner=_lazy_agent)
        assert out["state"] == "COMPLETED"
        assert out.get("note") == "No code changes produced."
        db = SessionLocal()
        try:
            assert db.query(PullRequest).filter_by(task_id=task_id).count() == 0
        finally:
            db.close()
    finally:
        _cleanup(task_id, repo_name)


def test_publish_refuses_empty_diff_and_default_branch(tmp_path: Path):
    import pytest as _pytest

    from app.db import SessionLocal as _S
    from app.db import init_db as _init
    from app.github.publisher import (
        PolicyDeniedError,
        _create_simple_pr,
        build_simple_pr_body,
        publish_issue_fix,
    )
    from app.models import Repository as _R
    from app.models import Task as _T

    _init()
    base = tmp_path / "b"
    base.mkdir()
    (base / "f.py").write_text("x = 1\n", encoding="utf-8")
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "t@t.t")
    _git(base, "config", "user.name", "t")
    _git(base, "add", ".")
    _git(base, "commit", "-qm", "init")

    db = _S()
    repo = _R(full_name=f"test/pub-{uuid.uuid4().hex[:6]}", default_branch="main")
    db.add(repo)
    db.flush()
    task = _T(repo_id=repo.id, issue_number=5, title="t", state="RUNNING")
    db.add(task)
    db.commit()
    # Empty workspace → PolicyDeniedError, never a PR.
    with _pytest.raises(PolicyDeniedError):
        publish_issue_fix(db, task, repo, base)
    # PR helper refuses default-branch targets.
    with _pytest.raises(PolicyDeniedError):
        _create_simple_pr("tok", "a/b", "main", "main", "t", "b")
    # PR body never fabricates test results.
    title, body = build_simple_pr_body(
        issue_number=5, issue_title="t", changed_files=["f.py"], tests_note=""
    )
    assert "Closes #5" in body
    assert "Not reported by agent." in body
    assert "All tests passed" not in body
    assert title.startswith("Fix #5")
    db.close()


def test_run_task_sync_delegates_everything_to_simple_path(monkeypatch):
    """Legacy states (CREATED/FAILED/...) all route to run_issue, never the
    old state machine."""
    from app import automation

    calls: list[int] = []
    monkeypatch.setattr(
        "app.agent.issue_worker.run_issue",
        lambda rid, **k: calls.append(rid) or {"run_id": rid, "state": "COMPLETED"},
    )
    init_db()
    db = SessionLocal()
    task = Task(repo_id=999999, issue_number=3, title="old row", state="CREATED")
    db.add(task)
    db.commit()
    task_id = task.id
    db.close()
    try:
        out = automation.run_task_sync(task_id, force=True)
        assert out["state"] == "COMPLETED"
        assert calls == [task_id]
    finally:
        _cleanup(task_id, f"never-created-{task_id}")


def test_isolated_workspaces_do_not_crosstalk(base_repo: Path):
    from app.repo.workspaces import create_task_workspace, remove_workspace

    ws_a, _ = create_task_workspace(base_repo, 910101)
    ws_b, _ = create_task_workspace(base_repo, 910102)
    try:
        assert ws_a != ws_b
        (ws_a / "calc.py").write_text("edited\n", encoding="utf-8")
        assert (ws_b / "calc.py").read_text() != "edited\n"
        assert (base_repo / "calc.py").read_text() != "edited\n"
    finally:
        remove_workspace(ws_a)
        remove_workspace(ws_b)


def test_command_policy_denies_publish_and_shell(tmp_path: Path):
    import subprocess as _sp

    from app.tools.command_policy import evaluate

    argv, _ = evaluate("git status")
    assert argv is not None
    assert evaluate("git push origin main")[0] is None
    assert evaluate("git commit -m x")[0] is None
    assert evaluate("pytest -q; rm -rf /")[0] is None
    assert evaluate("curl https://example.com")[0] is None
    # No shell=True execution anywhere in the agent command path
    # (docstrings may mention the words; code must not pass shell=True).
    import re as _re

    root = Path(__file__).resolve().parents[1] / "app" / "agent"
    sandbox = Path(__file__).resolve().parents[1] / "app" / "sandbox"
    for d in (root, sandbox):
        for f in d.glob("*.py"):
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if _re.search(r"shell\s*=\s*True", line) and _re.search(
                    r"subprocess|\.run\(|Popen|check_output|check_call", line
                ):
                    raise AssertionError(f"{f.name}:{i}: shell=True execution")
    # Sanity check helper runs with argv (no shell) — smoke only.
    p = _sp.run(
        ["git", "status"], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert isinstance(p.returncode, int)
