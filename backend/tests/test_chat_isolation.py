"""Phase 10/11: repo/session data isolation.

- Chat LLM history for Repo A never contains Repo B rows.
- GET /api/tasks?repo= filters at SQL level.
- A foreign installation_id cannot read a repo owned by another installation.
"""

import uuid

from fastapi.testclient import TestClient

from app.chat.router import ChatBody
from app.config import settings
from app.db import SessionLocal, init_db
from app.main import create_app
from app.models import ChatMessage, Repository, Task

app = create_app()
client = TestClient(app, raise_server_exceptions=False)


def _repos(db, tag: str):
    a = Repository(
        full_name=f"iso/{tag}-a-{uuid.uuid4().hex[:8]}", installation_id="inst-a"
    )
    b = Repository(
        full_name=f"iso/{tag}-b-{uuid.uuid4().hex[:8]}", installation_id="inst-b"
    )
    db.add(a)
    db.add(b)
    db.commit()
    db.refresh(a)
    db.refresh(b)
    return a, b


def test_chat_history_never_crosses_repos(monkeypatch):
    """Seed Repo B with a canary; ask in Repo A; the LLM must never see it."""
    import app.chat.router as chat_router
    from app.llm.base import LLMResponse

    init_db()
    db = SessionLocal()
    repo_a, repo_b = _repos(db, "chat")
    db.add(
        ChatMessage(repo_id=repo_b.id, role="user", content="canary-bravo-secret-plan")
    )
    db.add(
        ChatMessage(repo_id=repo_b.id, role="assistant", content="canary-bravo-answer")
    )
    db.add(ChatMessage(repo_id=repo_a.id, role="user", content="hello in A"))
    db.commit()

    captured: dict = {}
    monkeypatch.setattr(
        type(settings), "resolved_llm", lambda self: ("http://x", "key", "m")
    )

    class Spy:
        def generate(self, messages, **kwargs):
            captured["messages"] = messages
            return LLMResponse(text="answer-a")

    monkeypatch.setattr(chat_router, "_llm_available", lambda: True)
    import app.llm.openrouter as orouter

    monkeypatch.setattr(orouter, "provider_from_settings", lambda: Spy())
    try:
        body = ChatBody(repo=repo_a.full_name, message="what now?")
        reply = chat_router._llm_chat_answer(db, body, repo_a, "what now?")
        assert reply == "answer-a"
        blob = " ".join(m.get("content", "") for m in captured["messages"])
        assert "canary-bravo" not in blob
        assert "hello in A" in blob or repo_a.full_name in blob
    finally:
        db.query(ChatMessage).filter(
            ChatMessage.repo_id.in_([repo_a.id, repo_b.id])
        ).delete(synchronize_session=False)
        db.query(Repository).filter(Repository.id.in_([repo_a.id, repo_b.id])).delete(
            synchronize_session=False
        )
        db.commit()
        db.close()


def test_tasks_list_filters_by_repo():
    init_db()
    db = SessionLocal()
    repo_a, repo_b = _repos(db, "tasks")
    ta = Task(repo_id=repo_a.id, title="task in A", state="CREATED")
    tb = Task(repo_id=repo_b.id, title="task in B", state="CREATED")
    db.add(ta)
    db.add(tb)
    db.commit()
    try:
        r = client.get("/api/tasks", params={"repo": repo_a.full_name})
        assert r.status_code == 200
        ids = [t["id"] for t in r.json()]
        assert ta.id in ids
        assert tb.id not in ids
    finally:
        db.query(Task).filter(Task.id.in_([ta.id, tb.id])).delete(
            synchronize_session=False
        )
        db.query(Repository).filter(Repository.id.in_([repo_a.id, repo_b.id])).delete(
            synchronize_session=False
        )
        db.commit()
        db.close()


def test_chat_history_is_scoped_and_restored():
    """Phase 14: persisted conversation restores per repo; Repo B rows never
    leak into Repo A's history."""
    init_db()
    db = SessionLocal()
    repo_a, repo_b = _repos(db, "hist")
    db.add(ChatMessage(repo_id=repo_a.id, role="user", content="hello A"))
    db.add(ChatMessage(repo_id=repo_a.id, role="assistant", content="hi A"))
    db.add(ChatMessage(repo_id=repo_b.id, role="user", content="hello B"))
    db.commit()
    try:
        r = client.get("/api/chat/history", params={"repo": repo_a.full_name})
        assert r.status_code == 200
        texts = [m["content"] for m in r.json()["messages"]]
        assert texts == ["hello A", "hi A"]
        r = client.get("/api/chat/history")
        assert r.status_code == 400
    finally:
        db.query(ChatMessage).filter(
            ChatMessage.repo_id.in_([repo_a.id, repo_b.id])
        ).delete(synchronize_session=False)
        db.query(Repository).filter(Repository.id.in_([repo_a.id, repo_b.id])).delete(
            synchronize_session=False
        )
        db.commit()
        db.close()


def test_foreign_installation_is_forbidden():
    init_db()
    db = SessionLocal()
    repo_a, repo_b = _repos(db, "inst")
    try:
        # repo A is owned by inst-a; asking with inst-b must 403, no network.
        r = client.get(
            "/api/github/repos/issues",
            params={"full_name": repo_a.full_name, "installation_id": "inst-b"},
        )
        assert r.status_code == 403
    finally:
        db.query(Repository).filter(Repository.id.in_([repo_a.id, repo_b.id])).delete(
            synchronize_session=False
        )
        db.commit()
        db.close()


def test_stale_installation_self_heals_when_caller_sees_repo(monkeypatch):
    """Live 403: the repo row held a stale installation id (App reinstall /
    test pollution like '123') while the caller's installation genuinely owns
    the repo. GitHub is source of truth — adopt the working id and proceed."""
    import app.github.api as gh_api

    init_db()
    db = SessionLocal()
    repo = Repository(
        full_name=f"iso/heal-{uuid.uuid4().hex[:8]}", installation_id="stale-id"
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    try:
        monkeypatch.setattr(
            gh_api, "_installation_can_access_repo", lambda iid, full: True
        )
        monkeypatch.setattr(gh_api, "get_installation_token", lambda iid: "tok")
        monkeypatch.setattr(
            gh_api.GitHubReadClient,
            "list_repo_issues",
            lambda self, full, state="open", limit=30: [],
        )
        r = client.get(
            "/api/github/repos/issues",
            params={"full_name": repo.full_name, "installation_id": "real-id"},
        )
        assert r.status_code == 200, r.text[:200]
        db.refresh(repo)
        assert repo.installation_id == "real-id"
    finally:
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()


def test_foreign_installation_stays_forbidden_when_heal_fails(monkeypatch):
    """Same mismatch, but GitHub says the caller cannot see the repo —
    the 403 boundary must hold."""
    import app.github.api as gh_api

    init_db()
    db = SessionLocal()
    repo = Repository(
        full_name=f"iso/noheal-{uuid.uuid4().hex[:8]}", installation_id="inst-a"
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    try:
        monkeypatch.setattr(
            gh_api, "_installation_can_access_repo", lambda iid, full: False
        )
        r = client.get(
            "/api/github/repos/issues",
            params={"full_name": repo.full_name, "installation_id": "inst-b"},
        )
        assert r.status_code == 403
    finally:
        db.query(Repository).filter_by(id=repo.id).delete()
        db.commit()
        db.close()
