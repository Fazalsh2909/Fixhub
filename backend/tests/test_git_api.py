"""Git branch/commit/push (local remote) + PR publisher naming + task detail API."""
import os
import subprocess

from fastapi.testclient import TestClient

from app.db.models import Task
from app.github import publisher as _pub
from app.main import app
from tests.conftest import make_user, session_cookies


def _authed(db, email="git@example.com"):
    u = make_user(db, email=email)
    return u, session_cookies(db, u)


def _git(path, *args):
    r = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r


def _repo(tmp_path, name):
    remote = tmp_path / f"{name}.git"
    _git(str(tmp_path), "init", "--bare", str(remote))
    work = tmp_path / name
    _git(str(tmp_path), "clone", str(remote), str(work))
    _git(str(work), "config", "user.email", "t@t.t")
    _git(str(work), "config", "user.name", "t")
    (work / "app.py").write_text("x = 1\n")
    _git(str(work), "add", "-A")
    _git(str(work), "commit", "-m", "init")
    _git(str(work), "push", "-u", "origin", "HEAD:main")
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=str(remote), capture_output=True, timeout=30,
    )
    return str(remote), str(work)


def test_per_task_branch_naming():
    assert _pub.FIX_BRANCH == "fixhub-fixes"
    assert _pub.branch_for_issue(12, 7) == "fixhub-fixes/issue-12-task-7"
    assert _pub.branch_for_ci("abcdef1234", 8) == "fixhub-fixes/ci-abcdef1-task-8"
    # Deterministic: same task always resolves the same branch (repairs reuse it).
    assert _pub.branch_for_issue(12, 7) == _pub.branch_for_issue(12, 7)
    assert _pub.branch_for_issue(12, 7) != _pub.branch_for_issue(12, 9)


def test_ensure_branch_creates_from_base(tmp_path):
    _, work = _repo(tmp_path, "w2")
    out = _pub.ensure_branch(work, base="main")
    assert out == {"branch": "fixhub-fixes", "created": True}
    assert _git(work, "branch", "--show-current").stdout.strip() == "fixhub-fixes"


def test_ensure_branch_reuses_remote_branch(tmp_path):
    remote, work = _repo(tmp_path, "w3")
    _git(work, "checkout", "-b", "fixhub-fixes")
    with open(os.path.join(work, "fix.txt"), "w") as fh:
        fh.write("team\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "first fix")
    _git(work, "push", "-u", "origin", "fixhub-fixes")
    # fresh clone, like every task workspace: must track-checkout, not recreate
    work2 = str(tmp_path / "w3b")
    _git(str(tmp_path), "clone", remote, work2)
    out = _pub.ensure_branch(work2, base="main")
    assert out == {"branch": "fixhub-fixes", "created": False}
    with open(os.path.join(work2, "fix.txt")) as fh:
        assert fh.read() == "team\n"


def test_checkout_ref_sha_and_bad_ref(tmp_path):
    from app.repo import workspace as _ws

    _, work = _repo(tmp_path, "w6")
    with open(os.path.join(work, "second.txt"), "w") as fh:
        fh.write("two\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "second")
    sha2 = _git(work, "rev-parse", "HEAD").stdout.strip()
    sha1 = _git(work, "rev-parse", "HEAD~1").stdout.strip()
    assert _ws.checkout_ref(work, sha1) is True
    assert _ws.head_sha(work) == sha1
    assert _ws.checkout_ref(work, sha2) is True
    assert _ws.head_sha(work) == sha2
    assert _ws.checkout_ref(work, "deadbeef" * 5) is False
    assert _ws.checkout_ref(work, "") is False


def test_ensure_branch_from_start_ref(tmp_path):
    from app.repo import workspace as _ws

    _, work = _repo(tmp_path, "w7")
    with open(os.path.join(work, "second.txt"), "w") as fh:
        fh.write("two\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "second")
    sha2 = _git(work, "rev-parse", "HEAD").stdout.strip()
    out = _pub.ensure_branch(work, branch="fixhub-fixes/ci-x-task-1", base="main", start=sha2)
    assert out["created"] is True
    parent = _git(work, "rev-parse", "fixhub-fixes/ci-x-task-1~0").stdout.strip()
    # fresh branch starts exactly at the given ref (nothing committed yet)
    assert parent == sha2


def test_publish_reuses_open_pr(monkeypatch, tmp_path):
    remote, work = _repo(tmp_path, "w4")
    with open(os.path.join(work, "app.py"), "w") as fh:
        fh.write("x = 1\ny = 2\n")
    seen = {}
    monkeypatch.setattr(_pub._gh, "list_open_pulls", lambda **k: [
        {"number": 42, "url": "http://pr/42", "head_branch": "fixhub-fixes",
         "head_sha": "abc", "body": ""}])
    monkeypatch.setattr(_pub._gh, "update_pull",
                        lambda **k: seen.update(k) or {"number": 42})

    def _boom(**k):
        raise AssertionError("must reuse the open PR, not create one")

    monkeypatch.setattr(_pub._gh, "create_pull_request", _boom)
    out = _pub.publish(path=work, full_name="acme/r", base="main",
                       title="t", body="b", token="tok")
    assert out["branch"] == "fixhub-fixes" and out["pr_number"] == 42
    assert out["pr_url"] == "http://pr/42" and seen["title"] == "t"
    r = subprocess.run(["git", "ls-remote", remote, "fixhub-fixes"],
                       capture_output=True, text=True, timeout=30)
    assert "fixhub-fixes" in r.stdout


def test_publish_creates_pr_when_none(monkeypatch, tmp_path):
    remote, work = _repo(tmp_path, "w5")
    with open(os.path.join(work, "app.py"), "w") as fh:
        fh.write("x = 1\ny = 2\n")
    monkeypatch.setattr(_pub._gh, "list_open_pulls", lambda **k: [])
    monkeypatch.setattr(_pub._gh, "create_pull_request",
                        lambda **k: {"number": 7, "url": "http://pr/7"})
    out = _pub.publish(path=work, full_name="acme/r", base="main",
                       title="t", body="b", token="tok")
    assert out["pr_number"] == 7 and out["branch"] == "fixhub-fixes"


def test_changed_files_ignores_pycache(tmp_path):
    _, work = _repo(tmp_path, "wpc")
    os.makedirs(os.path.join(work, "__pycache__"), exist_ok=True)
    with open(os.path.join(work, "__pycache__", "a.cpython-312.pyc"), "w") as fh:
        fh.write("x")
    with open(os.path.join(work, "b.py"), "w") as fh:
        fh.write("y = 2\n")
    assert _pub.changed_files(work) == ["b.py"]
    assert _pub.has_meaningful_changes(work) is True


def test_meaningful_changes_and_commit_push(tmp_path):
    _, work = _repo(tmp_path, "w1")
    assert _pub.has_meaningful_changes(work) is False
    (open(os.path.join(work, "app.py"), "a")).write("y = 2\n")
    assert _pub.has_meaningful_changes(work) is True
    assert "app.py" in _pub.changed_files(work)


def test_installed_repos_merges_connected(db, monkeypatch):
    from app.db.models import Repository
    from app.github import app_auth as _auth
    from app.github import client as _gh

    u, cookies = _authed(db)
    db.add(Repository(github_full_name="u/connected", installation_id="99", owner_id=u.id))
    db.commit()
    monkeypatch.setattr(_auth, "app_jwt", lambda: "jwt")
    monkeypatch.setattr(_auth, "installation_token", lambda iid: "tok")
    monkeypatch.setattr(_gh, "list_installations", lambda **k: [
        {"id": "99", "account": "u", "type": "User"}])
    monkeypatch.setattr(_gh, "list_installation_repos", lambda **k: [
        {"full_name": "u/connected", "private": False, "default_branch": "main"},
        {"full_name": "u/newrepo", "private": True, "default_branch": "main"},
    ])
    c = TestClient(app)
    r = c.get("/api/github/repos", cookies=cookies)
    assert r.status_code == 200, r.text
    groups = r.json()
    assert groups[0]["installation_id"] == "99"
    by_name = {x["github_full_name"]: x for x in groups[0]["repos"]}
    assert by_name["u/connected"]["connected"] is True
    assert by_name["u/newrepo"]["connected"] is False
    assert by_name["u/newrepo"]["installation_id"] == "99"


def test_installed_repos_degrades_per_installation(db, monkeypatch):
    from app.github import app_auth as _auth
    from app.github import client as _gh

    _, cookies = _authed(db, email="git2@example.com")
    monkeypatch.setattr(_auth, "app_jwt", lambda: "jwt")
    monkeypatch.setattr(_gh, "list_installations", lambda **k: [
        {"id": "1", "account": "a", "type": "User"}])
    monkeypatch.setattr(_auth, "installation_token",
                        lambda iid: (_ for _ in ()).throw(RuntimeError("nope")))
    c = TestClient(app)
    r = c.get("/api/github/repos", cookies=cookies)
    assert r.status_code == 200 and r.json()[0]["repos"] == []


def test_task_detail_api(db):
    u, cookies = _authed(db, email="git3@example.com")
    db.add(Task(repository="acme/demo", trigger_type="issue", issue_number=3, issue_title="t", status="COMPLETED",
                branch="fixhub-fixes/issue-3-x", commit_sha="abc", pr_number=9, pr_url="http://pr/9",
                owner_id=u.id))
    db.commit()
    c = TestClient(app)
    r = c.get("/api/tasks", cookies=cookies)
    assert r.status_code == 200 and r.json()[0]["pr_url"] == "http://pr/9"
    tid = r.json()[0]["id"]
    d = c.get(f"/api/tasks/{tid}", cookies=cookies)
    assert d.status_code == 200 and "memory" in d.json() and "events" in d.json()
