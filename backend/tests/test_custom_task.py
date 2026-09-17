"""Custom-task tests: Claude-like freeform tasks without a GitHub issue."""

from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import create_app
from app.models import Repository, Task

app = create_app()
client = TestClient(app, raise_server_exceptions=False)


def _ensure_repo() -> str:
    init_db()
    db = SessionLocal()
    repo = db.query(Repository).filter_by(full_name="demo/custom").first()
    if repo is None:
        repo = Repository(full_name="demo/custom", local_path=".")
        db.add(repo)
        db.commit()
    db.close()
    return "demo/custom"


def test_create_task_ok():
    repo = _ensure_repo()
    r = client.post("/api/tasks", json={"repo": repo, "title": "fix login loop"})
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    db = SessionLocal()
    assert db.query(Task).filter_by(id=tid).first().state == "CREATED"
    db.close()


def test_create_task_requires_title():
    repo = _ensure_repo()
    r = client.post("/api/tasks", json={"repo": repo, "title": "  "})
    assert r.status_code == 400


def test_create_task_unknown_repo():
    r = client.post("/api/tasks", json={"repo": "ghost/nope", "title": "x"})
    assert r.status_code == 404
