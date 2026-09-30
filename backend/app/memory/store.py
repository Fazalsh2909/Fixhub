"""Persistent per-repository engineering memory. First-class, version-aware, bounded.

- save_memory(repo, path, summary, rev): upsert one scope (overview or file/dir)
- search_memory(repo, query): LIKE-based relevance over path+summary (v1; embeddings later)
- get_repository_overview(repo): concise prior knowledge injected into the agent
- update_repository_memory(...): alias for save (post-task delta)

Memory is prior knowledge, NOT truth. The agent prompt tells the LLM to verify.
An `embedding` column can be added later without touching the agent loop.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Memory

OVERVIEW_PATH = "__overview__"
MAX_SUMMARY_CHARS = 4000


def save_memory(db: Session, *, repository: str, path: str, summary: str, rev: str) -> Memory:
    summary = (summary or "")[:MAX_SUMMARY_CHARS]
    row = (
        db.query(Memory)
        .filter(Memory.repository == repository, Memory.path == path)
        .first()
    )
    if row:
        row.summary = summary
        row.commit_sha = rev
        row.last_analyzed_rev = rev
    else:
        row = Memory(
            repository=repository, path=path, summary=summary, commit_sha=rev, last_analyzed_rev=rev
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def search_memory(db: Session, *, repository: str, query: str, limit: int = 8) -> list[Memory]:
    """Simple relevance: all query tokens must appear in path+summary (case-insensitive)."""
    tokens = [t.lower() for t in query.split() if len(t) > 2][:8]
    rows = db.query(Memory).filter(Memory.repository == repository).order_by(Memory.id.asc()).all()
    if not tokens:
        return rows[:limit]
    scored = []
    for r in rows:
        hay = f"{r.path}\n{r.summary}".lower()
        hits = sum(1 for t in tokens if t in hay)
        if hits:
            scored.append((hits, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def get_repository_overview(db: Session, *, repository: str) -> str:
    row = (
        db.query(Memory)
        .filter(Memory.repository == repository, Memory.path == OVERVIEW_PATH)
        .first()
    )
    return row.summary if row else ""


def update_repository_memory(db: Session, *, repository: str, summary: str, rev: str) -> Memory:
    return save_memory(db, repository=repository, path=OVERVIEW_PATH, summary=summary, rev=rev)
