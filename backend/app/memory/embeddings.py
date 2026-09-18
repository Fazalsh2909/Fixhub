"""Embeddings-ready retrieval helpers.

Prod path: pgvector + hosted embeddings (OpenAI-compatible /embeddings).
Dev/test path: deterministic HashEmbedding (no network, no key).

Why this exists (YC interview answer):
- keyword-overlap is cheap and explainable but fails on paraphrase
  ("login redirect loop" vs "auth navigation cycle").
- vectors fix paraphrase but need infra + eval. This module makes the
  swap pluggable: `retrieve(..., embed_fn=...)` fuses both via a
  weighted hybrid, so we get semantic recall without a hard dependency.

pgvector migration (when ready):
  ALTER TABLE memories ADD COLUMN embedding vector(1536);
  CREATE INDEX ON memories USING ivfflat (embedding vector_cosine_ops);
  Backfill via OpenAICompatibleEmbedding.embed() in a worker, then set
  alpha < 0.6 to favor semantic. No API change — embed_fn just returns
  real vectors.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOP = {
    "the",
    "and",
    "for",
    "with",
    "use",
    "when",
    "you",
    "your",
    "this",
    "that",
    "from",
    "what",
    "where",
    "which",
    "how",
}


def tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower())) - _STOP


def cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedding(EmbeddingProvider):
    """Deterministic offline embedding: hashed char-trigrams, L2-normalized.

    Not a real semantic model — but captures paraphrase better than exact
    keyword overlap (e.g. 'login' ~ 'log-in') and lets the hybrid path be
    tested without keys or network.
    """

    def __init__(self, dim: int = 128) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        low = (text or "").lower()
        # char trigrams + word tokens both hashed in
        grams = [low[i : i + 3] for i in range(max(0, len(low) - 2))]
        for g in grams + sorted(tokens(text)):
            h = int(hashlib.sha256(g.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v))
        if n > 0:
            v = [x / n for x in v]
        return v

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class OpenAICompatibleEmbedding(EmbeddingProvider):
    """Hosted embeddings via OpenAI-compatible /embeddings endpoint."""

    def __init__(
        self, base_url: str, api_key: str, model: str = "text-embedding-3-small"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.model, "input": texts},
            )
            r.raise_for_status()
            data = r.json()
            return [row["embedding"] for row in data["data"]]


def hybrid_score(keyword_norm: float, semantic: float, alpha: float = 0.6) -> float:
    """Weighted fusion. alpha favors keywords (explainable default)."""
    return alpha * keyword_norm + (1.0 - alpha) * semantic
