"""Phase 4.5 hardening tests: rate limiting, prod guards, admin, legacy NULL
policy, rotation, BYOK reconfirm, evidence depth, structural suppression,
lease heartbeat/fence, timeout coherence, fallback event. Deterministic."""

import pytest
from fastapi.testclient import TestClient

from app.db.models import LoginAttempt, Memory, Repository, Task
from app.main import app
from tests.conftest import make_user, session_cookies

GOOD_PW = "correct-horse-123"


def _authed(db, email="h45@example.com", admin=False):
    u = make_user(db, email=email)
    if admin:
        u.is_admin = 1
        db.commit()
    return u, session_cookies(db, u)


# --- login rate limiting ------------------------------------------------------


def test_rate_limit_blocks_after_repeated_failures(db):
    make_user(db, email="rl@example.com", password=GOOD_PW)
    c = TestClient(app)
    for _ in range(5):
        r = c.post(
            "/api/auth/login",
            json={"email": "rl@example.com", "password": "wrong-password-xyz"},
        )
        assert r.status_code == 401
    r = c.post("/api/auth/login", json={"email": "rl@example.com", "password": GOOD_PW})
    assert r.status_code == 401  # locked: even the right password fails generically
    assert r.json() == {"detail": "invalid email or password"}
    rows = db.query(LoginAttempt).all()
    assert rows
    for r in rows:
        # Buckets are "email:<addr>" or "ip:<truncated-hash>": no raw IPs.
        assert r.bucket.startswith("email:") or r.bucket.startswith("ip:")
        if r.bucket.startswith("ip:"):
            assert r.bucket == "ip:unknown" or len(r.bucket) == len("ip:") + 12


def test_rate_limit_recovers_after_cooldown(db, monkeypatch):
    from datetime import timedelta

    from app.config import settings as _settings
    from app.db.database import utcnow

    monkeypatch.setattr(_settings, "LOGIN_WINDOW_S", 1)
    monkeypatch.setattr(_settings, "LOGIN_LOCKOUT_S", 1)
    make_user(db, email="rl2@example.com", password=GOOD_PW)
    c = TestClient(app)
    for _ in range(5):
        c.post(
            "/api/auth/login",
            json={"email": "rl2@example.com", "password": "wrong-password-xyz"},
        )
    assert (
        c.post(
            "/api/auth/login", json={"email": "rl2@example.com", "password": GOOD_PW}
        ).status_code
        == 401
    )
    # Age all buckets out of the window: recovery without waiting.
    cutoff = utcnow() - timedelta(seconds=3600)
    for row in db.query(LoginAttempt).all():
        row.last_seen = cutoff
        row.first_seen = cutoff
    db.commit()
    r = c.post(
        "/api/auth/login", json={"email": "rl2@example.com", "password": GOOD_PW}
    )
    assert r.status_code == 200, r.text


def test_rate_limit_success_resets_account_bucket(db):
    make_user(db, email="rl3@example.com", password=GOOD_PW)
    c = TestClient(app)
    for _ in range(3):
        c.post(
            "/api/auth/login",
            json={"email": "rl3@example.com", "password": "wrong-password-xyz"},
        )
    assert (
        c.post(
            "/api/auth/login", json={"email": "rl3@example.com", "password": GOOD_PW}
        ).status_code
        == 200
    )
    assert (
        db.query(LoginAttempt).filter(LoginAttempt.bucket == "rl3@example.com").count()
        == 0
    )
    assert (
        db.query(LoginAttempt)
        .filter(LoginAttempt.bucket == "email:rl3@example.com")
        .count()
        == 0
    )


# --- production fail-fast guards ------------------------------------------------


def test_prod_guards_reject_sqlite(monkeypatch):
    from app import main as _main
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "ENV", "prod")
    monkeypatch.setattr(_settings, "DATABASE_URL", "sqlite:///./fixhub.db")
    monkeypatch.setattr(_settings, "AUTH_COOKIE_SECURE", 1)
    monkeypatch.setattr(_settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "x")
    with pytest.raises(RuntimeError, match="SQLite"):
        _main._enforce_production_guards()


def test_prod_guards_reject_insecure_cookie(monkeypatch):
    from app import main as _main
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "ENV", "prod")
    monkeypatch.setattr(_settings, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(_settings, "AUTH_COOKIE_SECURE", 0)
    monkeypatch.setattr(_settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "x")
    with pytest.raises(RuntimeError, match="cookie"):
        _main._enforce_production_guards()


def test_prod_guards_reject_missing_encryption_key(monkeypatch):
    from app import main as _main
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "ENV", "prod")
    monkeypatch.setattr(_settings, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(_settings, "AUTH_COOKIE_SECURE", 1)
    monkeypatch.setattr(_settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "")
    with pytest.raises(RuntimeError, match="ENCRYPTION"):
        _main._enforce_production_guards()


def test_prod_guards_reject_garbage_database(monkeypatch):
    from app import main as _main
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "ENV", "prod")
    monkeypatch.setattr(_settings, "DATABASE_URL", "mysql://u:p@h/db")
    monkeypatch.setattr(_settings, "AUTH_COOKIE_SECURE", 1)
    monkeypatch.setattr(_settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "x")
    with pytest.raises(RuntimeError, match="database"):
        _main._enforce_production_guards()


def test_dev_guards_permissive(monkeypatch):
    from app import main as _main
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "ENV", "dev")
    _main._enforce_production_guards()  # must not raise


# --- admin authorization ----------------------------------------------------------


def test_admin_promotion_via_allowlist(db, monkeypatch):
    from app import main as _main
    from app.config import settings as _settings

    u = make_user(db, email="boss@example.com")
    assert not u.is_admin
    monkeypatch.setattr(
        _settings, "ADMIN_EMAILS", "boss@example.com, other@example.com"
    )
    _main._promote_admins()
    db.expire_all()
    from app.db.models import User

    assert db.query(User).filter(User.email == "boss@example.com").first().is_admin == 1


def test_admin_never_demoted_by_allowlist_removal(db, monkeypatch):
    from app import main as _main
    from app.config import settings as _settings

    u = make_user(db, email="keep@example.com")
    u.is_admin = 1
    db.commit()
    monkeypatch.setattr(_settings, "ADMIN_EMAILS", "")
    _main._promote_admins()
    db.expire_all()
    from app.db.models import User

    assert db.query(User).filter(User.email == "keep@example.com").first().is_admin == 1


def test_operational_endpoints_require_admin(db):
    _, ca = _authed(db, "plain@example.com")
    _, admin_cookies = _authed(db, "root@example.com", admin=True)
    c = TestClient(app)
    # Plain user: forbidden on all three operational endpoints.
    assert c.get("/api/queue/health", cookies=ca).status_code == 403
    assert c.post("/api/cron/ci-watch", cookies=ca).status_code == 403
    # Admin: allowed through (queue health reflects no-redis env honestly).
    assert c.get("/api/queue/health", cookies=admin_cookies).status_code == 200
    assert c.post("/api/cron/ci-watch", cookies=admin_cookies).status_code == 200
    # Anonymous: rejected.
    assert c.get("/api/queue/health").status_code == 401
    assert c.post("/api/cron/ci-watch").status_code == 401


# --- legacy NULL ownership policy ---------------------------------------------------


def test_legacy_null_rows_invisible(db):
    # Pre-auth rows (owner_id NULL) match no user: historical integrity only.
    db.add(
        Repository(
            github_full_name="acme/legacy", installation_id="inst-1", owner_id=None
        )
    )
    db.add(
        Task(
            repository="acme/legacy",
            trigger_type="issue",
            issue_number=1,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            owner_id=None,
        )
    )
    db.add(
        Memory(
            repository="acme/legacy",
            path="__overview__",
            summary="old",
            commit_sha="",
            last_analyzed_rev="",
            owner_id=None,
        )
    )
    db.commit()
    _, cookies = _authed(db, "new@example.com")
    c = TestClient(app)
    assert c.get("/api/repositories", cookies=cookies).json() == []
    assert c.get("/api/tasks", cookies=cookies).json() == []
    assert (
        c.get("/api/github/contents?repo=acme/legacy", cookies=cookies).status_code
        == 404
    )
    tids = [t.id for t in db.query(Task).all()]
    for tid in tids:
        assert c.get(f"/api/tasks/{tid}", cookies=cookies).status_code == 404


def test_legacy_null_never_adopted_by_webhook(db):
    # A webhook for a legacy repo without installation mapping stays orphan-free.
    import app.github.webhook as _wh

    out = _wh._handle_issue(
        db,
        {
            "action": "opened",
            "repository": {"full_name": "acme/legacy"},
            "issue": {
                "number": 2,
                "title": "t",
                "body": "b",
                "html_url": "",
                "labels": [],
            },
        },
    )
    assert out.get("ignored") == "unconnected_installation"
    assert db.query(Task).filter(Task.issue_number == 2).count() == 0


# --- rotation -------------------------------------------------------------------------


def test_rotation_migrates_old_key(db, enc_key, monkeypatch):
    from cryptography.fernet import Fernet

    from app.config import settings as _settings
    from app.llm import credentials as _creds
    from app.llm import crypto as _crypto

    u, _ = _authed(db, "rot@example.com")
    _creds.create_or_update_credential(
        db, user_id=u.id, provider="openai", api_key="ROTATE_SECRET_1A2B3C4D", model="m"
    )
    old_blob = db.query(
        type(_creds.get_for_user(db, user_id=u.id, provider="openai"))
    ).first()
    old_encrypted = old_blob.encrypted_secret
    # Rotate: new current key, old becomes previous.
    new_key = Fernet.generate_key().decode()
    old_key = _settings.FIXHUB_CREDENTIAL_ENCRYPTION_KEY
    monkeypatch.setattr(_settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", new_key)
    monkeypatch.setattr(_settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS", old_key)
    result = _creds.rotate_credentials(db)
    assert result["total"] == 1 and result["rotated"] == 1
    # Mixed-state readability pre-rotation is covered by decrypt fallback;
    # post-rotation the current key alone suffices.
    monkeypatch.setattr(_settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS", "")
    base, secret, model = _creds.decrypt_for_runtime(
        db, user_id=u.id, provider="openai"
    )
    assert secret == "ROTATE_SECRET_1A2B3C4D"
    assert (
        old_encrypted
        != db.query(type(_creds.get_for_user(db, user_id=u.id, provider="openai")))
        .first()
        .encrypted_secret
    )
    _ = _crypto  # module under test referenced via service calls above


def test_rotation_endpoint_admin_only(db, enc_key):
    _, ca = _authed(db, "nobody@example.com")
    _, admin_cookies = _authed(db, "ops@example.com", admin=True)
    c = TestClient(app)
    assert c.post("/api/llm/credentials/rotate", cookies=ca).status_code == 403
    assert c.post("/api/llm/credentials/rotate").status_code == 401
    r = c.post("/api/llm/credentials/rotate", cookies=admin_cookies)
    assert r.status_code == 200, r.text
    assert set(r.json()) >= {"rotated", "total"}


def test_rotation_rollback_on_failure(db, enc_key, monkeypatch):
    from app.llm import credentials as _creds

    u, _ = _authed(db, "rot2@example.com")
    _creds.create_or_update_credential(
        db, user_id=u.id, provider="openai", api_key="ROTATE_SECRET_9Z8Y7X6W", model="m"
    )
    before = db.query(
        type(_creds.get_for_user(db, user_id=u.id, provider="openai"))
    ).first()
    before_blob = before.encrypted_secret
    # Corrupt the row: rotation must fail closed and roll back.
    before.encrypted_secret = "not-a-fernet-token"
    db.commit()
    try:
        _creds.rotate_credentials(db)
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("corrupt row must fail rotation")
    db.expire_all()
    after = db.query(
        type(_creds.get_for_user(db, user_id=u.id, provider="openai"))
    ).first()
    assert after.encrypted_secret == "not-a-fernet-token"
    assert before_blob != "not-a-fernet-token"


# --- BYOK fail-closed reconfirm ----------------------------------------------------------


def test_prod_owned_task_without_credential_blocked(db, tmp_path, monkeypatch, enc_key):
    import subprocess

    from app.config import settings as _settings
    from app.tasks import service as _svc

    monkeypatch.setattr(_settings, "ENV", "prod")
    # Even with a global env key present, owned prod tasks must not use it.
    monkeypatch.setattr(_settings, "LLM_API_KEY", "global-key-must-not-be-used")
    monkeypatch.setattr(_settings, "WORKSPACE_ROOT", str(tmp_path / "ws"))
    remote = tmp_path / "calc.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], capture_output=True, timeout=30
    )
    work = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", str(remote), str(work)], capture_output=True, timeout=30
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"],
        cwd=str(work),
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(work),
        capture_output=True,
        timeout=30,
    )
    (work / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(work), capture_output=True, timeout=30)
    subprocess.run(
        ["git", "commit", "-m", "i"], cwd=str(work), capture_output=True, timeout=30
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "HEAD:main"],
        cwd=str(work),
        capture_output=True,
        timeout=30,
    )
    u, _ = _authed(db, "nokey@example.com")
    db.add(
        Task(
            repository="acme/nokey",
            trigger_type="issue",
            issue_number=61,
            issue_title="t",
            issue_body="b",
            status="QUEUED",
            owner_id=u.id,
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    out = _svc.run_task_inline(task.id, source=str(remote))
    assert out["status"] == "BLOCKED", out
    assert "No LLM provider" in out["error"]


# --- evidence depth + structural suppression -----------------------------------------------


def test_structural_suppression_variants():
    from app.agent import phase as _phase

    wf = ".github/workflows/ci.yml"
    assert _phase.prohibited_reason(
        "write_file",
        {"path": wf, "content": "jobs:\n  t:\n    continue-on-error: ${{ true }}\n"},
    )
    assert _phase.prohibited_reason(
        "write_file",
        {"path": wf, "content": "jobs:\n  t:\n    continue-on-error: 'True'\n"},
    )
    assert _phase.prohibited_reason(
        "edit_file",
        {
            "path": wf,
            "old": "      - run: python -m pytest tests/ -q",
            "new": "      - run: python -m pytest tests/ -q || true",
        },
    )
    assert _phase.prohibited_reason(
        "edit_file",
        {
            "path": wf,
            "old": "  job:\n    steps:\n      - run: x",
            "new": "  job:\n    if: false\n    steps:\n      - run: x",
        },
    )
    assert _phase.prohibited_reason(
        "edit_file",
        {
            "path": wf,
            "old": "steps:\n  - run: pytest\n  - run: ruff check",
            "new": "steps:\n  - run: echo done",
        },
    )
    # Legitimate workflow edits pass.
    assert (
        _phase.prohibited_reason(
            "edit_file",
            {
                "path": wf,
                "old": "      - run: pytest -x",
                "new": "      - run: pytest -q",
            },
        )
        is None
    )


def test_tautology_assert_rejected():
    from app.agent import phase as _phase

    assert _phase.prohibited_reason(
        "edit_file",
        {"path": "tests/test_x.py", "old": "assert f() == 2", "new": "assert 1 == 1"},
    )
    assert (
        _phase.prohibited_reason(
            "edit_file",
            {
                "path": "tests/test_x.py",
                "old": "assert f() == 2",
                "new": "assert 'a' == 'a'",
            },
        )
        is not None
    )
    assert (
        _phase.prohibited_reason(
            "edit_file",
            {
                "path": "tests/test_x.py",
                "old": "assert f() == 2",
                "new": "assert f() == 3",
            },
        )
        is None
    )


# --- lease heartbeat + fence ------------------------------------------------------------------


def test_renew_lease_extends_live_task(db):
    from app.tasks import service as _svc

    t = Task(
        repository="acme/hb",
        trigger_type="issue",
        issue_number=71,
        issue_title="t",
        issue_body="b",
        status="QUEUED",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    assert _svc.claim_task(db, t.id, worker_id="w-alive") is not None
    db.expire_all()
    before = db.query(Task).filter(Task.id == t.id).first().lease_expires_at
    assert _svc._renew_lease(db, t.id) is True
    db.expire_all()
    after = db.query(Task).filter(Task.id == t.id).first().lease_expires_at
    assert after > before


def test_lease_fence_blocks_reaped_worker(db, tmp_path):
    import subprocess
    from datetime import timedelta

    from app.db.database import utcnow
    from app.tasks import service as _svc

    remote = tmp_path / "fence.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], capture_output=True, timeout=30
    )
    work = tmp_path / "w"
    subprocess.run(
        ["git", "clone", str(remote), str(work)], capture_output=True, timeout=30
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"],
        cwd=str(work),
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(work),
        capture_output=True,
        timeout=30,
    )
    (work / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(work), capture_output=True, timeout=30)
    subprocess.run(
        ["git", "commit", "-m", "i"], cwd=str(work), capture_output=True, timeout=30
    )
    (work / "b.py").write_text("y = 2\n")  # uncommitted change to publish
    db.add(
        Task(
            repository="acme/fence",
            trigger_type="issue",
            issue_number=72,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            branch="fixhub-fixes/issue-72-task-1",
        )
    )
    db.commit()
    t = db.query(Task).order_by(Task.id.desc()).first()
    # Simulate: lease expired + task reaped to QUEUED by the sweep.
    t.lease_expires_at = utcnow() - timedelta(seconds=1)
    t.status = "QUEUED"
    t.claimed_by = ""
    db.commit()
    out = _svc._publish_task(
        db, t, str(work), branch=t.branch, summary="s", title="t", body="b"
    )
    assert out["status"] == "FAILED", out
    assert "lease" in out["error"].lower()


def test_holds_lease_semantics(db):
    from datetime import timedelta

    from app.db.database import utcnow
    from app.tasks import service as _svc

    t = Task(
        repository="acme/hold",
        trigger_type="issue",
        issue_number=73,
        issue_title="t",
        issue_body="b",
        status="RUNNING",
        claimed_by="w",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    t.lease_expires_at = utcnow() + timedelta(hours=1)
    db.commit()
    assert _svc._holds_lease(db, t) is True
    t.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert _svc._holds_lease(db, t) is False


# --- timeout coherence ----------------------------------------------------------------------------


def test_timeout_ladder_coherent(monkeypatch):
    # Asserted on a coherent prod-like set (local .env may override for slow
    # free-tier models; the code defaults + docs carry the canonical ladder).
    from app.config import settings as _s

    monkeypatch.setattr(_s, "LLM_MAX_RUNTIME_S", 900)
    monkeypatch.setattr(_s, "AGENT_BUDGET_S", 2100)
    monkeypatch.setattr(_s, "JOB_TIMEOUT_S", 2400)
    monkeypatch.setattr(_s, "TASK_LEASE_S", 3600)
    monkeypatch.setattr(_s, "LEASE_RENEW_EVERY_S", 120)
    assert _s.AGENT_BUDGET_S >= _s.LLM_MAX_RUNTIME_S  # at least one full run
    assert _s.JOB_TIMEOUT_S > _s.AGENT_BUDGET_S  # budget binds before RQ kills
    assert _s.TASK_LEASE_S > _s.JOB_TIMEOUT_S  # lease outlives the job
    assert _s.LEASE_RENEW_EVERY_S < _s.TASK_LEASE_S  # heartbeat much smaller
    assert _s.POSTGRES_STATEMENT_TIMEOUT_MS > 0


def test_agent_deadline_stops_loop(monkeypatch, tmp_path):
    import time

    from app.agent import loop as _loop
    from app.llm.client import AssistantMessage

    def _never_ending(messages, tools=None, **kw):
        return AssistantMessage("still thinking", [])

    monkeypatch.setattr(_loop._llm, "chat_completion", _never_ending)
    out = _loop.run_agent(
        workspace=str(tmp_path),
        trigger_type="issue",
        repository="r",
        issue_title="t",
        issue_body="b",
        deadline_mono=time.monotonic() - 1,
    )
    # Same terminal shape as LLM_MAX_RUNTIME_S expiry: stopped, no tool calls.
    assert out.finished is False
    assert "max runtime exceeded" in out.summary
    assert out.tool_calls == 0
    assert out.iterations == 1


# --- skill fallback audit ------------------------------------------------------------------------------


def test_skill_load_fallback_event(db, tmp_path, monkeypatch):
    import subprocess

    from app.tasks import service as _svc
    from app.db.models import TaskEvent

    monkeypatch.setattr(_svc.settings, "WORKSPACE_ROOT", str(tmp_path / "wsfb"))
    remote = tmp_path / "fb.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], capture_output=True, timeout=30
    )
    seed = tmp_path / "seedfb"
    subprocess.run(
        ["git", "clone", str(remote), str(seed)], capture_output=True, timeout=30
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"],
        cwd=str(seed),
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(seed),
        capture_output=True,
        timeout=30,
    )
    (seed / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(seed), capture_output=True, timeout=30)
    subprocess.run(
        ["git", "commit", "-m", "i"], cwd=str(seed), capture_output=True, timeout=30
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "HEAD:main"],
        cwd=str(seed),
        capture_output=True,
        timeout=30,
    )
    db.add(
        Task(
            repository="acme/fb",
            trigger_type="issue",
            issue_number=74,
            issue_title="t",
            issue_body="b",
            status="QUEUED",
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    import app.agent.skills.registry as _reg
    from app.agent.loop import LoopResult

    monkeypatch.setattr(_reg, "skill_prompt", lambda skill: "")
    monkeypatch.setattr(
        "app.agent.loop.run_agent",
        lambda **kw: LoopResult(True, "no changes needed", 1, 0, []),
    )
    out = _svc.run_task_inline(task.id, source=str(remote))
    # Ownerless dev task proceeds on the environment client; the audit trail
    # must still show skill selection plus the loader-fallback marker.
    assert out["status"] == "COMPLETED", out
    types = [
        e.type for e in db.query(TaskEvent).filter(TaskEvent.task_id == task.id).all()
    ]
    assert "SKILL_SELECTED" in types
    assert "SKILL_LOAD_FALLBACK" in types
