"""Approval-gate tests: no GitHub write without explicit approval + verified diff."""

from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import create_app
from app.models import Patch, Repository, Task

app = create_app()
client = TestClient(app, raise_server_exceptions=False)


def _make_task(state: str, diff: str = "diff --git a/x b/x") -> int:
    init_db()
    db = SessionLocal()
    repo = db.query(Repository).filter_by(full_name="demo/approval").first()
    if repo is None:
        repo = Repository(full_name="demo/approval")
        db.add(repo)
        db.commit()
        db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=9, title="approval gate", state=state)
    db.add(task)
    db.commit()
    db.refresh(task)
    tid = task.id
    db.add(Patch(task_id=tid, diff=diff, branch="fixhub/issue-9"))
    db.commit()
    db.close()
    return tid


def test_approve_requires_reviewing_state():
    tid = _make_task("DEBUGGING")
    r = client.post(f"/api/tasks/{tid}/approve", json={"approver": "dev"})
    assert r.status_code == 409


def test_approve_requires_real_diff():
    tid = _make_task("REVIEWING", diff="(no files changed)")
    r = client.post(f"/api/tasks/{tid}/approve", json={"approver": "dev"})
    assert r.status_code == 409


def test_approve_records_locally_without_installation():
    tid = _make_task("REVIEWING")
    r = client.post(f"/api/tasks/{tid}/approve", json={"approver": "dev"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    db = SessionLocal()
    assert db.query(Task).filter_by(id=tid).first().state == "COMMITTED"
    db.close()


def test_reject_returns_to_debugging():
    tid = _make_task("REVIEWING")
    r = client.post(
        f"/api/tasks/{tid}/reject", json={"approver": "dev", "reason": "needs tests"}
    )
    assert r.status_code == 200
    db = SessionLocal()
    assert db.query(Task).filter_by(id=tid).first().state == "DEBUGGING"
    db.close()
