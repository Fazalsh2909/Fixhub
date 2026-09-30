"""Same-repository concurrency isolation: ONE TASK = ONE WORKSPACE = ONE BRANCH = ONE PR.

- Two tasks for one repo must not share workspace, branch, commits, or PRs.
- Repair rounds reuse the same task's branch/PR (no second PR).
- A branch owned by another active task fails loudly, never last-writer-wins.
- Workspaces are cleaned up on terminal states.
"""
import os
import subprocess

import pytest

from app.agent.loop import LoopResult
from app.db.models import Repository, Task, TaskEvent


def _git(path, *args):
    r = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (args, r.stderr[-500:])
    return r


@pytest.fixture()
def remote(tmp_path):
    bare = tmp_path / "repo.git"
    _git(str(tmp_path), "init", "--bare", str(bare))
    work = tmp_path / "seed"
    _git(str(tmp_path), "clone", str(bare), str(work))
    _git(str(work), "config", "user.email", "t@t.t")
    _git(str(work), "config", "user.name", "t")
    (work / "a.py").write_text("x = 1\n")
    _git(str(work), "add", "-A")
    _git(str(work), "commit", "-m", "init")
    _git(str(work), "push", "-u", "origin", "HEAD:main")
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=str(bare), capture_output=True, timeout=30)
    return str(bare)


def _add_repo(db, full_name="acme/cc", installation_id=""):
    db.add(Repository(github_full_name=full_name, installation_id=installation_id))
    db.commit()
    return db.query(Repository).filter(Repository.github_full_name == full_name).first()


def _add_task(db, repo_row, **kw):
    kw.setdefault("repository", repo_row.github_full_name)
    kw.setdefault("repository_id", repo_row.id)
    kw.setdefault("trigger_type", "issue")
    kw.setdefault("status", "RUNNING")
    t = Task(**kw)
    db.add(t)
    db.commit()
    return t


def _write_then_finish(monkeypatch, svc, ws_mod, task_id, filename, content):
    """Mocked agent run that writes one file into its own fresh workspace."""
    from app.agent import loop as _loop

    def _fake_run_agent(**k):
        path = ws_mod.workspace_path(task_id)
        with open(os.path.join(path, filename), "w", encoding="utf-8") as fh:
            fh.write(content)
        return LoopResult(True, f"wrote {filename}", 2, 1, [])

    monkeypatch.setattr(_loop, "run_agent", _fake_run_agent)


def _show(remote, branch, path):
    """True if path exists on branch; False if missing (never raises)."""
    r = subprocess.run(["git", "show", f"{branch}:{path}"], cwd=remote,
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def test_two_tasks_same_repo_fully_isolated(db, tmp_path, monkeypatch, remote):
    from app.config import settings as _settings
    from app.tasks import service as _svc

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    repo = _add_repo(db)
    ta = _add_task(db, repo, issue_number=1, issue_title="fix A", issue_body="a")
    tb = _add_task(db, repo, issue_number=2, issue_title="fix B", issue_body="b")

    import app.repo.workspace as _ws

    _write_then_finish(monkeypatch, _svc, _ws, ta.id, "only_a.txt", "A\n")
    out_a = _svc.run_task_inline(ta.id, source=remote)
    assert out_a["status"] == "COMPLETED", out_a

    _write_then_finish(monkeypatch, _svc, _ws, tb.id, "only_b.txt", "B\n")
    out_b = _svc.run_task_inline(tb.id, source=remote)
    assert out_b["status"] == "COMPLETED", out_b

    assert out_a["branch"] != out_b["branch"]
    assert out_a["branch"] == f"fixhub-fixes/issue-1-task-{ta.id}"
    assert out_b["branch"] == f"fixhub-fixes/issue-2-task-{tb.id}"
    assert out_a["commit"] != out_b["commit"]

    # Cross-absence on the remote: A's branch lacks B's file and vice versa.
    assert _show(remote, out_a["branch"], "only_a.txt")
    assert not _show(remote, out_a["branch"], "only_b.txt")
    assert _show(remote, out_b["branch"], "only_b.txt")
    assert not _show(remote, out_b["branch"], "only_a.txt")

    # Workspaces cleaned up on terminal states.
    assert not os.path.exists(_ws.workspace_path(ta.id))
    assert not os.path.exists(_ws.workspace_path(tb.id))


def test_issue_and_ci_tasks_get_distinct_branches(db, tmp_path, monkeypatch, remote):
    from app.config import settings as _settings
    from app.tasks import service as _svc

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    repo = _add_repo(db)
    ta = _add_task(db, repo, issue_number=123, issue_title="i", issue_body="b")
    tb = _add_task(db, repo, trigger_type="ci", ci_job="backend",
                   ci_workflow="ci", ci_sha="abcdef123456", ci_url="http://ci/1",
                   ci_excerpt="backend failed")

    import app.repo.workspace as _ws

    _write_then_finish(monkeypatch, _svc, _ws, ta.id, "a.txt", "A\n")
    out_a = _svc.run_task_inline(ta.id, source=remote)
    _write_then_finish(monkeypatch, _svc, _ws, tb.id, "b.txt", "B\n")
    out_b = _svc.run_task_inline(tb.id, source=remote)

    assert out_a["branch"] == f"fixhub-fixes/issue-123-task-{ta.id}"
    assert out_b["branch"] == f"fixhub-fixes/ci-abcdef1-task-{tb.id}"
    assert out_a["branch"] != out_b["branch"]


def test_repair_reuses_branch_and_pr(db, tmp_path, monkeypatch, remote):
    from app.config import settings as _settings
    from app.github import app_auth as _app_auth
    from app.github import client as _gh
    from app.tasks import service as _svc

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    repo = _add_repo(db, installation_id="inst-1")
    t = _add_task(db, repo, issue_number=9, issue_title="i", issue_body="b")

    monkeypatch.setattr(_app_auth, "installation_token", lambda iid: "tok")
    created = []
    monkeypatch.setattr(_gh, "list_open_pulls", lambda **k: [] if not created else [
        {"number": 11, "url": "http://pr/11",
         "head_branch": f"fixhub-fixes/issue-9-task-{t.id}",
         "head_sha": "x", "body": ""}])
    monkeypatch.setattr(_gh, "create_pull_request",
                        lambda **k: created.append(k) or {"number": 11, "url": "http://pr/11"})
    monkeypatch.setattr(_gh, "update_pull", lambda **k: {"number": 11})
    monkeypatch.setattr(_gh, "sha_check_conclusion", lambda **k: "success")

    import app.repo.workspace as _ws

    _write_then_finish(monkeypatch, _svc, _ws, t.id, "fix.txt", "v1\n")
    out1 = _svc.run_task_inline(t.id, source=remote)
    assert out1["status"] == "AWAITING_CI", out1
    assert len(created) == 1 and created[0]["head"] == f"fixhub-fixes/issue-9-task-{t.id}"

    # Repair round on the same task: same branch, PR updated not created.
    _write_then_finish(monkeypatch, _svc, _ws, t.id, "fix.txt", "v2\n")
    out2 = _svc.run_task_inline(t.id, source=remote, repair=True)
    assert out2["branch"] == out1["branch"], (out1, out2)
    assert len(created) == 1, "repair must not create a second PR"
    assert out2["pr"] == "http://pr/11"


def test_branch_collision_fails_loudly(db, tmp_path, monkeypatch, remote):
    from app.config import settings as _settings
    from app.tasks import service as _svc

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    repo = _add_repo(db)
    ty = _add_task(db, repo, issue_number=1, issue_title="victim", issue_body="b")
    # Squatter: another ACTIVE task claiming Y's future branch.
    clash_branch = f"fixhub-fixes/issue-1-task-{ty.id}"
    _add_task(db, repo, issue_number=2, issue_title="squatter", issue_body="b",
              status="RUNNING", branch=clash_branch)

    import app.repo.workspace as _ws

    _write_then_finish(monkeypatch, _svc, _ws, ty.id, "v.txt", "v\n")
    out = _svc.run_task_inline(ty.id, source=remote)
    assert out["status"] == "FAILED", out
    assert "collision" in out["error"].lower()
    db.expire_all()
    types = [e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == ty.id).all()]
    assert "FAILED" in types
