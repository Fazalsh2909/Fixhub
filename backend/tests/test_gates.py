"""Gate verification + stale-guard: never push red, never race a green PR."""
import json
import subprocess

from app.agent.loop import LoopResult
from app.db.models import Task, TaskEvent
from app.verify import gates as _gates


def _git(path, *args):
    r = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (args, r.stderr[-500:])
    return r


def _repo(tmp_path, name="r"):
    remote = tmp_path / f"{name}.git"
    _git(str(tmp_path), "init", "--bare", str(remote))
    work = tmp_path / name
    _git(str(tmp_path), "clone", str(remote), str(work))
    _git(str(work), "config", "user.email", "t@t.t")
    _git(str(work), "config", "user.name", "t")
    (work / "a.py").write_text("x = 1\n")
    _git(str(work), "add", "-A")
    _git(str(work), "commit", "-m", "init")
    _git(str(work), "push", "-u", "origin", "HEAD:main")
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=str(remote), capture_output=True, timeout=30)
    return str(remote)


# --- detection ---

def test_detect_pinned(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi==1\nruff==0.8.0\n")
    assert _gates.detect_ruff_version(str(tmp_path)) == "0.8.0"


def test_detect_unpinned_via_workflow(tmp_path):
    gh = tmp_path / ".github" / "workflows"
    gh.mkdir(parents=True)
    (gh / "ci.yml").write_text("run: ruff check .\n")
    assert _gates.detect_ruff_version(str(tmp_path)) == "latest"


def test_detect_absent(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi==1\n")
    assert _gates.detect_ruff_version(str(tmp_path)) is None


def test_run_gates_skips_non_python(tmp_path):
    ok, out = _gates.run_gates(str(tmp_path), ["README.md"])
    assert ok is True


def test_run_gates_pass_fail(monkeypatch, tmp_path):
    from app.sandbox import sandbox as _sb

    (tmp_path / "requirements.txt").write_text("ruff==0.8.0\n")

    class _R:
        def __init__(self, rc, out=""):
            self.returncode = rc
            self.exit_code = rc
            self.stdout = out
            self.stderr = ""
            self.truncated = False
            self.duration_ms = 1
            self.timed_out = False
            self.cwd = "."

    monkeypatch.setattr(_sb, "run_command", lambda ws, cmd, timeout_s=None: _R(0, "All checks passed"))
    ok, _ = _gates.run_gates(str(tmp_path), ["a.py"])
    assert ok is True
    monkeypatch.setattr(_sb, "run_command", lambda ws, cmd, timeout_s=None: _R(1, "Would reformat: a.py"))
    # pinned version must be in the install command
    seen = []

    def _cap(ws, cmd, timeout_s=None):
        seen.append(cmd)
        return _R(1, "Would reformat: a.py")

    monkeypatch.setattr(_sb, "run_command", _cap)
    ok, out = _gates.run_gates(str(tmp_path), ["a.py"])
    assert ok is False and "Would reformat" in out
    assert any("ruff==0.8.0" in cmd for cmd in seen)
    # portable: single-purpose commands only, no shell chaining that breaks
    # on cmd.exe (regression: `| tail` killed gates on Windows); same
    # interpreter for install and tool (regression: bare `ruff` binary
    # missing from PATH after `pip install`).
    assert all(";" not in cmd and "| tail" not in cmd for cmd in seen)
    assert all(cmd.startswith("python -m ") for cmd in seen)


# --- stale guard ---

def test_green_upstream_fix(monkeypatch):
    from app.github import client as _gh
    from app.tasks import service as _svc

    monkeypatch.setattr(_gh, "list_open_pulls", lambda **k: [
        {"number": 7, "url": "http://pr/7", "head_branch": "fixhub-fixes",
         "head_sha": "abc", "body": ""}])
    monkeypatch.setattr(_gh, "pull_files", lambda **k: ["apps/api/tests/t.py"])
    monkeypatch.setattr(_gh, "sha_check_conclusion", lambda **k: "success")
    out = _svc._green_upstream_fix(token="t", full_name="o/r",
                                   changed=["apps/api/tests/t.py"])
    assert out == "http://pr/7"


def test_green_upstream_no_overlap(monkeypatch):
    from app.github import client as _gh
    from app.tasks import service as _svc

    monkeypatch.setattr(_gh, "list_open_pulls", lambda **k: [
        {"number": 7, "url": "http://pr/7", "head_branch": "fixhub-fixes",
         "head_sha": "abc", "body": ""}])
    monkeypatch.setattr(_gh, "pull_files", lambda **k: ["other.py"])
    out = _svc._green_upstream_fix(token="t", full_name="o/r",
                                   changed=["apps/api/tests/t.py"])
    assert out == ""


def test_green_upstream_api_error(monkeypatch):
    from app.github import client as _gh
    from app.tasks import service as _svc

    def boom(**k):
        raise RuntimeError("api down")

    monkeypatch.setattr(_gh, "list_open_pulls", boom)
    assert _svc._green_upstream_fix(token="t", full_name="o/r",
                                    changed=["a.py"]) == ""


# --- service orchestration ---

def _task(db, **kw):
    from app.db.models import Repository

    db.add(Repository(github_full_name="acme/g", installation_id=""))
    db.commit()
    t = Task(repository="acme/g", trigger_type="issue", issue_number=1,
             issue_title="t", issue_body="b", status="RUNNING", **kw)
    db.add(t)
    db.commit()
    return t


def _seams(monkeypatch, svc, summary="did stuff", loop_result=None, meaningful=True):
    import app.repo.workspace as _ws

    monkeypatch.setattr(svc, "_repo_token", lambda db, task: "")
    monkeypatch.setattr(svc, "_green_upstream_fix", lambda **k: "")
    if meaningful:
        monkeypatch.setattr(svc._pub, "has_meaningful_changes", lambda path: True)
    monkeypatch.setattr(svc._pub, "changed_files", lambda path: ["a.py"])
    monkeypatch.setattr(_ws, "head_sha", lambda path: "abc123")
    monkeypatch.setattr(svc, "_update_memory", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_cleanup", lambda task_id: None)
    res = loop_result or LoopResult(True, summary, 2, 1, [])
    monkeypatch.setattr(svc._loop, "run_agent", lambda **k: res)


def test_gate_persistent_red_needs_review(db, tmp_path, monkeypatch):
    from app.tasks import service as _svc
    from app.verify import gates as _gatesmod

    remote = _repo(tmp_path)
    t = _task(db)
    _seams(monkeypatch, _svc)
    monkeypatch.setattr(_gatesmod, "run_gates", lambda ws, files: (False, "Would reformat: a.py"))
    out = _svc.run_task_inline(t.id, source=remote)
    assert out["status"] == "NEEDS_REVIEW", out
    assert out["branch"] == f"fixhub-fixes/issue-1-task-{t.id}"
    db.expire_all()
    assert db.query(Task).filter(Task.id == t.id).first().status == "NEEDS_REVIEW"


def test_gate_fix_round_publishes(db, tmp_path, monkeypatch):
    from app.tasks import service as _svc
    from app.verify import gates as _gatesmod

    remote = _repo(tmp_path)
    t = _task(db)
    _seams(monkeypatch, _svc)
    calls = {"n": 0}

    def fake_gates(ws, files):
        calls["n"] += 1
        return (False, "Would reformat") if calls["n"] == 1 else (True, "clean")

    monkeypatch.setattr(_gatesmod, "run_gates", fake_gates)
    pushed = {}
    monkeypatch.setattr(_svc, "_local_commit_push",
                        lambda path, branch, title, start="": pushed.update(branch=branch))
    out = _svc.run_task_inline(t.id, source=remote)
    assert out["status"] == "COMPLETED", out
    assert pushed["branch"] == f"fixhub-fixes/issue-1-task-{t.id}"
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "COMPLETED" and row.branch == pushed["branch"]


def test_crash_with_changes_needs_review(db, tmp_path, monkeypatch):
    import os

    from app.tasks import service as _svc

    remote = _repo(tmp_path)
    t = _task(db)
    _seams(monkeypatch, _svc, meaningful=False)

    def boom(**k):
        # Simulate a productive run that dies: leave a real change behind.
        ws = _svc._ws.workspace_path(t.id)
        with open(os.path.join(ws, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 2\n")
        raise RuntimeError("Server disconnected without sending a response.")

    monkeypatch.setattr(_svc._loop, "run_agent", boom)
    out = _svc.run_task_inline(t.id, source=remote)
    assert out["status"] == "NEEDS_REVIEW", out
    assert out["branch"] == f"fixhub-fixes/issue-1-task-{t.id}"
    # workspace kept for review
    assert os.path.isdir(_svc._ws.workspace_path(t.id))


def test_crash_without_changes_failed(db, tmp_path, monkeypatch):
    from app.tasks import service as _svc

    remote = _repo(tmp_path)
    t = _task(db)
    _seams(monkeypatch, _svc, meaningful=False)
    monkeypatch.setattr(_svc._loop, "run_agent",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = _svc.run_task_inline(t.id, source=remote)
    assert out["status"] == "FAILED", out


def test_likely_paths():
    from app.tasks.service import _likely_paths

    assert _likely_paths("Would reformat: tests/test_a.py\nE501 x/y.py:10") == [
        "tests/test_a.py", "x/y.py"]
    assert _likely_paths("no files here") == []
    assert _likely_paths("") == []


def test_pre_agent_supersede_skips_run(db, tmp_path, monkeypatch):
    from app.tasks import service as _svc

    t = Task(repository="acme/g", trigger_type="ci", ci_sha="sha1", ci_job="backend",
             ci_excerpt="Would reformat: tests/t.py", status="RUNNING")
    db.add(t)
    db.commit()
    monkeypatch.setattr(_svc, "_repo_token", lambda db, task: "tok")
    monkeypatch.setattr(_svc, "_green_upstream_fix", lambda **k: "http://pr/7")

    def _boom(**k):
        raise AssertionError("agent must not run when superseded")

    monkeypatch.setattr(_svc._loop, "run_agent", _boom)
    out = _svc.run_task_inline(t.id, source="unused")
    assert out["status"] == "COMPLETED" and out.get("superseded_by") == "http://pr/7", out
    db.expire_all()
    row = db.query(Task).filter(Task.id == t.id).first()
    assert row.status == "COMPLETED"


def _repo2(tmp_path):
    """Two-commit repo; returns (remote, sha1, sha2)."""
    import os

    remote = _repo(tmp_path)
    # _repo leaves a `r` workdir clone; add a second commit through a fresh path.
    work = os.path.join(str(tmp_path), "r")
    with open(os.path.join(work, "second.txt"), "w") as fh:
        fh.write("two\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "second")
    _git(work, "push", "-u", "origin", "HEAD:main")
    sha2 = _git(work, "rev-parse", "HEAD").stdout.strip()
    sha1 = _git(work, "rev-parse", "HEAD~1").stdout.strip()
    return remote, sha1, sha2


def _ci_task(db, sha):
    from app.db.models import Repository

    db.add(Repository(github_full_name="acme/g", installation_id=""))
    db.commit()
    t = Task(repository="acme/g", trigger_type="ci", ci_sha=sha, ci_job="backend",
             ci_workflow="ci", ci_url="http://ci/1", ci_excerpt="backend failed",
             status="RUNNING")
    db.add(t)
    db.commit()
    return t


def test_ci_checks_out_failing_sha(db, tmp_path, monkeypatch):
    import os

    from app.tasks import service as _svc
    from app.verify import gates as _gatesmod

    remote, sha1, sha2 = _repo2(tmp_path)
    t = _ci_task(db, sha2)
    _seams(monkeypatch, _svc)
    monkeypatch.setattr(_gatesmod, "run_gates", lambda ws, files: (True, "clean"))

    def _write_then_finish(**k):
        import app.repo.workspace as _ws

        path = _ws.workspace_path(t.id)
        with open(os.path.join(path, "fix.txt"), "w", encoding="utf-8") as fh:
            fh.write("fixed\n")
        from app.agent.loop import LoopResult

        return LoopResult(True, "fixed it", 2, 1, [])

    monkeypatch.setattr(_svc._loop, "run_agent", _write_then_finish)
    pushed = {}
    monkeypatch.setattr(_svc, "_local_commit_push",
                        lambda path, branch, title, start="": pushed.update(branch=branch, start=start))
    out = _svc.run_task_inline(t.id, source=remote, auto_publish=False)
    assert out["status"] == "NEEDS_REVIEW", out
    # Workspace kept: HEAD must be the failing commit, not main-tip drift.
    import subprocess as _sp

    ws_path = _svc._ws.workspace_path(t.id)
    head = _sp.run(["git", "rev-parse", "HEAD"], cwd=ws_path,
                   capture_output=True, text=True, timeout=30).stdout.strip()
    assert head == sha2, head
    # Fix branch was created from the failing sha.
    assert pushed == {}  # auto_publish=False: no publish yet
    db.expire_all()
    types = [e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == t.id).all()]
    assert "CI_CHECKOUT" in types


def test_ci_unreachable_sha_falls_back(db, tmp_path, monkeypatch):
    from app.tasks import service as _svc
    from app.verify import gates as _gatesmod

    remote = _repo(tmp_path)
    t = _ci_task(db, "0" * 40)
    _seams(monkeypatch, _svc, meaningful=False)
    monkeypatch.setattr(_gatesmod, "run_gates", lambda ws, files: (True, "clean"))
    monkeypatch.setattr(_svc._loop, "run_agent",
                        lambda **k: __import__("app.agent.loop", fromlist=["LoopResult"]).LoopResult(
                            True, "nothing", 1, 0, []))
    out = _svc.run_task_inline(t.id, source=remote, auto_publish=False)
    # No changes -> COMPLETED, but must not BLOCK on the bad sha.
    assert out["status"] == "COMPLETED", out
    db.expire_all()
    types = [e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == t.id).all()]
    assert "CI_CHECKOUT" in types


def test_superseded_when_green_upstream(db, tmp_path, monkeypatch):
    from app.tasks import service as _svc

    remote = _repo(tmp_path)
    t = _task(db)
    _seams(monkeypatch, _svc)
    from app.verify import gates as _gatesmod

    monkeypatch.setattr(_gatesmod, "run_gates", lambda ws, files: (True, "clean"))
    monkeypatch.setattr(_svc, "_green_upstream_fix", lambda **k: "http://pr/7")
    out = _svc.run_task_inline(t.id, source=remote)
    assert out["status"] == "COMPLETED" and out.get("superseded_by") == "http://pr/7", out
    db.expire_all()
    evs = [e.type for e in db.query(_svc.TaskEvent).filter(_svc.TaskEvent.task_id == t.id).all()]
    assert "SUPERSEDED" in evs
