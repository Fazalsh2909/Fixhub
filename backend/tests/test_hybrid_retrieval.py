"""Hybrid retrieval: paraphrase recall + fail-open without keys."""

import uuid

from app.db import SessionLocal, init_db
from app.memory.embeddings import HashEmbedding, cosine_sim, hybrid_score
from app.memory.store import remember, retrieve
from app.models import Repository


def _db():
    init_db()
    return SessionLocal()


def test_hybrid_paraphrase_beats_keyword_only():
    db = _db()
    repo = Repository(full_name=f"test/hybrid-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    remember(db, repo.id, "repository", "Authentication uses JWT middleware for login", source_path="auth.py")
    remember(db, repo.id, "repository", "Payments use Stripe for billing", source_path="pay.py")
    db.commit()
    # paraphrase: no exact token "authentication", but semantic overlap via trigrams
    hits = retrieve(db, repo.id, "sign-in token validation", embed_fn=HashEmbedding(dim=64).embed)
    assert hits, "hybrid should return something on paraphrase"
    db.close()


def test_keyword_path_unchanged_without_embed_fn():
    db = _db()
    repo = Repository(full_name=f"test/kw-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    remember(db, repo.id, "repository", "Authentication uses JWT middleware", source_path="a.py")
    remember(db, repo.id, "repository", "Payments use Stripe", source_path="b.py")
    db.commit()
    hits = retrieve(db, repo.id, "authentication JWT middleware")
    assert hits and "JWT" in hits[0].fact
    db.close()


def test_embed_failure_falls_back_to_keyword():
    db = _db()
    repo = Repository(full_name=f"test/fallback-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    remember(db, repo.id, "repository", "JWT auth middleware", source_path="a.py")
    db.commit()

    def boom(_texts):
        raise RuntimeError("no network")

    hits = retrieve(db, repo.id, "JWT", embed_fn=boom)
    assert hits and "JWT" in hits[0].fact
    db.close()


def test_cosine_and_hybrid_math():
    assert cosine_sim([1, 0], [1, 0]) == 1.0
    assert cosine_sim([1, 0], [0, 1]) == 0.0
    assert hybrid_score(1.0, 0.0, alpha=0.6) == 0.6
