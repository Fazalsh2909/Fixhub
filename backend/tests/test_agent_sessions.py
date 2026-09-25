"""Interactive coding sessions: bounded loop, persistence, honest no-key error."""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, init_db
from app.llm.base import LLMProvider, LLMResponse
from app.main import create_app
from app.models import Repository

app = create_app()
client = TestClient(app, raise_server_exceptions=False)


class ScriptedCoder(LLMProvider):
    """Reads a file, edits it, then stops. Records transcript growth."""

    def __init__(self):
        self.n = 0
        self.first_seen = 0

    def generate(self, messages, **kwargs):
        return LLMResponse(text="hi")

    def tool_call(self, messages, tools, **kwargs):
        self.n += 1
        if self.n == 1:
            self.first_seen = len(messages)
            return LLMResponse(
                text="",
                tool_calls=[{"name": "read_file", "arguments": '{"path": "a.py"}'}],
            )
        if self.n == 2:
            assert len(messages) > self.first_seen  # transcript grows across turns
            return LLMResponse(
                text="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "arguments": '{"path": "a.py", "old_string": "x=1", "new_string": "x=2"}',
                    }
                ],
            )
        return LLMResponse(text="done — bumped x", tool_calls=[])


def _repo(tmp_path: Path) -> str:
    init_db()
    db = SessionLocal()
    name = f"demo/sess-{uuid.uuid4().hex[:8]}"
    (tmp_path / "a.py").write_text("x=1\n")
    db.add(Repository(full_name=name, local_path=str(tmp_path)))
    db.commit()
    db.close()
    return name


def detail_rows(sid: int):
    from app.models import AgentMessage, AgentSession

    db = SessionLocal()
    try:
        s = db.query(AgentSession).filter_by(id=sid).first()
        assert s is not None
        return (
            db.query(AgentMessage)
            .filter_by(session_id=sid)
            .order_by(AgentMessage.id.asc())
            .all()
        )
    finally:
        db.close()


def test_session_codes_end_to_end(tmp_path: Path, monkeypatch):
    import app.llm.openrouter as _or

    monkeypatch.setattr(_or, "provider_from_settings", lambda: ScriptedCoder())
    repo = _repo(tmp_path)
    r = client.post("/api/agent/sessions", json={"repo": repo})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]

    r = client.post(
        f"/api/agent/sessions/{sid}/message", json={"content": "bump x", "max_turns": 5}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done"
    # P0-1: the edit lands in the session's isolated workspace, never in the
    # shared base. The base file must be untouched.
    from app.models import AgentSession as _AgentSession
    from app.repo.workspaces import remove_workspace as _remove_ws

    _db = SessionLocal()
    _sess = _db.query(_AgentSession).filter_by(id=sid).first()
    assert _sess is not None and _sess.workspace_path
    _ws = Path(_sess.workspace_path)
    _db.close()
    assert _ws.is_dir() and _ws != tmp_path
    assert (_ws / "a.py").read_text() == "x=2\n"  # edit applied in session ws
    assert (tmp_path / "a.py").read_text() == "x=1\n"  # shared base untouched
    tools = [m for m in body["messages"] if m["role"] == "tool"]
    assert any(m["tool"] == "read_file" and m["ok"] for m in tools)
    assert any(m["tool"] == "edit_file" and m["ok"] for m in tools)

    r = client.get(f"/api/agent/sessions/{sid}")
    assert r.status_code == 200
    roles = [m["role"] for m in r.json()["messages"]]
    assert roles[0] == "user"
    assert "assistant" in roles and "tool" in roles  # transcript persisted

    # follow-up reuses the transcript (pairing intact — no provider 400 path)
    r = client.post(
        f"/api/agent/sessions/{sid}/message", json={"content": "thanks", "max_turns": 2}
    )
    assert r.status_code == 200, r.text
    _remove_ws(_ws)


def test_session_pauses_when_turns_run_out(monkeypatch, tmp_path: Path):
    import app.llm.openrouter as _or

    class Chatty(LLMProvider):
        def generate(self, messages, **kwargs):
            return LLMResponse(text="hi")

        def tool_call(self, messages, tools, **kwargs):
            return LLMResponse(
                text="",
                tool_calls=[{"name": "list_files", "arguments": '{"dir": "."}'}],
            )

    monkeypatch.setattr(_or, "provider_from_settings", lambda: Chatty())
    repo = _repo(tmp_path)
    sid = client.post("/api/agent/sessions", json={"repo": repo}).json()["id"]
    r = client.post(
        f"/api/agent/sessions/{sid}/message",
        json={"content": "look around", "max_turns": 2},
    )
    assert r.json()["status"] == "paused"


class ReasonedPlanner(LLMProvider):
    """Thinks, plans with write_todos, then edits. Exercises timeline fields."""

    def __init__(self):
        self.n = 0

    def generate(self, messages, **kwargs):
        return LLMResponse(text="hi")

    def tool_call(self, messages, tools, **kwargs):
        self.n += 1
        if self.n == 1:
            return LLMResponse(
                text="Let me check the file first.",
                reasoning="I should read a.py before editing it.",
                tool_calls=[{"name": "read_file", "arguments": '{"path": "a.py"}'}],
            )
        if self.n == 2:
            return LLMResponse(
                text="",
                tool_calls=[
                    {
                        "name": "write_todos",
                        "arguments": '{"todos": [{"content": "bump x", "activeForm": "bumping x", "status": "in_progress"}]}',
                    }
                ],
            )
        if self.n == 3:
            return LLMResponse(
                text="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "arguments": '{"path": "a.py", "old_string": "x=1", "new_string": "x=2"}',
                    }
                ],
            )
        return LLMResponse(text="done", tool_calls=[])


def test_session_timeline_fields_and_plan(monkeypatch, tmp_path: Path):
    import app.llm.openrouter as _or

    monkeypatch.setattr(_or, "provider_from_settings", lambda: ReasonedPlanner())
    repo = _repo(tmp_path)
    sid = client.post("/api/agent/sessions", json={"repo": repo}).json()["id"]
    r = client.post(
        f"/api/agent/sessions/{sid}/message", json={"content": "bump x", "max_turns": 5}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done"
    # Phase 12: private reasoning is never exposed or persisted. `thinking`
    # stays present-but-empty; the engineering summary carries what happened.
    assistants = [m for m in body["messages"] if m["role"] == "assistant"]
    assert assistants and all(m["thinking"] == "" for m in assistants)
    assert any(m.get("summary") for m in assistants)
    for m in assistants:
        assert "read a.py before editing" not in (m["content"] or "")
    rows = detail_rows(sid)
    assert not any(
        "read a.py before editing" in r.content for r in rows if r.role == "assistant"
    )
    assert not any("reasoning" in (r.extra or "") for r in rows)
    # every tool row carries timing; the edit carries a diff preview
    tools = [m for m in body["messages"] if m["role"] == "tool"]
    assert tools and all("duration_ms" in m for m in tools)
    edit = next(m for m in tools if m["tool"] == "edit_file")
    assert "-x=1" in edit["diff"] and "+x=2" in edit["diff"]
    # write_todos now works in sessions (was rejected) and persists
    assert body["plan"] and body["plan"][0]["content"] == "bump x"
    detail = client.get(f"/api/agent/sessions/{sid}").json()
    plans = [m for m in detail["messages"] if m["role"] == "plan"]
    assert plans and plans[-1]["todos"][0]["status"] == "in_progress"


def _sse_events(text: str) -> list[tuple[str, str]]:
    out = []
    for chunk in text.split("\n\n"):
        event, data = None, None
        for line in chunk.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if event and data is not None:
            out.append((event, data))
    return out


def test_session_stream_emits_rows_live(monkeypatch, tmp_path: Path):
    import json as _json

    import app.llm.openrouter as _or

    monkeypatch.setattr(_or, "provider_from_settings", lambda: ReasonedPlanner())
    repo = _repo(tmp_path)
    sid = client.post("/api/agent/sessions", json={"repo": repo}).json()["id"]
    r = client.get(f"/api/agent/sessions/{sid}/stream?content=bump+x&max_turns=1")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(r.text)
    kinds = [e for e, _ in events]
    assert "message" in kinds and kinds[-1] == "done"
    rows = [_json.loads(d) for e, d in events if e == "message"]
    roles = [m["role"] for m in rows]
    assert roles[0] == "user"  # rows arrive in save order, live
    for m in rows:
        if m["role"] == "assistant":
            assert m.get("thinking") == ""  # no private reasoning over SSE either
            assert isinstance(m.get("summary"), str)
    final = _json.loads([d for e, d in events if e == "done"][-1])
    assert final["status"] == "paused"  # max_turns=1 with a tool call


def test_no_key_is_honest_503(monkeypatch, tmp_path: Path):
    for field in (
        "tokenrouter_api_key",
        "openai_api_key",
        "explabs_api_key",
        "bynara_api_key",
        "xkiro_api_key",
    ):
        monkeypatch.setattr(settings, field, "")
    repo = _repo(tmp_path)
    sid = client.post("/api/agent/sessions", json={"repo": repo}).json()["id"]
    r = client.post(f"/api/agent/sessions/{sid}/message", json={"content": "hi"})
    assert r.status_code == 503
    assert "LLM key" in r.json()["detail"]
