"""Engineering memory store: 7 types, provenance, freshness."""

from __future__ import annotations


from sqlalchemy.orm import Session

from ..models import Memory

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


def retrieve(db: Session, repo_id: int, query: str, limit: int = 8) -> list[Memory]:
    """Selective retrieval: keyword overlap over ACTIVE+VERIFIED (STALE only if nothing else)."""
    terms = {t.lower() for t in query.split() if len(t) > 2}
    scored: list[tuple[int, Memory]] = []
    for m in db.query(Memory).filter_by(repo_id=repo_id).all():
        if m.status in ("INVALIDATED",):
            continue
        hay = m.fact.lower()
        score = (
            sum(1 for t in terms if t in hay)
            + (2 if m.status == "VERIFIED" else 0)
            - (3 if m.status == "STALE" else 0)
        )
        if score > 0 or not terms:
            scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored[:limit]]
