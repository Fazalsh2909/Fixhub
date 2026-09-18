"""LLM provider abstraction. Agent depends on this, never on a vendor SDK."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, messages: list[dict], **kwargs: object) -> LLMResponse: ...

    @abstractmethod
    def tool_call(
        self, messages: list[dict], tools: list[ToolSpec], **kwargs: object
    ) -> LLMResponse: ...

    def generate_stream(self, messages: list[dict], **kwargs: object):  # type: ignore[no-untyped-def]
        """SSE-ready streaming. Default: fall back to single generate().

        Prod frontends consume this via GET /api/chat/stream (chunked).
        Providers override with real token streaming when available.
        """
        yield self.generate(messages, **kwargs)

    def structured_output(
        self, messages: list[dict], schema: dict, **kwargs: object
    ) -> dict:
        resp = self.generate(messages, **kwargs)
        import json

        try:
            return json.loads(resp.text)
        except Exception:
            return {"raw": resp.text}
