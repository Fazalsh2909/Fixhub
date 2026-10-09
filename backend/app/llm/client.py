"""Direct LLM client for OpenAI-compatible gateways (bynara/xkiro switchable).

Endpoint:  POST {base_url}/chat/completions (see app.config.active_llm)
No agent framework. This client sends messages + tool schemas and returns
the raw assistant message (content + tool_calls). The agent loop decides.

Transient upstream failures (429/5xx, connect errors, HTTP-200 error
envelopes) are retried with backoff; auth failures and other 4xx fail fast.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx

from app.config import settings


class LLMBlockedError(RuntimeError):
    """No API key / unreachable gateway -> task BLOCKED (not FAILED)."""


class LLMError(RuntimeError):
    """Gateway returned an error or bad payload -> task FAILED."""


@dataclass
class ProviderUsage:
    """Token counts from one chat call. Nulls when the provider omits them."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


# Phase 3: thread-local runtime credential (BYOK). The worker sets this per
# task from the task owner's decrypted credential; the agent loop and all
# callers keep using chat_completion() unchanged and never see the key.
_runtime = threading.local()
_usage_collectors: list[list[dict]] = []
_usage_lock = threading.Lock()


def _runtime_credential() -> tuple[str, str, str] | None:
    cred = getattr(_runtime, "credential", None)
    if cred and len(cred) == 3 and cred[1]:
        return cred[0], cred[1], cred[2]
    return None


@contextmanager
def use_runtime_credential(base_url: str, api_key: str, model: str):
    """Scope a user credential to the current thread (task execution only)."""
    prev = getattr(_runtime, "credential", None)
    _runtime.credential = (base_url, api_key, model)
    try:
        yield
    finally:
        _runtime.credential = prev


def start_usage_collection() -> list[dict]:
    """Begin collecting per-call {latency_ms, usage} records on this thread."""
    buf: list[dict] = []
    with _usage_lock:
        _usage_collectors.append(buf)
    return buf


def stop_usage_collection(buf: list[dict]) -> list[dict]:
    with _usage_lock:
        if buf in _usage_collectors:
            _usage_collectors.remove(buf)
    return buf


def _record_usage(latency_ms: int, usage: ProviderUsage) -> None:
    with _usage_lock:
        targets = list(_usage_collectors)
    for buf in targets:
        buf.append(
            {
                "latency_ms": latency_ms,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            }
        )


def _sanitize(text: str) -> str:
    """Defense-in-depth: strip any active credential from error text."""
    out = text or ""
    for source in (_runtime_credential(),):
        if source and source[1] and len(source[1]) >= 8 and source[1] in out:
            out = out.replace(source[1], "[REDACTED]")
    try:
        _, env_key, _ = _active_env()
    except Exception:
        env_key = ""
    if env_key and len(env_key) >= 8 and env_key in out:
        out = out.replace(env_key, "[REDACTED]")
    return out


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class AssistantMessage:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def _active_env() -> tuple[str, str, str]:
    """Development/test path: server environment credentials.

    Preserved ONLY for ownerless dev/test/benchmark tasks. Production user
    traffic must use use_runtime_credential() (BYOK); see tasks/service.py.
    """
    from app.config import active_llm

    base_url, api_key, model = active_llm()
    if not api_key:
        raise LLMBlockedError(
            "no LLM key configured (set BYNARA_API_KEY / XKIRO_API_KEY or LLM_API_KEY)"
        )
    return base_url, api_key, model


def _active() -> tuple[str, str, str]:
    runtime = _runtime_credential()
    if runtime:
        base_url, api_key, model = runtime
        if not api_key:
            raise LLMBlockedError("no LLM credential available for this task")
        return base_url, api_key, model or "default"
    return _active_env()


def _headers() -> dict:
    _, api_key, _ = _active()
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _extract_usage(data: dict) -> ProviderUsage:
    """Provider usage envelope -> nullable token counts. Never fabricates."""
    use = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(use, dict):
        return ProviderUsage()

    def _int(key: str) -> int | None:
        try:
            v = use.get(key)
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return ProviderUsage(
        input_tokens=_int("prompt_tokens"),
        output_tokens=_int("completion_tokens"),
        total_tokens=_int("total_tokens"),
    )


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
    usage = ProviderUsage()
    started = time.monotonic()
    for attempt in range(attempts):
        try:
            resp = httpx.post(
                url, headers=_headers(), json=payload, timeout=settings.LLM_TIMEOUT_S
            )
        except httpx.TransportError as exc:
            # ConnectError, TimeoutException, RemoteProtocolError (truncated
            # bodies / peer disconnects), ReadError, PoolTimeout, ... — all
            # transient at the transport layer and worth retrying.
            last_error = LLMBlockedError(_sanitize(f"LLM gateway unreachable: {exc}"))
            if attempt + 1 < attempts:
                _sleep(attempt, base)
            continue
        if resp.status_code in (401, 403):
            raise LLMBlockedError(_sanitize(f"LLM auth rejected ({resp.status_code})"))
        if resp.status_code == 429 or resp.status_code >= 500:
            last_error = LLMError(
                _sanitize(f"LLM gateway error {resp.status_code}: {resp.text[:300]}")
            )
            if attempt + 1 < attempts:
                _sleep(attempt, base)
            continue
        if resp.status_code >= 400:
            raise LLMError(
                _sanitize(f"LLM gateway error {resp.status_code}: {resp.text[:500]}")
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(
                _sanitize(f"LLM bad response payload: {resp.text[:500]}")
            ) from exc
        # Some routers answer HTTP 200 with an error envelope on upstream blips.
        if isinstance(data, dict) and "choices" not in data and "error" in data:
            last_error = LLMError(
                _sanitize(f"LLM upstream error: {str(data.get('error'))[:300]}")
            )
            if attempt + 1 < attempts:
                _sleep(attempt, base)
            continue
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(
                _sanitize(f"LLM bad response payload: {resp.text[:500]}")
            ) from exc
        usage = _extract_usage(data) if isinstance(data, dict) else ProviderUsage()
        break
    else:
        raise last_error or LLMError("LLM gateway failed after retries")

    _record_usage(int((time.monotonic() - started) * 1000), usage)
    out = AssistantMessage(content=msg.get("content") or "")
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except ValueError:
            args = {"_raw": raw_args}
        out.tool_calls.append(
            ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args)
        )
    return out
