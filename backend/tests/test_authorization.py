"""Phase 2 authorization + IDOR tests: users can only touch their own resources.

Pattern: user A owns repo A + task A; user B owns repo B + task B.
A->A ok, A->B 404 (no leak), B->B ok. Same shape for tasks, task events,
memories, pull requests, terminal/files, and client-supplied ID tampering.
"""

from fastapi.testclient import TestClient

from app.db.models import Memory, PullRequest, Repository, Task, TaskEvent
from app.main import app
from tests.conftest import make_user, session_cookies


def _owned(db, email, repo_name, task_status="RUNNING"):
    """Seed one user + connected repo + task. Returns (user, cookies, repo, task)."""
    from app.db.models import GitHubConnection

    u = make_user(db, email=email)
    repo = Repository(
        github_full_name=repo_name, installation_id="inst-" + email, owner_id=u.id
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    db.add(GitHubConnection(user_id=u.id, installation_id="inst-" + email))
    task = Task(
        repository=repo_name,
        repository_id=repo.id,
        owner_id=u.id,
        trigger_type="issue",
        issue_number=1,
        issue_title="t",
        issue_body="b",
        status=task_status,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return u, session_cookies(db, u), repo, task


def test_cross_user_task_detail_forbidden(db):
    _, _ca, _, ta = _owned(db, "a1@example.com", "acme/a1")
    _, cb, _, _ = _owned(db, "b1@example.com", "acme/b1")
    c = TestClient(app)
    assert c.get(f"/api/tasks/{ta.id}", cookies=cb).status_code == 404
    assert c.get(f"/api/tasks/{ta.id}").status_code == 401


def test_task_list_scoped_to_owner(db):
    ua, ca, _, ta = _owned(db, "a2@example.com", "acme/a2")
    _, _, _, tb = _owned(db, "b2@example.com", "acme/b2")
    c = TestClient(app)
    ids = [t["id"] for t in c.get("/api/tasks", cookies=ca).json()]
    assert ta.id in ids and tb.id not in ids


def test_cross_user_task_events_forbidden(db):
    _, _ca, _, ta = _owned(db, "a3@example.com", "acme/a3")
    _, cb, _, _ = _owned(db, "b3@example.com", "acme/b3")
    db.add(TaskEvent(task_id=ta.id, type="TOOL_CALL", data_json="{}"))
    db.commit()
    c = TestClient(app)
    assert c.get(f"/api/tasks/{ta.id}/events", cookies=cb).status_code == 404
    assert c.get(f"/api/tasks/{ta.id}/verification", cookies=cb).status_code == 404


def test_cross_user_memories_not_leaked(db):
    _, ca, _, ta = _owned(db, "a4@example.com", "acme/a4")
    _, cb, _, _ = _owned(db, "b4@example.com", "acme/b4")
    db.add(
        Memory(
            repository="acme/a4",
            path="__overview__",
            summary="A secret",
            commit_sha="",
            last_analyzed_rev="",
            owner_id=ta.owner_id,
        )
    )
    db.commit()
    c = TestClient(app)
    body = c.get(f"/api/tasks/{ta.id}", cookies=ca).json()
    assert any(m["summary"] == "A secret" for m in body["memory"])
    # B cannot even open A's task, so A's memory is unreachable.
    assert c.get(f"/api/tasks/{ta.id}", cookies=cb).status_code == 404


def test_cross_user_pull_request_not_leaked(db):
    _, _ca, _, ta = _owned(db, "a5@example.com", "acme/a5")
    _, cb, _, _ = _owned(db, "b5@example.com", "acme/b5")
    ta.pr_number = 42
    ta.pr_url = "http://pr/42"
    # published-diff needs GitHub; without PR network it 502s — but for an
    # unowned task it must 404 before any GitHub touch.
    c = TestClient(app)
    assert c.get(f"/api/tasks/{ta.id}/published-diff", cookies=cb).status_code == 404
    db.add(
        PullRequest(
            task_id=ta.id,
            pr_number=42,
            pr_url="http://pr/42",
            branch="b",
            commit_sha="c",
        )
    )
    db.commit()
    assert c.get(f"/api/tasks/{ta.id}", cookies=cb).status_code == 404


def test_cross_user_repo_endpoints_forbidden(db):
    _, _ca, _, _ = _owned(db, "a6@example.com", "acme/a6")
    _, cb, _, _ = _owned(db, "b6@example.com", "acme/b6")
    c = TestClient(app)
    names = [
        r["github_full_name"] for r in c.get("/api/repositories", cookies=cb).json()
    ]
    assert "acme/a6" not in names and "acme/b6" in names
    assert c.get("/api/github/issues?repo=acme/a6", cookies=cb).status_code == 404
    assert c.get("/api/github/contents?repo=acme/a6", cookies=cb).status_code == 404
    assert (
        c.get("/api/github/file?repo=acme/a6&path=x.py", cookies=cb).status_code == 404
    )


def test_cross_user_mutations_forbidden(db):
    _, _ca, _, ta = _owned(db, "a7@example.com", "acme/a7")
    _, cb, _, tb = _owned(db, "b7@example.com", "acme/b7")
    c = TestClient(app)
    assert c.post(f"/api/tasks/{ta.id}/run", cookies=cb).status_code == 404
    assert c.post(f"/api/tasks/{ta.id}/cancel", cookies=cb).status_code == 404
    # Cleanup is ADMIN-only: non-admin gets 403 before ownership is even read.
    assert c.post(f"/api/tasks/{ta.id}/cleanup", cookies=cb).status_code == 403
    assert c.post(f"/api/tasks/{ta.id}/approve", json={}, cookies=cb).status_code == 404
    assert (
        c.post(
            f"/api/tasks/{ta.id}/terminal", json={"command": "echo hi"}, cookies=cb
        ).status_code
        == 404
    )
    assert (
        c.put(
            f"/api/tasks/{ta.id}/file", json={"path": "x", "content": "y"}, cookies=cb
        ).status_code
        == 404
    )
    assert (
        c.post(
            f"/api/tasks/{ta.id}/chat", json={"message": "hi"}, cookies=cb
        ).status_code
        == 404
    )
    assert c.get(f"/api/tasks/{ta.id}/files", cookies=cb).status_code == 404
    assert c.get(f"/api/tasks/{ta.id}/file?path=x.py", cookies=cb).status_code == 404
    assert c.get(f"/api/tasks/{ta.id}/diff", cookies=cb).status_code == 404
    # Owner's own resources still work (terminal validation error proves routing
    # reached the owned task instead of 404).
    r = c.post(f"/api/tasks/{tb.id}/terminal", json={"command": ""}, cookies=cb)
    assert r.status_code == 400, r.text


def test_sequential_id_guessing_blocked(db):
    _, _ca, _, ta = _owned(db, "a8@example.com", "acme/a8")
    _, cb, _, tb = _owned(db, "b8@example.com", "acme/b8")
    c = TestClient(app)
    # B cannot open A's task, but can open its own; unknown IDs 404.
    assert c.get(f"/api/tasks/{ta.id}", cookies=cb).status_code == 404
    assert c.get(f"/api/tasks/{tb.id}", cookies=cb).status_code == 200
    assert c.get("/api/tasks/999999", cookies=cb).status_code == 404


def test_client_user_id_tampering_ignored(db):
    ua, ca, ra, ta = _owned(db, "a9@example.com", "acme/a9")
    ub, cb, _, _ = _owned(db, "b9@example.com", "acme/b9")
    c = TestClient(app)
    # from-issue with another repo: B cannot create tasks on A's repo.
    r = c.post(
        "/api/tasks/from-issue",
        json={"repository": "acme/a9", "issue_number": 3},
        cookies=cb,
    )
    assert r.status_code == 404, r.text
    # connect cannot steal A's repo or installation.
    r = c.post(
        "/api/repositories/connect",
        json={"github_full_name": "acme/a9", "installation_id": "x"},
        cookies=cb,
    )
    assert r.status_code == 404, r.text
    r = c.post(
        "/api/repositories/connect",
        json={
            "github_full_name": "acme/evil",
            "installation_id": "inst-a9@example.com",
        },
        cookies=cb,
    )
    assert r.status_code == 404, r.text
    # Queue/cron ops require auth too.
    assert c.get("/api/queue/health").status_code == 401
    assert c.post("/api/cron/ci-watch").status_code == 401


def test_csrf_origin_mismatch_rejected(db):
    _, ca, _, ta = _owned(db, "a10@example.com", "acme/a10")
    c = TestClient(app)
    r = c.post(
        f"/api/tasks/{ta.id}/cancel",
        cookies=ca,
        headers={"origin": "https://evil.example.com"},
    )
    assert r.status_code == 403, r.text
    # Same-origin requests still pass.
    r = c.post(
        f"/api/tasks/{ta.id}/cancel",
        cookies=ca,
        headers={"origin": "http://testserver"},
    )
    assert r.status_code in (200, 400), r.text
