"""Final hardening: timeout-ladder + usage-limit validation, fail-closed prod.

Deterministic. Validators live in app.config; enforcement in app.main.
"""

import pytest


def _coherent(monkeypatch):
    from app.config import settings as _s

    monkeypatch.setattr(_s, "LLM_TIMEOUT_S", 120)
    monkeypatch.setattr(_s, "COMMAND_TIMEOUT_S", 180)
    monkeypatch.setattr(_s, "LLM_MAX_RUNTIME_S", 900)
    monkeypatch.setattr(_s, "AGENT_BUDGET_S", 2100)
    monkeypatch.setattr(_s, "JOB_TIMEOUT_S", 2400)
    monkeypatch.setattr(_s, "TASK_LEASE_S", 3600)
    monkeypatch.setattr(_s, "LEASE_RENEW_EVERY_S", 120)
    monkeypatch.setattr(_s, "TOOL_CLEANUP_GRACE_S", 30)
    monkeypatch.setattr(_s, "LLM_MAX_ITERATIONS", 40)
    monkeypatch.setattr(_s, "MAX_LLM_REQUESTS_PER_TASK", 40)
    monkeypatch.setattr(_s, "MAX_TASKS_PER_USER_PER_DAY", 20)
    monkeypatch.setattr(_s, "MAX_RUNNING_TASKS_GLOBAL", 20)
    monkeypatch.setattr(_s, "MAX_RUNNING_PER_USER", 5)
    return _s


def test_ladder_accepts_coherent_prod(monkeypatch):
    from app.config import validate_timeout_ladder, validate_usage_limits

    _s = _coherent(monkeypatch)
    monkeypatch.setattr(_s, "ENV", "prod")
    validate_timeout_ladder(_s)
    validate_usage_limits(_s)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("AGENT_BUDGET_S", 2400, "AGENT_BUDGET_S"),
        ("JOB_TIMEOUT_S", 2100, "JOB_TIMEOUT_S"),
        ("JOB_TIMEOUT_S", 3600, "JOB_TIMEOUT_S"),
        ("TASK_LEASE_S", 2400, "TASK_LEASE_S"),
        ("LEASE_RENEW_EVERY_S", 3600, "LEASE_RENEW"),
        ("TOOL_CLEANUP_GRACE_S", 120, "TOOL_CLEANUP_GRACE_S"),
        ("LLM_TIMEOUT_S", 180, "LLM_TIMEOUT_S"),
        ("TASK_LEASE_S", 3600 + 2 * 2400 + 1, "arbitrarily huge"),
    ],
)
def test_ladder_rejects_unsafe_combos(monkeypatch, field, value, match):
    from app.config import validate_timeout_ladder

    _s = _coherent(monkeypatch)
    monkeypatch.setattr(_s, field, value)
    with pytest.raises(ValueError, match=match):
        validate_timeout_ladder(_s)


def test_ladder_rejects_lease_below_provider_plus_tool(monkeypatch):
    from app.config import validate_timeout_ladder

    _s = _coherent(monkeypatch)
    # 120+180+120+30 = 450 < 3600 ok; force lease just under the floor.
    monkeypatch.setattr(_s, "TASK_LEASE_S", 449)
    monkeypatch.setattr(_s, "JOB_TIMEOUT_S", 448)
    monkeypatch.setattr(_s, "AGENT_BUDGET_S", 447)
    monkeypatch.setattr(_s, "LLM_MAX_RUNTIME_S", 447)
    with pytest.raises(ValueError, match="must exceed"):
        validate_timeout_ladder(_s)


def test_dev_incoherent_needs_explicit_flag(monkeypatch):
    from app import main as _main
    from app.config import settings as _s

    _coherent(monkeypatch)
    monkeypatch.setattr(_s, "ENV", "dev")
    monkeypatch.setattr(_s, "JOB_TIMEOUT_S", 1800)  # legacy incoherent value
    monkeypatch.setattr(_s, "DEV_SLOW_MODEL", 0)
    with pytest.raises(RuntimeError, match="DEV_SLOW_MODEL"):
        _main._validate_startup_timeouts()
    monkeypatch.setattr(_s, "DEV_SLOW_MODEL", 1)
    _main._validate_startup_timeouts()  # explicit relaxation: boots


def test_usage_validator_rejects_nonsense(monkeypatch):
    from app.config import validate_usage_limits

    _s = _coherent(monkeypatch)
    monkeypatch.setattr(_s, "MAX_LLM_REQUESTS_PER_TASK", 0)
    with pytest.raises(ValueError, match="MAX_LLM_REQUESTS_PER_TASK"):
        validate_usage_limits(_s)


def test_usage_validator_rejects_request_cap_below_iterations(monkeypatch):
    from app.config import validate_usage_limits

    _s = _coherent(monkeypatch)
    monkeypatch.setattr(_s, "MAX_LLM_REQUESTS_PER_TASK", 10)
    monkeypatch.setattr(_s, "LLM_MAX_ITERATIONS", 40)
    with pytest.raises(ValueError, match="LLM_MAX_ITERATIONS"):
        validate_usage_limits(_s)


def test_prod_rejects_unbounded_workers(monkeypatch):
    from app import main as _main
    from app.config import settings as _s

    _coherent(monkeypatch)
    monkeypatch.setattr(_s, "ENV", "prod")
    monkeypatch.setattr(_s, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(_s, "AUTH_COOKIE_SECURE", 1)
    monkeypatch.setattr(_s, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "x")
    monkeypatch.setattr(_s, "MAX_RUNNING_TASKS_GLOBAL", 0)
    with pytest.raises(RuntimeError, match="unbounded"):
        _main._enforce_production_guards()


def test_prod_rejects_bad_ladder(monkeypatch):
    from app import main as _main
    from app.config import settings as _s

    _coherent(monkeypatch)
    monkeypatch.setattr(_s, "ENV", "prod")
    monkeypatch.setattr(_s, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(_s, "AUTH_COOKIE_SECURE", 1)
    monkeypatch.setattr(_s, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "x")
    monkeypatch.setattr(_s, "JOB_TIMEOUT_S", 1800)
    with pytest.raises(RuntimeError, match="production startup"):
        _main._enforce_production_guards()


def test_no_silent_redis_fallback(db):
    """Redis down -> QUEUED + clear error, never inline execution."""
    from app.tasks import queue as _queue

    from tests.test_phase4 import _task

    t = _task(db)
    out = _queue.enqueue_task(t.id)
    # Either redis is up (enqueued) or the failure is explicit. What must
    # never happen is a silent inline run: status stays QUEUED on failure.
    if not out.get("enqueued"):
        assert "redis" in out.get("error", "").lower()
        db.expire_all()
        from app.db.models import Task

        assert db.query(Task).filter(Task.id == t.id).first().status == "QUEUED"
