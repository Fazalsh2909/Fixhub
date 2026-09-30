"""Direct LLM client for OpenAI-compatible gateways (bynara/xkiro switchable).

Endpoint:  POST {base_url}/chat/completions (see app.config.active_llm)
No agent framework. This client sends messages + tool schemas and returns
the raw assistant message (content + tool_calls). The agent loop decides.

Transient upstream failures (429/5xx, connect errors, HTTP-200 error
envelopes) are retried with backoff; auth failures and other 4xx fail fast.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from app.config import settings


class LLMBlockedError(RuntimeError):
    """No API key / unreachable gateway -> task BLOCKED (not FAILED)."""


class LLMError(RuntimeError):
    """Gateway returned an error or bad payload -> task FAILED."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class AssistantMessage:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def _active() -> tuple[str, str, str]:
    from app.config import active_llm

    base_url, api_key, model = active_llm()
    if not api_key:
        raise LLMBlockedError("no LLM key configured (set BYNARA_API_KEY / XKIRO_API_KEY or LLM_API_KEY)")
    return base_url, api_key, model


def _headers() -> dict:
    _, api_key, _ = _active()
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _sleep(attempt: int, base: float) -> None:
    """Backoff between retries. Skipped when base is 0 (tests)."""
    if base > 0:
        time.sleep(base * (2**attempt))


def chat_completion(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.2,
) -> AssistantMessage:
    """One non-streaming chat call. Raises LLMBlockedError or LLMError."""
    base_url, _, model = _active()
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    attempts = max(1, settings.LLM_RETRY_ATTEMPTS)
    base = max(0.0, settings.LLM_RETRY_BASE_S)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = httpx.post(url, headers=_headers(), json=payload, timeout=settings.LLM_TIMEOUT_S)
        except httpx.TransportError as exc:
            # ConnectError, TimeoutException, RemoteProtocolError (truncated
            # bodies / peer disconnects), ReadError, PoolTimeout, ... — all
            # transient at the transport layer and worth retrying.
            last_error = LLMBlockedError(f"LLM gateway unreachable: {exc}")
            if attempt + 1 < attempts:
                _sleep(attempt, base)
            continue
        if resp.status_code in (401, 403):
            raise LLMBlockedError(f"LLM auth rejected ({resp.status_code})")
        if resp.status_code == 429 or resp.status_code >= 500:
            last_error = LLMError(f"LLM gateway error {resp.status_code}: {resp.text[:300]}")
            if attempt + 1 < attempts:
                _sleep(attempt, base)
            continue
        if resp.status_code >= 400:
            raise LLMError(f"LLM gateway error {resp.status_code}: {resp.text[:500]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"LLM bad response payload: {resp.text[:500]}") from exc
        # Some routers answer HTTP 200 with an error envelope on upstream blips.
        if isinstance(data, dict) and "choices" not in data and "error" in data:
            last_error = LLMError(f"LLM upstream error: {str(data.get('error'))[:300]}")
            if attempt + 1 < attempts:
                _sleep(attempt, base)
            continue
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"LLM bad response payload: {resp.text[:500]}") from exc
        break
    else:
        raise last_error or LLMError("LLM gateway failed after retries")

    out = AssistantMessage(content=msg.get("content") or "")
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except ValueError:
            args = {"_raw": raw_args}
        out.tool_calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
    return out
