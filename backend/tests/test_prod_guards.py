"""Prod guards: cost cap stops the loop, streaming falls back cleanly."""

from pathlib import Path

from app.agent.orchestrator import engineer_issue
from app.config import settings
from app.db import SessionLocal, init_db
from app.llm.base import LLMProvider, LLMResponse
from app.models import Repository, Task


class CountingProvider(LLMProvider):
    def __init__(self):
        self.calls = 0

    def generate(self, messages, **kwargs):
        return LLMResponse(text="hi")

    def tool_call(self, messages, tools, **kwargs):
        self.calls += 1
        # record fake spend so the cap trips
        from app.metrics import record_llm_call

        record_llm_call(
            "gpt-4o-mini", {"prompt_tokens": 100000, "completion_tokens": 0}, 1
        )
        return LLMResponse(
            text="loop",
            tool_calls=[{"name": "list_files", "arguments": '{"dir": "."}'}],
        )


def test_cost_cap_aborts(tmp_path: Path):
    init_db()
    db = SessionLocal()
    repo = db.query(Repository).filter_by(full_name="demo/costcap").first()
    if repo is None:
        repo = Repository(full_name="demo/costcap")
        db.add(repo)
        db.commit()
        db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=1, title="cap", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    old = settings.agent_max_cost_usd
    settings.agent_max_cost_usd = 0.001  # trip after 1 fake call
    try:
        import app.agent.orchestrator as orch

        orig = orch.run_in_sandbox
        orch.run_in_sandbox = lambda *a, **k: {"ok": True, "output": "ok"}
        try:
            prov = CountingProvider()
            engineer_issue(db, task, tmp_path, prov)
        finally:
            orch.run_in_sandbox = orig
        assert prov.calls <= 2, f"cap should stop early, got {prov.calls} calls"
    finally:
        settings.agent_max_cost_usd = old
    db.close()


def test_streaming_fallback():
    p = CountingProvider()
    chunks = list(p.generate_stream([{"role": "user", "content": "hi"}]))
    assert len(chunks) == 1 and chunks[0].text == "hi"
