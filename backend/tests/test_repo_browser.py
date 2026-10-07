"""Read-only repo browser (contents/file) + published-diff endpoints."""
from fastapi.testclient import TestClient

from app.db.models import Repository, Task
from app.main import app


def _connected(db, full_name="u/demo"):
    db.add(Repository(github_full_name=full_name, installation_id="99"))
    db.commit()


def _no_github(monkeypatch):
    from app.github import app_auth as _auth

    monkeypatch.setattr(_auth, "installation_token", lambda iid: "tok")


def test_contents_lists_directory(db, monkeypatch):
    from app.github import client as _gh

    _connected(db)
    _no_github(monkeypatch)
    monkeypatch.setattr(_gh, "repo_dir_contents", lambda **k: [
        {"name": "app.py", "type": "file", "size": 10, "sha": "a"},
        {"name": "tests", "type": "dir", "size": 0, "sha": "b"},
    ])
    c = TestClient(app)
    r = c.get("/api/github/contents", params={"repo": "u/demo", "path": ".", "ref": "main"})
    assert r.status_code == 200, r.text
    assert [e["name"] for e in r.json()] == ["app.py", "tests"]


def test_contents_rejects_unconnected_repo(db):
    c = TestClient(app)
    r = c.get("/api/github/contents", params={"repo": "u/ghost"})
    assert r.status_code == 400


def test_contents_maps_value_error_to_400(db, monkeypatch):
    from app.github import client as _gh

    _connected(db)
    _no_github(monkeypatch)

    def _boom(**k):
        raise ValueError("x is a file, not a directory")

    monkeypatch.setattr(_gh, "repo_dir_contents", _boom)
    c = TestClient(app)
    assert c.get("/api/github/contents", params={"repo": "u/demo", "path": "app.py"}).status_code == 400


def test_file_returns_content_shape(db, monkeypatch):
    from app.github import client as _gh

    _connected(db)
    _no_github(monkeypatch)
    monkeypatch.setattr(_gh, "repo_file_content", lambda **k: {
        "content": "x = 1\n", "truncated": False, "binary": False, "size": 6})
    c = TestClient(app)
    r = c.get("/api/github/file", params={"repo": "u/demo", "path": "app.py", "ref": "main"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "x = 1\n" and body["binary"] is False


def test_file_rejects_unconnected_repo(db):
    c = TestClient(app)
    assert c.get("/api/github/file", params={"repo": "u/ghost", "path": "a.py"}).status_code == 400


def test_published_diff_returns_diffinfo_shape(db, monkeypatch):
    from app.github import client as _gh

    _connected(db, "acme/demo")
    _no_github(monkeypatch)
    db.add(Task(repository="acme/demo", trigger_type="ci", status="COMPLETED",
                branch="fixhub-fixes/ci-abc-task-1", pr_number=11, pr_url="http://pr/11"))
    db.commit()
    monkeypatch.setattr(_gh, "pull_files", lambda **k: ["a.py"])
    monkeypatch.setattr(_gh, "pull_diff", lambda **k: {"diff": "diff --git a/a.py", "truncated": False})
    c = TestClient(app)
    tid = c.get("/api/tasks").json()[0]["id"]
    r = c.get(f"/api/tasks/{tid}/published-diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "published" and body["files"] == ["a.py"]
    assert body["diff"].startswith("diff --git") and body["pr_number"] == 11


def test_published_diff_404_without_pr(db):
    db.add(Task(repository="acme/demo", trigger_type="ci", status="FAILED", branch="branch pending"))
    db.commit()
    c = TestClient(app)
    tid = c.get("/api/tasks").json()[0]["id"]
    assert c.get(f"/api/tasks/{tid}/published-diff").status_code == 404


def test_published_diff_404_missing_task(db):
    c = TestClient(app)
    assert c.get("/api/tasks/424242/published-diff").status_code == 404
