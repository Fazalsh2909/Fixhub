"""Task detail + schema-drift guards.

Live symptom: the task view returned HTTP 500 while the agent trace existed
in the DB. Root cause was schema drift — long-lived dev DBs missed columns
(tasks.workspace_path, task_events.prev_state, verification_runs.status,
...) so every SELECT touching them raised OperationalError. init_db() now
self-heals via _ensure_columns(); these tests lock that mitigation and the
endpoint contract (200 with "" defaults, never a traceback)."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

import app.db as dbmod
from app.db import SessionLocal, init_db
from app.main import create_app
from app.models import Repository, Task


def _stale_table_statements():
    """Pre-P0-1 schemas: tables exist but newer columns are missing."""
    return {
        "tasks": (
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, repo_id INTEGER, "
            "issue_number INTEGER, title VARCHAR(512), state VARCHAR(32), "
            "created_at DATETIME, updated_at DATETIME)"
        ),
        "task_events": (
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY, "
            "task_id INTEGER, stage VARCHAR(64), message TEXT, "
            "created_at DATETIME)"
        ),
        "verification_runs": (
            "CREATE TABLE verification_runs (id INTEGER PRIMARY KEY, "
            'task_id INTEGER, "check" VARCHAR(64), passed BOOLEAN, output TEXT)'
        ),
        "pull_requests": (
            "CREATE TABLE pull_requests (id INTEGER PRIMARY KEY, "
            "task_id INTEGER, url VARCHAR(1024), number INTEGER)"
        ),
    }


def test_ensure_columns_upgrades_stale_db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/stale.db")
    with eng.begin() as conn:
        for stmt in _stale_table_statements().values():
            conn.execute(text(stmt))
    orig = dbmod.engine
    dbmod.engine = eng
    try:
        dbmod._ensure_columns()
    finally:
        dbmod.engine = orig
    cols = {
        t: {c["name"] for c in inspect(eng).get_columns(t)}
        for t in (
            "tasks",
            "task_events",
            "verification_runs",
            "pull_requests",
        )
    }
    assert {"workspace_path", "base_sha"} <= cols["tasks"]
    assert {"prev_state", "reason"} <= cols["task_events"]
    assert {"status", "required"} <= cols["verification_runs"]
    assert "commit_sha" in cols["pull_requests"]


def test_task_detail_contract_empty_task_and_unknown_id():
    init_db()
    db = SessionLocal()
    repo = Repository(full_name=f"demo/detail-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=1, title="detail", state="DEBUGGING")
    db.add(task)
    db.commit()
    db.refresh(task)
    tid = task.id
    try:
        client = TestClient(create_app(), raise_server_exceptions=False)
        r = client.get(f"/api/tasks/{tid}")
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["diff"] == "" and body["branch"] == ""
        assert body["verification"] == [] and body["events"] == []
        r = client.get("/api/tasks/999999999")
        assert r.status_code == 200
        assert r.json() == {"error": "not found"}
    finally:
        db.query(Task).filter_by(id=tid).delete()
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()


def test_github_repos_bogus_installation_is_503_not_500(monkeypatch):
    """Live 500: GET /api/github/repos?installation_id=123 blew up because
    get_installation_token raises httpx.HTTPStatusError (not RuntimeError)
    for unknown installations, and _client_for only caught RuntimeError.
    Auth failures must be 503 (retryable upstream), never a traceback 500."""
    import httpx

    import app.github.api as gh_api

    def boom(installation_id: str) -> str:
        req = httpx.Request(
            "POST",
            "https://api.github.com/app/installations/123/access_tokens",
        )
        raise httpx.HTTPStatusError(
            "Client error '404 Not Found'",
            request=req,
            response=httpx.Response(404, request=req),
        )

    monkeypatch.setattr(gh_api, "get_installation_token", boom)
    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.get("/api/github/repos", params={"installation_id": "123"})
    assert r.status_code == 503, r.text[:300]
