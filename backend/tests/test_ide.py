"""IDE APIs (Phase 3B backend): review gate + files/diff/terminal/events/approve."""
import subprocess

from fastapi.testclient import TestClient

from app.agent import loop as _loop
from app.db.models import Task, TaskEvent
from app.llm.client import AssistantMessage, ToolCall
from app.main import app
from app.tasks import service as _svc
from tests.conftest import make_user, session_cookies


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
        AssistantMessage("", [ToolCall("1", "read_file", {"path": "test_calc.py"})]),
        AssistantMessage("", [ToolCall("2", "read_file", {"path": "calc.py"})]),
        AssistantMessage("", [ToolCall("3", "edit_file", {"path": "calc.py",
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
    user = make_user(db, email="ide@example.com")
    from app.llm import credentials as _creds

    _creds.create_or_update_credential(db, user_id=user.id, provider="openai",
                                       api_key="TEST_IDE_KEY_12345678", model="gpt-4o-mini")
    db.add(Task(repository="acme/ide", trigger_type="issue", issue_number=5,
                issue_title="add broken", issue_body="fix me", status="RUNNING",
                owner_id=user.id))
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    _scripted_llm(monkeypatch)
    out = _svc.run_task_inline(task.id, source=remote, auto_publish=False)
    assert out["status"] == "NEEDS_REVIEW", out
    db.expire_all()
    t = db.query(Task).filter(Task.id == task.id).first()
    return t, session_cookies(db, user)


def test_review_gate_then_approve(db, tmp_path, monkeypatch):
    t, _ = _make_review_task(db, tmp_path, monkeypatch)
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
    t, cookies = _make_review_task(db, tmp_path, monkeypatch)
    c = TestClient(app)
    r = c.get(f"/api/tasks/{t.id}/files", params={"path": "."}, cookies=cookies)
    assert r.status_code == 200, r.text
    names = [e["name"] for e in r.json()["entries"]]
    assert "calc.py" in names

    r = c.get(f"/api/tasks/{t.id}/file", params={"path": "calc.py"}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert "return a + b" in r.json()["content"]

    r = c.get(f"/api/tasks/{t.id}/diff", cookies=cookies)
    assert r.status_code == 200, r.text
    assert "calc.py" in " ".join(r.json()["files"])

    r = c.get(f"/api/tasks/{t.id}/events", cookies=cookies)
    assert r.status_code == 200, r.text
    assert any(e["type"] == "NEEDS_REVIEW" for e in r.json()["events"])

    r = c.get(f"/api/tasks/{t.id}/verification", cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "NEEDS_REVIEW"


def test_ide_save_and_terminal(db, tmp_path, monkeypatch):
    t, cookies = _make_review_task(db, tmp_path, monkeypatch)
    c = TestClient(app)
    r = c.put(f"/api/tasks/{t.id}/file", json={"path": "note.txt", "content": "hello ide"},
              cookies=cookies)
    assert r.status_code == 200, r.text
    r = c.get(f"/api/tasks/{t.id}/file", params={"path": "note.txt"}, cookies=cookies)
    assert "hello ide" in r.json()["content"]

    r = c.post(f"/api/tasks/{t.id}/terminal", json={"command": "echo hi"}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert "hi" in r.json()["stdout"]

    r = c.post(f"/api/tasks/{t.id}/chat", json={"message": "looks good"}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert db.query(TaskEvent).filter(TaskEvent.task_id == t.id,
                                      TaskEvent.type == "CHAT_MSG").count() == 1


def test_ide_blocks_sensitive_and_escapes(db, tmp_path, monkeypatch):
    t, cookies = _make_review_task(db, tmp_path, monkeypatch)
    c = TestClient(app)
    r = c.get(f"/api/tasks/{t.id}/file", params={"path": ".env"}, cookies=cookies)
    assert r.status_code == 403
    r = c.get(f"/api/tasks/{t.id}/file", params={"path": "../outside.txt"}, cookies=cookies)
    assert r.status_code == 400
    r = c.post(f"/api/tasks/{t.id}/terminal", json={"command": "ssh somewhere"},
               cookies=cookies)
    assert r.status_code == 403


def test_approve_endpoint_publishes(db, tmp_path, monkeypatch):
    t, cookies = _make_review_task(db, tmp_path, monkeypatch)
    c = TestClient(app)
    r = c.post(f"/api/tasks/{t.id}/approve", json={}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# Task 3 Explorer: Git-accurate changes + per-file diffs
# ---------------------------------------------------------------------------

def _repo(tmp_path, files):
    ws = tmp_path / "explorer-ws"
    ws.mkdir()
    _git(str(ws), "init", "-q")
    _git(str(ws), "config", "user.email", "t@t.t")
    _git(str(ws), "config", "user.name", "t")
    for rel, content in files.items():
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(str(ws), "add", "-A")
    _git(str(ws), "commit", "-qm", "init")
    return ws


def _explorer_task(db, ws):
    user = make_user(db, email="explorer@example.com")
    task = Task(repository="acme/explorer", trigger_type="issue", issue_number=8,
                issue_title="explorer", issue_body="", status="NEEDS_REVIEW",
                owner_id=user.id, workspace=str(ws))
    db.add(task)
    db.commit()
    db.refresh(task)
    return task, session_cookies(db, user)


def _get(c, url, cookies, **params):
    r = c.get(url, params=params, cookies=cookies)
    assert r.status_code == 200, r.text
    return r.json()


def test_changes_detect_modified_and_untracked_files(db, tmp_path):
    ws = _repo(tmp_path, {"calc.py": "def add(a, b):\n    return a - b\n"})
    task, cookies = _explorer_task(db, ws)
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (ws / "brand new.py").write_text("print('fresh line')\n")
    c = TestClient(app)

    body = _get(c, f"/api/tasks/{task.id}/changes", cookies)
    changes = {entry["path"]: entry for entry in body["changes"]}
    assert changes["calc.py"]["status"] == "modified"
    assert changes["brand new.py"]["status"] == "added"
    assert body["counts"] == {
        "modified": 1, "added": 1, "deleted": 0, "renamed": 0, "total": 2,
    }
    assert body["source"] == "workspace"


def test_changes_detect_deleted_and_renamed_files(db, tmp_path):
    ws = _repo(tmp_path, {"gone.py": "x = 1\n", "old_name.py": "y = 2\n"})
    task, cookies = _explorer_task(db, ws)
    (ws / "gone.py").unlink()
    _git(str(ws), "mv", "old_name.py", "new_name.py")
    c = TestClient(app)

    body = _get(c, f"/api/tasks/{task.id}/changes", cookies)
    changes = {entry["path"]: entry for entry in body["changes"]}
    assert changes["gone.py"]["status"] == "deleted"
    assert changes["new_name.py"]["status"] == "renamed"
    assert changes["new_name.py"]["previous_path"] == "old_name.py"
    assert body["counts"]["deleted"] == 1
    assert body["counts"]["renamed"] == 1


def test_changes_handles_filenames_with_spaces(db, tmp_path):
    ws = _repo(tmp_path, {"docs/my notes.txt": "one\n"})
    task, cookies = _explorer_task(db, ws)
    (ws / "docs" / "my notes.txt").write_text("two\n")
    c = TestClient(app)

    body = _get(c, f"/api/tasks/{task.id}/changes", cookies)
    assert [(e["path"], e["status"]) for e in body["changes"]] == [
        ("docs/my notes.txt", "modified"),
    ]
    diff = _get(c, f"/api/tasks/{task.id}/diff/file", cookies, path="docs/my notes.txt")
    assert "-one" in diff["diff"] and "+two" in diff["diff"]


def test_file_diff_modified_added_and_deleted(db, tmp_path):
    ws = _repo(tmp_path, {"calc.py": "old line\n", "gone.py": "removed line\n"})
    task, cookies = _explorer_task(db, ws)
    (ws / "calc.py").write_text("new line\n")
    (ws / "gone.py").unlink()
    (ws / "fresh.py").write_text("brand new\n")
    c = TestClient(app)

    modified = _get(c, f"/api/tasks/{task.id}/diff/file", cookies, path="calc.py")
    assert modified["status"] == "modified"
    assert "-old line" in modified["diff"] and "+new line" in modified["diff"]

    # Untracked files are invisible to `git diff HEAD`; the API must still
    # produce a real new-file patch instead of an empty panel.
    added = _get(c, f"/api/tasks/{task.id}/diff/file", cookies, path="fresh.py")
    assert added["status"] == "added"
    assert "+brand new" in added["diff"]
    assert "+++ b/fresh.py" in added["diff"]
    assert added["diff"].strip() != ""

    deleted = _get(c, f"/api/tasks/{task.id}/diff/file", cookies, path="gone.py")
    assert deleted["status"] == "deleted"
    assert "-removed line" in deleted["diff"]


def test_file_diff_renamed_reports_previous_path(db, tmp_path):
    ws = _repo(tmp_path, {"src/old_name.py": "one\ntwo\nthree\n"})
    task, cookies = _explorer_task(db, ws)
    _git(str(ws), "mv", "src/old_name.py", "src/new_name.py")
    (ws / "src" / "new_name.py").write_text("one\nTWO\nthree\n")
    c = TestClient(app)

    body = _get(c, f"/api/tasks/{task.id}/diff/file", cookies, path="src/new_name.py")
    assert body["status"] == "renamed"
    assert body["previous_path"] == "src/old_name.py"
    assert "rename from src/old_name.py" in body["diff"]
    assert "rename to src/new_name.py" in body["diff"]
    assert "-two" in body["diff"] and "+TWO" in body["diff"]


def test_changes_exclude_bytecode_and_sensitive_files(db, tmp_path):
    ws = _repo(tmp_path, {"ok.py": "x = 1\n"})
    task, cookies = _explorer_task(db, ws)
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "ok.cpython-311.pyc").write_bytes(b"bytecode")
    (ws / ".env").write_text("SECRET=1\n")
    (ws / "deploy.pem").write_text("private key")
    c = TestClient(app)

    body = _get(c, f"/api/tasks/{task.id}/changes", cookies)
    assert body["changes"] == []
    assert body["counts"]["total"] == 0

    r = c.get(f"/api/tasks/{task.id}/diff/file", params={"path": ".env"}, cookies=cookies)
    assert r.status_code == 403
    r = c.get(f"/api/tasks/{task.id}/diff/file", params={"path": "deploy.pem"}, cookies=cookies)
    assert r.status_code == 403
    r = c.get(f"/api/tasks/{task.id}/diff/file", params={"path": "../outside.py"}, cookies=cookies)
    assert r.status_code == 400
    r = c.get(f"/api/tasks/{task.id}/diff/file", params={"path": "ok.py"}, cookies=cookies)
    assert r.status_code == 404  # unchanged file: not part of the change set


def test_task_diff_response_fields_are_preserved(db, tmp_path):
    ws = _repo(tmp_path, {"calc.py": "return a - b\n"})
    task, cookies = _explorer_task(db, ws)
    (ws / "calc.py").write_text("return a + b\n")
    c = TestClient(app)

    body = _get(c, f"/api/tasks/{task.id}/diff", cookies)
    assert {"branch", "status", "files", "stat", "diff"} <= set(body)
    assert "calc.py" in body["files"]
    assert "return a + b" in body["diff"]


def test_changes_route_410_when_workspace_is_gone(db, tmp_path):
    task, cookies = _explorer_task(db, tmp_path / "missing-ws")
    c = TestClient(app)
    assert c.get(f"/api/tasks/{task.id}/changes", cookies=cookies).status_code == 410


def test_pull_file_changes_maps_github_payload(monkeypatch):
    from app.github import client as _gh

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"filename": "src/a.py", "status": "modified", "additions": 2,
                 "deletions": 1, "patch": "@@ -1 +1 @@"},
                {"filename": "assets/logo.png", "status": "added",
                 "additions": 0, "deletions": 0},
                {"filename": "src/new.py", "status": "renamed",
                 "previous_filename": "src/old.py", "patch": "@@"},
            ]

    monkeypatch.setattr(_gh.httpx, "get", lambda *a, **k: _Resp())
    files = _gh.pull_file_changes(token="t", full_name="acme/x", number=3)
    assert files[0]["has_patch"] is True and files[0]["status"] == "modified"
    assert files[1]["has_patch"] is False and files[1]["path"] == "assets/logo.png"
    assert files[2]["previous_path"] == "src/old.py"


def test_published_diff_exposes_change_metadata(db, monkeypatch):
    from app.db.models import Repository
    from app.github import app_auth as _app_auth
    from app.github import client as _gh
    from app.github import install as _install

    user = make_user(db, email="published-meta@example.com")
    db.add(Repository(github_full_name="acme/explorer", installation_id="77",
                      owner_id=user.id))
    task = Task(repository="acme/explorer", trigger_type="issue", status="COMPLETED",
                pr_number=12, pr_url="https://github.com/acme/explorer/pull/12",
                owner_id=user.id, branch="fixhub-fixes/issue-8-task-1")
    db.add(task)
    db.commit()
    db.refresh(task)
    cookies = session_cookies(db, user)

    monkeypatch.setattr(_app_auth, "installation_token", lambda iid: "tok")
    monkeypatch.setattr(_install, "installation_verified_for_user", lambda *a, **k: True)
    monkeypatch.setattr(_gh, "pull_file_changes", lambda **k: [
        {"path": "a.py", "status": "modified", "previous_path": None,
         "patch": "@@ -1 +1 @@\n-a\n+b", "has_patch": True},
        {"path": "new.py", "status": "added", "previous_path": None,
         "patch": "@@ -0,0 +1 @@\n+x", "has_patch": True},
        {"path": "old.py", "status": "removed", "previous_path": None,
         "patch": "@@ -1 +0,0 @@\n-x", "has_patch": True},
        {"path": "src/renamed.py", "status": "renamed",
         "previous_path": "src/original.py", "patch": "", "has_patch": False},
    ])
    monkeypatch.setattr(_gh, "pull_diff",
                        lambda **k: {"diff": "diff --git a/a.py b/a.py", "truncated": False})
    c = TestClient(app)

    body = _get(c, f"/api/tasks/{task.id}/published-diff", cookies)
    assert body["source"] == "published"
    assert body["counts"] == {
        "modified": 1, "added": 1, "deleted": 1, "renamed": 1, "total": 4,
    }
    changes = {entry["path"]: entry for entry in body["changes"]}
    assert changes["old.py"]["status"] == "deleted"
    assert changes["src/renamed.py"]["previous_path"] == "src/original.py"

    file_body = _get(c, f"/api/tasks/{task.id}/published-diff/file", cookies, path="a.py")
    assert file_body["available"] is True
    assert file_body["diff"].startswith("--- a/a.py")
    assert "+b" in file_body["diff"]

    # GitHub omits patches for binaries / oversized diffs: say so, don't fake one.
    missing = _get(c, f"/api/tasks/{task.id}/published-diff/file", cookies,
                   path="src/renamed.py")
    assert missing["available"] is False
    assert "did not provide a text patch" in missing["message"]
    assert missing["diff"] == ""


def test_published_file_diff_404_for_file_outside_pr(db, monkeypatch):
    from app.db.models import Repository
    from app.github import app_auth as _app_auth
    from app.github import client as _gh
    from app.github import install as _install

    user = make_user(db, email="published-missing@example.com")
    db.add(Repository(github_full_name="acme/explorer", installation_id="77",
                      owner_id=user.id))
    task = Task(repository="acme/explorer", trigger_type="issue", status="COMPLETED",
                pr_number=13, owner_id=user.id)
    db.add(task)
    db.commit()
    db.refresh(task)
    cookies = session_cookies(db, user)
    monkeypatch.setattr(_app_auth, "installation_token", lambda iid: "tok")
    monkeypatch.setattr(_install, "installation_verified_for_user", lambda *a, **k: True)
    monkeypatch.setattr(_gh, "pull_file_changes", lambda **k: [])
    monkeypatch.setattr(_gh, "pull_diff", lambda **k: {"diff": "", "truncated": False})
    c = TestClient(app)

    r = c.get(f"/api/tasks/{task.id}/published-diff/file", params={"path": "nope.py"},
              cookies=cookies)
    assert r.status_code == 404
    r = c.get(f"/api/tasks/{task.id}/published-diff/file", params={"path": ".env"},
              cookies=cookies)
    assert r.status_code == 403
