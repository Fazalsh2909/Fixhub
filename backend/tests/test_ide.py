"""IDE APIs (Phase 3B backend): review gate + files/diff/terminal/events/approve."""
import subprocess

from fastapi.testclient import TestClient

from app.agent import loop as _loop
from app.db.models import Task, TaskEvent
from app.llm.client import AssistantMessage, ToolCall
from app.main import app
from app.tasks import service as _svc


def _git(path, *args):
    r = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (args, r.stderr[-500:])
    return r


def _fixture_repo(tmp_path):
    remote = tmp_path / "calc.git"
    _git(str(tmp_path), "init", "--bare", str(remote))
    work = tmp_path / "src"
    _git(str(tmp_path), "clone", str(remote), str(work))
    _git(str(work), "config", "user.email", "t@t.t")
    _git(str(work), "config", "user.name", "t")
    (work / "calc.py").write_text("def add(a, b):\n    return a - b  # BUG\n")
    (work / "test_calc.py").write_text("from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n")
    _git(str(work), "add", "-A")
    _git(str(work), "commit", "-m", "init with bug")
    _git(str(work), "push", "-u", "origin", "HEAD:main")
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                   cwd=str(remote), capture_output=True, timeout=30)
    return str(remote)


def _scripted_llm(monkeypatch):
    script = [
        AssistantMessage("", [ToolCall("1", "read_file", {"path": "calc.py"})]),
        AssistantMessage("", [ToolCall("2", "edit_file", {"path": "calc.py",
            "old": "return a - b  # BUG", "new": "return a + b"})]),
        AssistantMessage("Fixed add().", []),
    ]
    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)


def _make_review_task(db, tmp_path, monkeypatch):
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    remote = _fixture_repo(tmp_path)
    db.add(Task(repository="acme/ide", trigger_type="issue", issue_number=5,
                issue_title="add broken", issue_body="fix me", status="RUNNING"))
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    _scripted_llm(monkeypatch)
    out = _svc.run_task_inline(task.id, source=remote, auto_publish=False)
    assert out["status"] == "NEEDS_REVIEW", out
    db.expire_all()
    return db.query(Task).filter(Task.id == task.id).first()


def test_review_gate_then_approve(db, tmp_path, monkeypatch):
    t = _make_review_task(db, tmp_path, monkeypatch)
    assert t.status == "NEEDS_REVIEW"
    assert t.branch == f"fixhub-fixes/issue-5-task-{t.id}"

    # Workspace kept for review (no auto-cleanup on NEEDS_REVIEW).
    import os

    assert os.path.isdir(t.workspace)

    out = _svc.approve_task(t.id)
    assert out["status"] == "COMPLETED", out
    db.expire_all()
    t2 = db.query(Task).filter(Task.id == t.id).first()
    assert t2.status == "COMPLETED"
    assert t2.commit_sha and len(t2.commit_sha) == 40


def test_ide_files_and_read_and_diff(db, tmp_path, monkeypatch):
    t = _make_review_task(db, tmp_path, monkeypatch)
    c = TestClient(app)
    r = c.get(f"/api/tasks/{t.id}/files", params={"path": "."})
    assert r.status_code == 200, r.text
    names = [e["name"] for e in r.json()["entries"]]
    assert "calc.py" in names

    r = c.get(f"/api/tasks/{t.id}/file", params={"path": "calc.py"})
    assert r.status_code == 200, r.text
    assert "return a + b" in r.json()["content"]

    r = c.get(f"/api/tasks/{t.id}/diff")
    assert r.status_code == 200, r.text
    assert "calc.py" in " ".join(r.json()["files"])

    r = c.get(f"/api/tasks/{t.id}/events")
    assert r.status_code == 200, r.text
    assert any(e["type"] == "NEEDS_REVIEW" for e in r.json()["events"])

    r = c.get(f"/api/tasks/{t.id}/verification")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "NEEDS_REVIEW"


def test_ide_save_and_terminal(db, tmp_path, monkeypatch):
    t = _make_review_task(db, tmp_path, monkeypatch)
    c = TestClient(app)
    r = c.put(f"/api/tasks/{t.id}/file", json={"path": "note.txt", "content": "hello ide"})
    assert r.status_code == 200, r.text
    r = c.get(f"/api/tasks/{t.id}/file", params={"path": "note.txt"})
    assert "hello ide" in r.json()["content"]

    r = c.post(f"/api/tasks/{t.id}/terminal", json={"command": "echo hi"})
    assert r.status_code == 200, r.text
    assert "hi" in r.json()["stdout"]

    r = c.post(f"/api/tasks/{t.id}/chat", json={"message": "looks good"})
    assert r.status_code == 200, r.text
    assert db.query(TaskEvent).filter(TaskEvent.task_id == t.id,
                                      TaskEvent.type == "CHAT_MSG").count() == 1


def test_ide_blocks_sensitive_and_escapes(db, tmp_path, monkeypatch):
    t = _make_review_task(db, tmp_path, monkeypatch)
    c = TestClient(app)
    r = c.get(f"/api/tasks/{t.id}/file", params={"path": ".env"})
    assert r.status_code == 403
    r = c.get(f"/api/tasks/{t.id}/file", params={"path": "../outside.txt"})
    assert r.status_code == 400
    r = c.post(f"/api/tasks/{t.id}/terminal", json={"command": "ssh somewhere"})
    assert r.status_code == 403


def test_approve_endpoint_publishes(db, tmp_path, monkeypatch):
    t = _make_review_task(db, tmp_path, monkeypatch)
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/approve", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "COMPLETED"
