"""Phase 3 BYOK tests: storage, authz, provider matrix, runtime isolation,
leak sweep, usage, delete. Deterministic: mocked HTTP, fake key
TEST_SECRET_123456789 (never a real credential)."""

import subprocess
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.db.models import LLMCredential, LLMUsage, Task, TaskEvent
from app.main import app
from tests.conftest import make_user, session_cookies

FAKE_KEY_A = "TEST_SECRET_123456789_AAAA"
FAKE_KEY_B = "TEST_SECRET_123456789_BBBB"


@pytest.fixture()
def enc_key(monkeypatch):
    from cryptography.fernet import Fernet

    from app.config import settings

    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", key)
    return key


def _authed(db, email="byok@example.com"):
    u = make_user(db, email=email)
    return u, session_cookies(db, u)


def _save(
    db, user, provider="openai", key=FAKE_KEY_A, model="gpt-4o-mini", base_url=""
):
    from app.llm import credentials as _creds

    return _creds.create_or_update_credential(
        db,
        user_id=user.id,
        provider=provider,
        api_key=key,
        model=model,
        base_url=base_url,
    )


# --- storage ---------------------------------------------------------------


def test_key_encrypted_at_rest(db, enc_key):
    u, _ = _authed(db)
    row = _save(db, u)
    assert row.encrypted_secret and FAKE_KEY_A not in row.encrypted_secret
    assert row.key_hint == "••••AAAA"
    assert "TEST_SECRET" not in row.key_hint


def test_decrypt_round_trip(db, enc_key):
    from app.llm import crypto as _crypto

    u, _ = _authed(db)
    _save(db, u)
    row = db.query(LLMCredential).filter_by(user_id=u.id).first()
    assert _crypto.decrypt_secret(row.encrypted_secret) == FAKE_KEY_A


def test_metadata_never_exposes_secret(db, enc_key):
    u, cookies = _authed(db)
    _save(db, u)
    c = TestClient(app)
    body = c.get("/api/llm/credentials", cookies=cookies).json()
    blob = str(body)
    assert FAKE_KEY_A not in blob and "encrypted_secret" not in blob
    assert body[0]["key_hint"] == "••••AAAA"


def test_missing_master_key_fails_closed(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "FIXHUB_CREDENTIAL_ENCRYPTION_KEY", "")
    u, cookies = _authed(db)
    c = TestClient(app)
    r = c.post(
        "/api/llm/credentials",
        json={"provider": "openai", "api_key": FAKE_KEY_A},
        cookies=cookies,
    )
    assert r.status_code == 503, r.text


# --- authorization ----------------------------------------------------------


def test_cross_user_credential_isolation(db, enc_key):
    ua, ca = _authed(db, "bk-a@example.com")
    ub, cb = _authed(db, "bk-b@example.com")
    _save(db, ua)
    c = TestClient(app)
    assert c.get("/api/llm/credentials", cookies=cb).json() == []
    assert c.delete("/api/llm/credentials/openai", cookies=cb).status_code == 404
    assert (
        c.post(
            "/api/llm/credentials/test", json={"provider": "openai"}, cookies=cb
        ).status_code
        == 404
    )
    # A still intact.
    assert len(c.get("/api/llm/credentials", cookies=ca).json()) == 1


def test_user_id_tampering_ignored(db, enc_key):
    ua, ca = _authed(db, "bk-c@example.com")
    ub, _cb = _authed(db, "bk-d@example.com")
    c = TestClient(app)
    r = c.post(
        "/api/llm/credentials",
        json={"provider": "openai", "api_key": FAKE_KEY_A, "user_id": ub.id},
        cookies=ca,
    )
    assert r.status_code == 200, r.text
    rows = db.query(LLMCredential).all()
    assert len(rows) == 1 and rows[0].user_id == ua.id


def test_unauthenticated_rejected(db):
    c = TestClient(app)
    assert c.get("/api/llm/credentials").status_code == 401
    assert c.post("/api/llm/credentials", json={}).status_code == 401
    assert c.post("/api/llm/credentials/test", json={}).status_code == 401
    assert c.delete("/api/llm/credentials/openai").status_code == 401


# --- create validation -------------------------------------------------------


def test_create_valid_and_update(db, enc_key):
    ua, ca = _authed(db, "bk-e@example.com")
    c = TestClient(app)
    r = c.post(
        "/api/llm/credentials",
        json={"provider": "openai", "api_key": FAKE_KEY_A, "model": "gpt-4o"},
        cookies=ca,
    )
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "gpt-4o"
    r = c.post(
        "/api/llm/credentials",
        json={"provider": "openai", "api_key": FAKE_KEY_B, "model": "gpt-4o-mini"},
        cookies=ca,
    )
    assert r.json()["key_hint"] == "••••BBBB"
    assert db.query(LLMCredential).filter_by(user_id=ua.id).count() == 1


def test_create_rejects_unknown_provider(db, enc_key):
    _, ca = _authed(db, "bk-f@example.com")
    c = TestClient(app)
    r = c.post(
        "/api/llm/credentials",
        json={"provider": "anthropic", "api_key": FAKE_KEY_A},
        cookies=ca,
    )
    assert r.status_code == 400, r.text


def test_create_rejects_malformed(db, enc_key):
    _, ca = _authed(db, "bk-g@example.com")
    c = TestClient(app)
    assert (
        c.post(
            "/api/llm/credentials",
            json={"provider": "openai", "api_key": "tiny"},
            cookies=ca,
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/api/llm/credentials",
            json={"provider": "custom", "api_key": FAKE_KEY_A, "model": "m"},
            cookies=ca,
        ).status_code
        == 400
    )
    r = c.post(
        "/api/llm/credentials",
        json={
            "provider": "custom",
            "api_key": FAKE_KEY_A,
            "model": "m",
            "base_url": "https://gw.example.com/v1",
        },
        cookies=ca,
    )
    assert r.status_code == 200, r.text


# --- provider matrix (mocked HTTP) --------------------------------------------


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = f"status {status_code}"


def _ok_models(*a, **k):
    return _Resp(200)


def test_probe_matrix(db, enc_key, monkeypatch):
    import httpx

    from app.api import llm_keys as _api

    ua, ca = _authed(db, "bk-h@example.com")
    _save(db, ua)
    c = TestClient(app)

    monkeypatch.setattr(httpx, "get", _ok_models)
    r = c.post("/api/llm/credentials/test", json={"provider": "openai"}, cookies=ca)
    assert r.json()["success"] is True, r.text
    row = db.query(LLMCredential).filter_by(user_id=ua.id).first()
    assert row.status == "verified" and row.last_tested_at is not None

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(401))
    r = c.post("/api/llm/credentials/test", json={"provider": "openai"}, cookies=ca)
    assert r.json()["success"] is False
    assert r.json()["error_category"] == "invalid_credential"
    assert FAKE_KEY_A not in r.text

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(429))
    r = c.post("/api/llm/credentials/test", json={"provider": "openai"}, cookies=ca)
    assert r.json()["error_category"] == "rate_limited"

    # /models missing -> chat fallback success (may incur usage, tested cheaply here).
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(404))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(200))
    r = c.post("/api/llm/credentials/test", json={"provider": "openai"}, cookies=ca)
    assert r.json()["success"] is True, r.text
    _ = _api  # silence unused (documents unit under test)


def test_probe_transport_failure_safe(db, enc_key, monkeypatch):
    import httpx

    ua, ca = _authed(db, "bk-i@example.com")
    _save(db, ua)

    def _boom(*a, **k):
        raise httpx.ConnectError("dns down")

    monkeypatch.setattr(httpx, "get", _boom)
    c = TestClient(app)
    r = c.post("/api/llm/credentials/test", json={"provider": "openai"}, cookies=ca)
    assert r.json()["success"] is False
    assert FAKE_KEY_A not in r.text


def test_client_error_sanitization(enc_key):
    from app.llm import client as _cli

    with _cli.use_runtime_credential("https://x.example.com", FAKE_KEY_A, "m"):
        msg = _cli._sanitize(f"boom {FAKE_KEY_A} tail")
    assert FAKE_KEY_A not in msg and "[REDACTED]" in msg


def test_provider_registry():
    from app.llm import providers as _p

    assert _p.resolve_endpoint(provider="openai", model="")["model"] == "gpt-4o-mini"
    assert (
        _p.resolve_endpoint(
            provider="custom", base_url="https://g.example.com/v1", model="m"
        )["base_url"]
        == "https://g.example.com/v1"
    )
    try:
        _p.resolve_endpoint(provider="anthropic", model="m")
    except _p.UnknownProvider:
        pass
    else:  # pragma: no cover
        raise AssertionError("anthropic must be rejected in Phase 3")
    assert (
        _p.resolve_endpoint(provider="openai", model="user-model")["model"]
        == "user-model"
    )


def test_usage_extraction():
    from app.llm import client as _cli

    full = _cli._extract_usage(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    )
    assert (full.input_tokens, full.output_tokens, full.total_tokens) == (10, 5, 15)
    empty = _cli._extract_usage({})
    assert empty.input_tokens is None and empty.total_tokens is None


# --- runtime isolation ----------------------------------------------------------


def _fixture_repo(tmp_path):
    def _git(*args, cwd=None):
        r = subprocess.run(
            ["git", *args],
            cwd=cwd or str(tmp_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, (args, r.stderr[-500:])
        return r

    remote = tmp_path / "calc.git"
    _git("init", "--bare", str(remote))
    work = tmp_path / "src"
    _git("clone", str(remote), str(work))
    _git("config", "user.email", "t@t.t", cwd=str(work))
    _git("config", "user.name", "t", cwd=str(work))
    (work / "calc.py").write_text("def add(a, b):\n    return a - b  # BUG\n")
    (work / "test_calc.py").write_text(
        "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    _git("add", "-A", cwd=str(work))
    _git("commit", "-m", "init", cwd=str(work))
    _git("push", "-u", "origin", "HEAD:main", cwd=str(work))
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=str(remote),
        capture_output=True,
        timeout=30,
    )
    return str(remote)


def _scripted(monkeypatch, script):
    from app.agent import loop as _loop
    from app.llm.client import AssistantMessage, ToolCall  # noqa: F401 (re-export check)

    calls = {"i": 0}

    def fake_chat(messages, tools=None, **kw):
        m = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return m

    monkeypatch.setattr(_loop._llm, "chat_completion", fake_chat)


def _calc_script():
    from app.llm.client import AssistantMessage, ToolCall

    return [
        AssistantMessage("", [ToolCall("1", "read_file", {"path": "test_calc.py"})]),
        AssistantMessage("", [ToolCall("2", "read_file", {"path": "calc.py"})]),
        AssistantMessage(
            "",
            [
                ToolCall(
                    "3",
                    "edit_file",
                    {
                        "path": "calc.py",
                        "old": "return a - b  # BUG",
                        "new": "return a + b",
                    },
                )
            ],
        ),
        AssistantMessage(
            "",
            [
                ToolCall(
                    "4",
                    "run_command",
                    {"command": 'python -c "from calc import add; assert add(2,3)==5"'},
                )
            ],
        ),
        AssistantMessage("Fixed.", []),
    ]


def test_task_uses_owner_credential(db, tmp_path, monkeypatch, enc_key):
    from app.llm import client as _cli
    from app.tasks import service as _svc

    monkeypatch.setattr(_svc.settings, "WORKSPACE_ROOT", str(tmp_path / "ws"))
    remote = _fixture_repo(tmp_path)
    ua, _ = _authed(db, "bk-j@example.com")
    _save(db, ua, key=FAKE_KEY_A)
    db.add(
        Task(
            repository="acme/byok",
            trigger_type="issue",
            issue_number=51,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            owner_id=ua.id,
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    _scripted(monkeypatch, _calc_script())

    seen = {}
    orig = _cli.use_runtime_credential

    @contextmanager
    def rec(base, key, model):
        seen.update(base=base, key=key, model=model)
        with orig(base, key, model):
            yield

    monkeypatch.setattr(_cli, "use_runtime_credential", rec)
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "COMPLETED", out
    assert seen.get("key") == FAKE_KEY_A
    assert seen.get("model") == "gpt-4o-mini"


def test_user_b_task_uses_b_credential(db, tmp_path, monkeypatch, enc_key):
    from app.llm import client as _cli
    from app.tasks import service as _svc

    monkeypatch.setattr(_svc.settings, "WORKSPACE_ROOT", str(tmp_path / "ws2"))
    remote = _fixture_repo(tmp_path)
    ua, _ = _authed(db, "bk-k1@example.com")
    ub, _ = _authed(db, "bk-k2@example.com")
    _save(db, ua, key=FAKE_KEY_A)
    _save(db, ub, key=FAKE_KEY_B)
    db.add(
        Task(
            repository="acme/byok",
            trigger_type="issue",
            issue_number=52,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            owner_id=ub.id,
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    _scripted(monkeypatch, _calc_script())

    seen = {}
    orig = _cli.use_runtime_credential

    @contextmanager
    def rec(base, key, model):
        seen.update(key=key)
        with orig(base, key, model):
            yield

    monkeypatch.setattr(_cli, "use_runtime_credential", rec)
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "COMPLETED", out
    assert seen.get("key") == FAKE_KEY_B


def test_missing_credential_blocks_before_agent(db, tmp_path, monkeypatch, enc_key):
    from app.agent import loop as _loop
    from app.tasks import service as _svc

    monkeypatch.setattr(_svc.settings, "WORKSPACE_ROOT", str(tmp_path / "ws3"))
    remote = _fixture_repo(tmp_path)
    ua, _ = _authed(db, "bk-l@example.com")
    db.add(
        Task(
            repository="acme/byok",
            trigger_type="issue",
            issue_number=53,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            owner_id=ua.id,
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()

    calls = {"n": 0}

    def _boom(messages, tools=None, **kw):
        calls["n"] += 1
        raise AssertionError("agent must not run without a credential")

    monkeypatch.setattr(_loop._llm, "chat_completion", _boom)
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "BLOCKED", out
    assert "No LLM provider" in out["error"]
    assert calls["n"] == 0


def test_prod_ownerless_fails_closed(db, tmp_path, monkeypatch, enc_key):
    from app.tasks import service as _svc

    monkeypatch.setattr(_svc.settings, "WORKSPACE_ROOT", str(tmp_path / "ws4"))
    monkeypatch.setattr(_svc.settings, "ENV", "prod")
    remote = _fixture_repo(tmp_path)
    db.add(
        Task(
            repository="acme/byok",
            trigger_type="issue",
            issue_number=54,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "BLOCKED", out
    assert "owning user" in out["error"]


# --- leak sweep -------------------------------------------------------------------


def test_fake_key_absent_everywhere(db, tmp_path, monkeypatch, enc_key):
    from app.tasks import service as _svc

    monkeypatch.setattr(_svc.settings, "WORKSPACE_ROOT", str(tmp_path / "ws5"))
    remote = _fixture_repo(tmp_path)
    ua, ca = _authed(db, "bk-m@example.com")
    _save(db, ua, key=FAKE_KEY_A)
    db.add(
        Task(
            repository="acme/byok",
            trigger_type="issue",
            issue_number=55,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            owner_id=ua.id,
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    _scripted(monkeypatch, _calc_script())
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "COMPLETED", out

    c = TestClient(app)
    for payload in (
        c.get("/api/llm/credentials", cookies=ca).text,
        c.get(f"/api/tasks/{task.id}", cookies=ca).text,
        c.get(f"/api/tasks/{task.id}/events", cookies=ca).text,
    ):
        assert FAKE_KEY_A not in payload
    for e in db.query(TaskEvent).filter(TaskEvent.task_id == task.id).all():
        assert FAKE_KEY_A not in (e.data_json or "")
    db.expire_all()
    t = db.query(Task).filter(Task.id == task.id).first()
    assert FAKE_KEY_A not in (t.error or "")
    for u in db.query(LLMUsage).filter(LLMUsage.task_id == task.id).all():
        assert FAKE_KEY_A not in str(u.provider) + str(u.model) + str(u.error_category)


# --- usage --------------------------------------------------------------------------


def test_usage_recorded_on_success(db, tmp_path, monkeypatch, enc_key):
    from app.tasks import service as _svc

    monkeypatch.setattr(_svc.settings, "WORKSPACE_ROOT", str(tmp_path / "ws6"))
    remote = _fixture_repo(tmp_path)
    ua, _ = _authed(db, "bk-n@example.com")
    _save(db, ua, key=FAKE_KEY_A, model="gpt-4o")
    db.add(
        Task(
            repository="acme/byok",
            trigger_type="issue",
            issue_number=56,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            owner_id=ua.id,
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    _scripted(monkeypatch, _calc_script())
    assert _svc.run_task_inline(task.id, source=remote)["status"] == "COMPLETED"
    rows = db.query(LLMUsage).filter(LLMUsage.task_id == task.id).all()
    assert len(rows) >= 1
    row = rows[0]
    assert row.user_id == ua.id and row.provider == "openai" and row.model == "gpt-4o"
    assert row.success == 1 and row.tool_calls > 0
    # Mocked LLM makes no real client calls: counts null, never fabricated.
    assert row.input_tokens is None and row.total_tokens is None


def test_usage_recorded_on_crash(db, monkeypatch, enc_key):
    from app.agent import loop as _loop
    from app.tasks import service as _svc

    ua, _ = _authed(db, "bk-o@example.com")
    _save(db, ua, key=FAKE_KEY_A)
    db.add(
        Task(
            repository="acme/byok",
            trigger_type="issue",
            issue_number=57,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            owner_id=ua.id,
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()

    def _boom(**kw):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(_loop, "run_agent", _boom)
    scope = _svc._resolve_llm_scope(db, task)
    assert not isinstance(scope, dict)
    try:
        _svc._run_agent_tracked(
            db, task, scope, workspace=".", trigger_type="issue", repository="r"
        )
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")
    rows = db.query(LLMUsage).filter(LLMUsage.task_id == task.id).all()
    assert len(rows) == 1
    assert rows[0].success == 0 and rows[0].error_category == "RuntimeError"


# --- delete ---------------------------------------------------------------------------


def test_delete_revokes_and_blocks_new_tasks(db, tmp_path, monkeypatch, enc_key):
    from app.tasks import service as _svc

    ua, ca = _authed(db, "bk-p@example.com")
    _save(db, ua, key=FAKE_KEY_A)
    c = TestClient(app)
    assert c.delete("/api/llm/credentials/openai", cookies=ca).status_code == 200
    assert c.get("/api/llm/credentials", cookies=ca).json() == []
    assert db.query(LLMCredential).filter_by(user_id=ua.id).count() == 0

    monkeypatch.setattr(_svc.settings, "WORKSPACE_ROOT", str(tmp_path / "ws7"))
    remote = _fixture_repo(tmp_path)
    db.add(
        Task(
            repository="acme/byok",
            trigger_type="issue",
            issue_number=58,
            issue_title="t",
            issue_body="b",
            status="RUNNING",
            owner_id=ua.id,
        )
    )
    db.commit()
    task = db.query(Task).order_by(Task.id.desc()).first()
    out = _svc.run_task_inline(task.id, source=remote)
    assert out["status"] == "BLOCKED", out
    assert "No LLM provider" in out["error"]
