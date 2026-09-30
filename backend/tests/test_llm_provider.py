"""LLM provider switch: LLM_PROVIDER selects, legacy LLM_API_KEY overrides."""
from app.config import active_llm, settings as _settings


def test_provider_bynara(monkeypatch):
    monkeypatch.setattr(_settings, "LLM_PROVIDER", "bynara")
    monkeypatch.setattr(_settings, "LLM_API_KEY", "")
    monkeypatch.setattr(_settings, "BYNARA_BASE_URL", "https://router.bynara.id/v1")
    monkeypatch.setattr(_settings, "BYNARA_API_KEY", "b-key")
    monkeypatch.setattr(_settings, "BYNARA_MODEL", "nemotron-3.5-lightning-free")
    assert active_llm() == ("https://router.bynara.id/v1", "b-key", "nemotron-3.5-lightning-free")


def test_provider_xkiro(monkeypatch):
    monkeypatch.setattr(_settings, "LLM_PROVIDER", "xkiro")
    monkeypatch.setattr(_settings, "LLM_API_KEY", "")
    monkeypatch.setattr(_settings, "XKIRO_BASE_URL", "https://api.xkiro.com/v1")
    monkeypatch.setattr(_settings, "XKIRO_API_KEY", "x-key")
    monkeypatch.setattr(_settings, "XKIRO_MODEL", "qwen/qwen3-coder-plus:free")
    assert active_llm() == ("https://api.xkiro.com/v1", "x-key", "qwen/qwen3-coder-plus:free")


def test_legacy_override_wins(monkeypatch):
    monkeypatch.setattr(_settings, "LLM_PROVIDER", "xkiro")
    monkeypatch.setattr(_settings, "LLM_API_KEY", "legacy-key")
    monkeypatch.setattr(_settings, "LLM_BASE_URL", "https://legacy.example/v1")
    monkeypatch.setattr(_settings, "LLM_MODEL", "legacy-model")
    assert active_llm() == ("https://legacy.example/v1", "legacy-key", "legacy-model")


def test_missing_key_blocks(monkeypatch):
    import pytest

    from app.llm.client import LLMBlockedError, _active

    monkeypatch.setattr(_settings, "LLM_PROVIDER", "bynara")
    monkeypatch.setattr(_settings, "LLM_API_KEY", "")
    monkeypatch.setattr(_settings, "BYNARA_API_KEY", "")
    with pytest.raises(LLMBlockedError):
        _active()


def _ok_resp():
    import httpx

    return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


def _cfg(monkeypatch):
    monkeypatch.setattr(_settings, "LLM_PROVIDER", "bynara")
    monkeypatch.setattr(_settings, "LLM_API_KEY", "")
    monkeypatch.setattr(_settings, "BYNARA_BASE_URL", "https://x.test/v1")
    monkeypatch.setattr(_settings, "BYNARA_API_KEY", "k")
    monkeypatch.setattr(_settings, "BYNARA_MODEL", "m")
    monkeypatch.setattr(_settings, "LLM_RETRY_BASE_S", 0.0)


def test_retry_then_success(monkeypatch):
    import httpx

    from app.llm import client as _llm

    _cfg(monkeypatch)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return _ok_resp()

    monkeypatch.setattr(_llm.httpx, "post", fake_post)
    out = _llm.chat_completion([{"role": "user", "content": "hi"}])
    assert out.content == "hi"
    assert calls["n"] == 3


def test_truncated_body_retried(monkeypatch):
    """Peer disconnects mid-body (httpx.RemoteProtocolError) are transient."""
    import httpx

    from app.llm import client as _llm

    _cfg(monkeypatch)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        return _ok_resp()

    monkeypatch.setattr(_llm.httpx, "post", fake_post)
    out = _llm.chat_completion([{"role": "user", "content": "hi"}])
    assert out.content == "hi"
    assert calls["n"] == 3


def test_200_error_envelope_retried(monkeypatch):
    import httpx

    from app.llm import client as _llm

    _cfg(monkeypatch)
    seq = [httpx.Response(200, json={"error": {"type": "upstream_error"}}), _ok_resp()]
    monkeypatch.setattr(_llm.httpx, "post", lambda *a, **k: seq.pop(0))
    out = _llm.chat_completion([{"role": "user", "content": "hi"}])
    assert out.content == "hi"


def test_exhausted_retries_raise(monkeypatch):
    import httpx
    import pytest

    from app.llm import client as _llm

    _cfg(monkeypatch)
    monkeypatch.setattr(_settings, "LLM_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(_llm.httpx, "post", lambda *a, **k: httpx.Response(429, text="slow"))
    with pytest.raises(_llm.LLMError):
        _llm.chat_completion([{"role": "user", "content": "hi"}])


def test_auth_fails_fast_without_retry(monkeypatch):
    import httpx
    import pytest

    from app.llm import client as _llm

    _cfg(monkeypatch)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return httpx.Response(401, text="nope")

    monkeypatch.setattr(_llm.httpx, "post", fake_post)
    with pytest.raises(_llm.LLMBlockedError):
        _llm.chat_completion([{"role": "user", "content": "hi"}])
    assert calls["n"] == 1
