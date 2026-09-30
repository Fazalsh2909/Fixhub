"""SQLAlchemy engine + session factory. SQLite dev, Postgres prod."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _connect_args(url: str) -> dict:
    if url.startswith("sqlite"):
        # timeout: backend API + worker share one SQLite file via a volume;
        # wait on locks instead of failing fast.
        return {"check_same_thread": False, "timeout": 30}
    return {}


engine = create_engine(settings.DATABASE_URL, connect_args=_connect_args(settings.DATABASE_URL))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Additive columns that may be missing on databases created before the models
# gained them (create_all never alters existing tables).
_SCHEMA_PATCHES = {
    "tasks": [
        ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("ci_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_ci_failure", "TEXT NOT NULL DEFAULT ''"),
    ],
}


def ensure_columns() -> None:
    """Additive, idempotent schema upgrade for existing databases."""
    from sqlalchemy import text as _text

    for table, columns in _SCHEMA_PATCHES.items():
        if engine.dialect.name == "sqlite":
            with engine.begin() as conn:
                existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
                for name, ddl in columns:
                    if name not in existing:
                        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        else:
            with engine.begin() as conn:
                for name, ddl in columns:
                    conn.execute(_text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
