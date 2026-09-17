"""Orchestrator robustness: provider failure => FAILED task, never an exception."""

from pathlib import Path

from app.agent.orchestrator import engineer_issue
from app.db import SessionLocal, init_db
from app.llm.base import LLMProvider, LLMResponse
from app.llm.openrouter import ProviderError
from app.models import Repository, Task


class BoomProvider(LLMProvider):
    def generate(self, messages: list[dict], **kwargs: object) -> LLMResponse:
        raise ProviderError("provider 429: rate limited", 429)

    def tool_call(
        self, messages: list[dict], tools: list, **kwargs: object
    ) -> LLMResponse:
        raise ProviderError("provider 429: rate limited", 429)


def test_provider_error_becomes_failed_not_exception(tmp_path: Path):
    init_db()
    db = SessionLocal()
    repo = db.query(Repository).filter_by(full_name="demo/x").first()
    if repo is None:
        repo = Repository(full_name="demo/x")
        db.add(repo)
        db.commit()
        db.refresh(repo)
    task = Task(repo_id=repo.id, issue_number=1, title="t", state="CREATED")
    db.add(task)
    db.commit()
    db.refresh(task)
    # No exception escapes; task lands in FAILED with the cause recorded.
    result = engineer_issue(db, task, tmp_path, BoomProvider())
    assert result["verified"] is False
    db.refresh(task)
    assert task.state == "FAILED"
    db.close()
