"""Read-only repo browser (contents/file) + published-diff endpoints."""
from fastapi.testclient import TestClient

from app.db.models import Repository, Task
from app.main import app
from tests.conftest import make_user, session_cookies


def _authed(db, email="rb@example.com"):
    u = make_user(db, email=email)
    return u, session_cookies(db, u)


def _connected(db, user, full_name="u/demo"):
    db.add(Repository(github_full_name=full_name, installation_id="99", owner_id=user.id))
    db.commit()


def _no_github(monkeypatch):
    from app.github import app_auth as _auth

    monkeypatch.setattr(_auth, "installation_token", lambda iid: "tok")


def test_contents_lists_directory(db, monkeypatch):
    from app.github import client as _gh

    u, cookies = _authed(db)
    _connected(db, u)
    _no_github(monkeypatch)
    monkeypatch.setattr(_gh, "repo_dir_contents", lambda **k: [
        {"name": "app.py", "type": "file", "size": 10, "sha": "a"},
        {"name": "tests", "type": "dir", "size": 0, "sha": "b"},
    ])
    c = TestClient(app)
    r = c.get("/api/github/contents", params={"repo": "u/demo", "path": ".", "ref": "main"},
              cookies=cookies)
    assert r.status_code == 200, r.text
    assert [e["name"] for e in r.json()] == ["app.py", "tests"]


def test_contents_rejects_unconnected_repo(db):
    _, cookies = _authed(db)
    c = TestClient(app)
    r = c.get("/api/github/contents", params={"repo": "u/ghost"}, cookies=cookies)
    assert r.status_code == 404


def test_contents_maps_value_error_to_400(db, monkeypatch):
    from app.github import client as _gh

    u, cookies = _authed(db)
    _connected(db, u)
    _no_github(monkeypatch)

    def _boom(**k):
        raise ValueError("x is a file, not a directory")

    monkeypatch.setattr(_gh, "repo_dir_contents", _boom)
    c = TestClient(app)
    assert c.get("/api/github/contents", params={"repo": "u/demo", "path": "app.py"},
                 cookies=cookies).status_code == 400


def test_file_returns_content_shape(db, monkeypatch):
    from app.github import client as _gh

    u, cookies = _authed(db)
    _connected(db, u)
    _no_github(monkeypatch)
    monkeypatch.setattr(_gh, "repo_file_content", lambda **k: {
        "content": "x = 1\n", "truncated": False, "binary": False, "size": 6})
    c = TestClient(app)
    r = c.get("/api/github/file", params={"repo": "u/demo", "path": "app.py", "ref": "main"},
              cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "x = 1\n" and body["binary"] is False


def test_file_rejects_unconnected_repo(db):
    _, cookies = _authed(db)
    c = TestClient(app)
    assert c.get("/api/github/file", params={"repo": "u/ghost", "path": "a.py"},
                 cookies=cookies).status_code == 404


def test_published_diff_returns_diffinfo_shape(db, monkeypatch):
    from app.github import client as _gh

    u, cookies = _authed(db)
    _connected(db, u, "acme/demo")
    _no_github(monkeypatch)
    db.add(Task(repository="acme/demo", trigger_type="ci", status="COMPLETED",
                branch="fixhub-fixes/ci-abc-task-1", pr_number=11, pr_url="http://pr/11",
                owner_id=u.id))
    db.commit()
    monkeypatch.setattr(_gh, "pull_files", lambda **k: ["a.py"])
    monkeypatch.setattr(_gh, "pull_diff", lambda **k: {"diff": "diff --git a/a.py", "truncated": False})
    c = TestClient(app)
    tid = c.get("/api/tasks", cookies=cookies).json()[0]["id"]
    r = c.get(f"/api/tasks/{tid}/published-diff", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "published" and body["files"] == ["a.py"]
    assert body["diff"].startswith("diff --git") and body["pr_number"] == 11


def test_published_diff_404_without_pr(db):
    u, cookies = _authed(db)
    db.add(Task(repository="acme/demo", trigger_type="ci", status="FAILED",
                branch="branch pending", owner_id=u.id))
    db.commit()
    c = TestClient(app)
    tid = c.get("/api/tasks", cookies=cookies).json()[0]["id"]
    assert c.get(f"/api/tasks/{tid}/published-diff", cookies=cookies).status_code == 404


def test_published_diff_404_missing_task(db):
    _, cookies = _authed(db)
    c = TestClient(app)
    assert c.get("/api/tasks/424242/published-diff", cookies=cookies).status_code == 404
