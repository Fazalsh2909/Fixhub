"""Run metrics: tokens, latency, cost. In-memory + persisted per task event.

No secrets here — only counts. Costs are estimates for display.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

_lock = threading.Lock()
_totals = {
    "llm_calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "tool_calls": 0,
    "tasks_run": 0,
    "tasks_verified": 0,
    "est_cost_usd": 0.0,
    "total_latency_ms": 0,
}

# Rough $/1K tokens for display. Free-tier default model = 0.
_PRICE_PER_1K = {
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "z-ai/glm-5.3-free": (0.0, 0.0),
}


def _price(model: str) -> tuple[float, float]:
    for key, val in _PRICE_PER_1K.items():
        if key in model:
            return val
    return (0.001, 0.003)  # conservative fallback for unknown paid models


def record_llm_call(model: str, usage: dict, latency_ms: int) -> float:
    """Record one LLM call. Returns estimated cost USD."""
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    pin, pout = _price(model)
    cost = prompt / 1000 * pin + completion / 1000 * pout
    with _lock:
        _totals["llm_calls"] += 1
        _totals["prompt_tokens"] += prompt
        _totals["completion_tokens"] += completion
        _totals["total_tokens"] += prompt + completion
        _totals["est_cost_usd"] += cost
        _totals["total_latency_ms"] += latency_ms
    return cost


def record_tool_call() -> None:
    with _lock:
        _totals["tool_calls"] += 1


def record_task(verified: bool) -> None:
    with _lock:
        _totals["tasks_run"] += 1
        if verified:
            _totals["tasks_verified"] += 1


def snapshot() -> dict:
    with _lock:
        data = dict(_totals)
    calls = data["llm_calls"] or 1
    data["avg_latency_ms"] = data["total_latency_ms"] // calls
    data["est_cost_usd"] = round(data["est_cost_usd"], 6)
    return data


def timer_ms() -> tuple[float, Callable[[], int]]:
    start = time.monotonic()
    return start, lambda: int((time.monotonic() - start) * 1000)
