"""Provider retry tests: 429/5xx back off (honoring Retry-After), then succeed."""

import httpx
import pytest

from app.llm.openrouter import OpenRouterProvider, ProviderError


def _provider() -> OpenRouterProvider:
    return OpenRouterProvider(
        base_url="https://x.test", api_key="k", model="m", timeout_s=5
    )


def test_backoff_honors_retry_after():
    req = httpx.Request("POST", "https://x.test/chat/completions")
    res = httpx.Response(429, headers={"retry-after": "2"}, request=req)
    wait = OpenRouterProvider._backoff_s(res, 0)
    assert 2 <= wait < 4


def test_backoff_exponential_capped():
    req = httpx.Request("POST", "https://x.test/chat/completions")
    res = httpx.Response(500, request=req)
    assert 5 <= OpenRouterProvider._backoff_s(res, 0) < 7
    assert OpenRouterProvider._backoff_s(res, 10) <= 61


def test_post_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}
    slept = []

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            if calls["n"] < 2:
                calls["n"] += 1
                raise httpx.HTTPStatusError(
                    "limited",
                    request=httpx.Request("POST", "https://x.test/chat/completions"),
                    response=httpx.Response(429, request=httpx.Request("POST", "u")),
                )

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr("app.llm.openrouter.time.sleep", slept.append)
    assert _provider()._post({}) == {"ok": True}
    assert len(slept) == 2


def _flaky_client(failures, exc_factory):
    """Fake httpx.Client raising `exc_factory()` `failures` times, then ok."""

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    state = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            if state["n"] < failures:
                state["n"] += 1
                raise exc_factory()
            return FakeResp()

    return FakeClient, state


def test_transport_error_retried_then_succeeds(monkeypatch):
    # Regression: RemoteProtocolError (server disconnect, task 110) must be
    # retried like any transient transport failure, never leaked raw.
    def boom():
        return httpx.RemoteProtocolError(
            "Server disconnected",
            request=httpx.Request("POST", "https://x.test/chat/completions"),
        )

    FakeClient, _ = _flaky_client(2, boom)
    slept = []
    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr("app.llm.openrouter.time.sleep", slept.append)
    assert _provider()._post({}) == {"ok": True}
    assert len(slept) == 2


def test_transport_error_exhausted_raises_provider_error(monkeypatch):
    def boom():
        return httpx.RemoteProtocolError(
            "Server disconnected",
            request=httpx.Request("POST", "https://x.test/chat/completions"),
        )

    FakeClient, _ = _flaky_client(99, boom)
    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr("app.llm.openrouter.time.sleep", lambda s: None)
    with pytest.raises(ProviderError, match="unreachable|failed"):
        _provider()._post({})
