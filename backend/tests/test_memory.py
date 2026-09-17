"""Memory freshness + retrieval tests."""

import uuid

from app.db import SessionLocal, init_db
from app.memory.store import mark_stale_for_files, remember, retrieve
from app.models import Repository


def _db():
    init_db()
    return SessionLocal()


def test_remember_and_retrieve_selective():
    db = _db()
    repo = Repository(full_name=f"test/mem-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    remember(
        db,
        repo.id,
        "repository",
        "Authentication uses JWT middleware in src/auth/middleware.ts",
        source_path="src/auth/middleware.ts",
    )
    remember(
        db,
        repo.id,
        "repository",
        "Payments use Stripe in src/pay/x.ts",
        source_path="src/pay/x.ts",
    )
    db.commit()
    hits = retrieve(db, repo.id, "authentication JWT middleware")
    assert hits and "JWT" in hits[0].fact
    db.close()


def test_stale_on_file_change():
    db = _db()
    repo = Repository(full_name=f"test/stale-{uuid.uuid4().hex[:8]}")
    db.add(repo)
    db.commit()
    db.refresh(repo)
    m = remember(
        db, repo.id, "codebase", "auth helper", source_path="src/auth/middleware.ts"
    )
    db.commit()
    n = mark_stale_for_files(db, repo.id, ["src/auth/middleware.ts"])
    db.commit()
    assert n == 1
    db.refresh(m)
    assert m.status == "STALE"
    db.close()
