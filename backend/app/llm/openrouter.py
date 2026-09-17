"""OpenAI-compatible provider. Works with tokenrouter.com, OpenAI, any /chat/completions.

Credentials: passed in, never logged, never forwarded to sandbox.
Usage + latency are recorded in app.metrics for /metrics display.
"""

from __future__ import annotations

import time

import httpx

from .base import LLMProvider, LLMResponse, ToolSpec


class ProviderError(RuntimeError):
    """Typed wrapper for provider failures (safe to record: no credentials)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OpenRouterProvider(LLMProvider):
    def __init__(
        self, base_url: str, api_key: str, model: str, timeout_s: int = 60
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key  # never log this
        self.model = model
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, payload: dict) -> dict:
        import random

        attempts = 5
        last: ProviderError | None = None
        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    r = client.post(
                        f"{self.base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
                    r.raise_for_status()
                    return r.json()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                # Retry set per gateway contracts (experiential docs: backoff on
                # 429/502/503/504; never blind-retry 400/401/403).
                if status in (429, 500, 502, 503, 504, 529) and attempt < attempts - 1:
                    wait = self._backoff_s(e.response, attempt)
                    last = ProviderError(
                        f"provider {status}, retrying in {wait:.0f}s "
                        f"(attempt {attempt + 1}/{attempts})",
                        status,
                    )
                    time.sleep(wait)
                    continue
                raise ProviderError(
                    f"provider {status}: {e.response.text[:300]} "
                    f"(after {attempt + 1} attempts — wait a minute and re-run)",
                    status,
                ) from e
            except (httpx.TransportError, httpx.TimeoutException) as e:
                # TransportError covers ConnectError, Read/WriteError,
                # PoolTimeout and RemoteProtocolError (server disconnect) —
                # all transient, all retried, never leaked raw to the agent loop.
                if attempt >= attempts - 1:
                    break
                wait = min(60.0, 5.0 * 2**attempt) + random.uniform(0, 1)
                last = ProviderError(f"provider unreachable: {type(e).__name__}")
                time.sleep(wait)
        raise last or ProviderError("provider request failed")

    @staticmethod
    def _backoff_s(response: httpx.Response, attempt: int) -> float:
        """Honor server Retry-After on 429; else exponential 5→60s + jitter."""
        import random

        try:
            retry_after = float(response.headers.get("retry-after", ""))
            if 0 < retry_after <= 120:
                return retry_after + random.uniform(0, 1)
        except (TypeError, ValueError):
            pass
        return min(60.0, 5.0 * 2**attempt) + random.uniform(0, 1)

    @staticmethod
    def _text_of(msg: dict) -> str:
        # Some reasoning models return content:null with the answer in reasoning_content.
        return msg.get("content") or msg.get("reasoning_content") or ""

    def generate(self, messages: list[dict], **kwargs: object) -> LLMResponse:
        import time

        from ..metrics import record_llm_call

        start = time.monotonic()
        data = self._post({"model": self.model, "messages": messages})
        latency = int((time.monotonic() - start) * 1000)
        text = self._text_of(data["choices"][0]["message"])
        usage = data.get("usage", {})
        record_llm_call(self.model, usage, latency)
        return LLMResponse(text=text, usage=usage)

    def tool_call(
        self, messages: list[dict], tools: list[ToolSpec], **kwargs: object
    ) -> LLMResponse:
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        data = self._post(
            {"model": self.model, "messages": messages, "tools": openai_tools}
        )
        msg = data["choices"][0]["message"]
        calls = [
            {
                "name": c["function"]["name"],
                "arguments": c["function"].get("arguments", "{}"),
            }
            for c in (msg.get("tool_calls") or [])
        ]
        usage = data.get("usage", {})
        # tool_call latency ~0 extra (already in _post); record with 0ms and real usage
        try:
            from ..metrics import record_llm_call

            record_llm_call(self.model, usage, 0)
        except Exception:
            pass
        return LLMResponse(
            text=self._text_of(msg),
            tool_calls=calls,
            usage=usage,
        )


# Backwards-compatible alias: any OpenAI-compatible endpoint works.
OpenAICompatibleProvider = OpenRouterProvider


def provider_from_settings() -> OpenRouterProvider:
    from ..config import settings

    base_url, api_key, model = settings.resolved_llm()
    return OpenRouterProvider(base_url=base_url, api_key=api_key, model=model)
