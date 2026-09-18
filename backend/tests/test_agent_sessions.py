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
    assert (tmp_path / "a.py").read_text() == "x=2\n"  # edit applied to workdir
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
