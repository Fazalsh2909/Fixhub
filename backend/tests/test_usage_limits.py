"""Final hardening: per-user LLM usage limits + BYOK ownership safety.

Deterministic, SQLite + mocks. Token caps use nullable provider fields: no
usage data means no fabrication (non-token limits still bind).
"""

from app.db.models import LLMUsage, Task
from tests.conftest import make_user


def _task(db, owner=None, repo="acme/limits"):
    t = Task(
        repository=repo,
        trigger_type="issue",
        issue_number=1,
        issue_title="t",
        issue_body="b",
        status="QUEUED",
        owner_id=owner.id if owner else None,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _usage(db, owner, task, requests=1, inp=None, outp=None):
    from app.db.database import utcnow

    db.add(
        LLMUsage(
            user_id=owner.id if owner else None,
            task_id=task.id,
            provider="openai",
            model="m",
            started_at=utcnow(),
            completed_at=utcnow(),
            latency_ms=10,
            request_count=requests,
            tool_calls=0,
            input_tokens=inp,
            output_tokens=outp,
            total_tokens=None,
            success=1,
            error_category="",
        )
    )
    db.commit()


def test_request_cap_per_task(db, monkeypatch):
    from app.config import settings as _s
    from app.tasks import service as _svc

    monkeypatch.setattr(_s, "MAX_LLM_REQUESTS_PER_TASK", 3)
    u = make_user(db, email="req@example.com")
    t = _task(db, owner=u)
    assert _svc._usage_limit_reason(db, t) == ""
    _usage(db, u, t, requests=2)
    assert _svc._usage_limit_reason(db, t) == ""
    _usage(db, u, t, requests=1)
    assert "max LLM requests" in _svc._usage_limit_reason(db, t)


def test_null_usage_never_fabricated(db, monkeypatch):
    """Provider omitted token counts: token caps must NOT fire on nothing."""
    from app.config import settings as _s
    from app.tasks import service as _svc

    monkeypatch.setattr(_s, "MAX_LLM_REQUESTS_PER_TASK", 1000)
    monkeypatch.setattr(_s, "MAX_INPUT_TOKENS_PER_TASK", 100)
    monkeypatch.setattr(_s, "MAX_OUTPUT_TOKENS_PER_TASK", 100)
    u = make_user(db, email="null@example.com")
    t = _task(db, owner=u)
    _usage(db, u, t, requests=1, inp=None, outp=None)
    assert _svc._usage_limit_reason(db, t) == ""


def test_token_caps_enforced_when_reported(db, monkeypatch):
    from app.config import settings as _s
    from app.tasks import service as _svc

    monkeypatch.setattr(_s, "MAX_LLM_REQUESTS_PER_TASK", 1000)
    monkeypatch.setattr(_s, "MAX_INPUT_TOKENS_PER_TASK", 50)
    monkeypatch.setattr(_s, "MAX_OUTPUT_TOKENS_PER_TASK", 0)
    u = make_user(db, email="tok@example.com")
    t = _task(db, owner=u)
    _usage(db, u, t, requests=1, inp=60, outp=5)
    assert "input tokens" in _svc._usage_limit_reason(db, t)


def test_limit_hit_makes_no_provider_call(monkeypatch):
    import app.agent.loop as _loopmod

    calls = []

    def _fake_chat(messages, tools=None, **kw):
        calls.append(1)
        raise AssertionError("provider must not be called after limit")

    monkeypatch.setattr(_loopmod._llm, "chat_completion", _fake_chat)
    out = _loopmod.run_agent(
        workspace=".",
        trigger_type="issue",
        repository="acme/limits",
        issue_title="t",
        issue_body="b",
        request_guard=lambda: "max LLM requests per task reached (40)",
    )
    assert out.usage_limited is True and out.finished is False
    assert calls == []
    assert any(e.get("tool") == "usage_limit" for e in out.events)


def test_daily_cap_blocks_enqueue(db, monkeypatch):
    from app.config import settings as _s
    from app.tasks import queue as _queue

    monkeypatch.setattr(_s, "MAX_TASKS_PER_USER_PER_DAY", 2)
    monkeypatch.setattr(_s, "MAX_RUNNING_TASKS_GLOBAL", 0)
    monkeypatch.setattr(_s, "MAX_RUNNING_PER_USER", 0)
    monkeypatch.setattr(_s, "MAX_RUNNING_PER_REPO", 0)
    u = make_user(db, email="daily@example.com")
    _task(db, owner=u)
    _task(db, owner=u)
    t3 = _task(db, owner=u)
    out = _queue.enqueue_task(t3.id)
    assert out.get("limited") is True
    assert "daily" in out.get("error", "")


def test_byok_accounting_is_owner_scoped(db, monkeypatch):
    """User A at cap must not consume User B's budget (same provider)."""
    from app.config import settings as _s
    from app.tasks import service as _svc

    monkeypatch.setattr(_s, "MAX_LLM_REQUESTS_PER_TASK", 2)
    a = make_user(db, email="a-cap@example.com")
    b = make_user(db, email="b-free@example.com")
    ta, tb = _task(db, owner=a), _task(db, owner=b)
    _usage(db, a, ta, requests=2)
    assert "max LLM requests" in _svc._usage_limit_reason(db, ta)
    assert _svc._usage_limit_reason(db, tb) == ""


def test_run_inline_fails_fast_on_spent_budget(db, monkeypatch, enc_key):
    from app.config import settings as _s
    from app.tasks import service as _svc

    monkeypatch.setattr(_s, "MAX_LLM_REQUESTS_PER_TASK", 1)
    monkeypatch.setattr(_s, "MAX_TASKS_PER_USER_PER_DAY", 1000)
    u = make_user(db, email="spent@example.com")
    t = _task(db, owner=u)
    _usage(db, u, t, requests=1)
    out = _svc.run_task_inline(t.id, source="", worker_id="w-test")
    assert out["status"] == "FAILED"
    assert out.get("usage_limited") is True
    db.expire_all()
    from app.db.models import TaskEvent

    types = {
        e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == t.id).all()
    }
    assert "USAGE_LIMIT_HIT" in types
