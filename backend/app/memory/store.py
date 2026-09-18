"""Engineering memory store: 7 types, provenance, freshness.

Retrieval is hybrid: rarity-weighted keyword overlap (IDF-like, explainable)
fused with optional embedding cosine. Pass embed_fn=None (default) for pure
keyword mode; pass HashEmbedding().embed or a hosted provider for semantic.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from ..models import Memory
from .embeddings import cosine_sim, hybrid_score, tokens

TYPES = {
    "repository",
    "task",
    "failure",
    "decision",
    "known_problem",
    "verification",
    "codebase",
}
STATUSES = {"ACTIVE", "STALE", "INVALIDATED", "VERIFIED"}


def remember(
    db: Session,
    repo_id: int,
    type: str,
    fact: str,
    source_path: str = "",
    commit_sha: str = "",
    confidence: float = 0.8,
) -> Memory:
    assert type in TYPES, f"unknown memory type {type}"
    m = Memory(
        repo_id=repo_id,
        type=type,
        fact=fact,
        source_path=source_path,
        commit_sha=commit_sha,
        confidence=confidence,
        status="ACTIVE",
    )
    db.add(m)
    db.flush()
    return m


def mark_stale_for_files(db: Session, repo_id: int, changed_files: list[str]) -> int:
    """Mark memories whose source_path overlaps changed files as STALE. Returns count."""
    changed = set(changed_files)
    n = 0
    for m in db.query(Memory).filter_by(repo_id=repo_id, status="ACTIVE").all():
        if m.source_path and (
            m.source_path in changed or any(m.source_path.endswith(c) for c in changed)
        ):
            m.status = "STALE"
            n += 1
    return n


def snapshot_task(
    db: Session,
    repo_id: int,
    task_id: int,
    title: str,
    state: str,
    note: str = "",
) -> Memory:
    """Persist task progress as a `task`-type memory so it survives restarts.

    One row per snapshot (cheap, explainable). Recall surfaces these via
    `retrieve()` alongside user `remember` facts.
    """
    fact = f"Task #{task_id} ({title[:200]}) is {state}."
    if note:
        fact += f" {note[:300]}"
    return remember(db, repo_id, "task", fact)


def retrieve(
    db: Session,
    repo_id: int,
    query: str,
    limit: int = 8,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    alpha: float = 0.6,
) -> list[Memory]:
    """Hybrid retrieval over ACTIVE+VERIFIED (STALE penalized, INVALIDATED skipped).

    - keyword: rarity-weighted overlap (rare tokens like 'jwt' beat 'api').
    - semantic (optional): cosine between query embedding and fact embedding,
      fused via hybrid_score(). Fail-open: embedding errors fall back to keyword.
    """
    terms = tokens(query)
    rows = db.query(Memory).filter_by(repo_id=repo_id).all()
    rows = [m for m in rows if m.status != "INVALIDATED"]
    if not rows:
        return []

    # IDF-like rarity: tokens in few memories count more.
    doc_freq: dict[str, int] = {}
    hays: list[set[str]] = []
    for m in rows:
        hay = tokens(m.fact)
        hays.append(hay)
        for t in hay:
            doc_freq[t] = doc_freq.get(t, 0) + 1

    kw_raw: list[float] = []
    for hay in hays:
        overlap = terms & hay
        kw_raw.append(sum(1.0 / doc_freq[t] for t in overlap) if overlap else 0.0)
    kw_max = max(kw_raw) if kw_raw else 0.0

    sem_scores: list[float] = [0.0] * len(rows)
    if embed_fn is not None and terms:
        try:
            vecs = embed_fn([query] + [m.fact for m in rows])
            qv, fvs = vecs[0], vecs[1:]
            sem_scores = [cosine_sim(qv, fv) for fv in fvs]
        except Exception:
            sem_scores = [0.0] * len(rows)

    scored: list[tuple[float, Memory]] = []
    for m, hay, raw, sem in zip(rows, hays, kw_raw, sem_scores):
        kw_norm = (raw / kw_max) if kw_max > 0 else 0.0
        if embed_fn is None:
            score = raw
        else:
            score = hybrid_score(kw_norm, sem, alpha=alpha)
        score += 2.0 if m.status == "VERIFIED" else 0.0
        score -= 3.0 if m.status == "STALE" else 0.0
        # Minimum evidence in pure-keyword mode: 1+ overlapping token,
        # or empty query (list recent). Hybrid mode keeps semantic hits.
        if embed_fn is None:
            if score > 0 or not terms:
                scored.append((score, m))
        else:
            if score > 0.01 or not terms:
                scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored[:limit]]
