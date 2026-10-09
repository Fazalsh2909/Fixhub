"""SQLAlchemy engine + session factory. SQLite dev, Postgres prod."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Phase 4: single aware-UTC clock for all writes/comparisons.

    PostgreSQL returns tz-aware datetimes for DateTime(timezone=True); naive
    datetime.utcnow() values raise TypeError on compare. SQLite stores ISO
    strings where aware values still order correctly.
    """
    return datetime.now(timezone.utc)


def _connect_args(url: str) -> dict:
    if url.startswith("sqlite"):
        # timeout: backend API + worker share one SQLite file via a volume;
        # wait on locks instead of failing fast.
        return {"check_same_thread": False, "timeout": 30}
    if url.startswith("postgres"):
        args: dict = {"connect_timeout": settings.DB_POOL_TIMEOUT}
        # Phase 4.5: bound runaway app queries (migrations/admin bypass this
        # via their own connections). 0 = off.
        try:
            ms = int(settings.POSTGRES_STATEMENT_TIMEOUT_MS)
        except (TypeError, ValueError):
            ms = 0
        if ms > 0:
            args["options"] = f"-c statement_timeout={ms}ms"
        return args
    return {}


def _pool_kwargs(url: str) -> dict:
    # Phase 4: bounded production pool for PostgreSQL (pre-ping + recycle so
    # idle disconnects behind API/worker replicas never wedge requests).
    # SQLite keeps a lightweight SingletonThreadPool-friendly default.
    if url.startswith("postgres"):
        from sqlalchemy.pool import QueuePool

        return {
            "poolclass": QueuePool,
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_MAX_OVERFLOW,
            "pool_timeout": settings.DB_POOL_TIMEOUT,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "pool_pre_ping": True,
        }
    return {"pool_pre_ping": True}


engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args(settings.DATABASE_URL),
    **_pool_kwargs(settings.DATABASE_URL),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Additive columns that may be missing on databases created before the models
# gained them (create_all never alters existing tables).
# Phase 4: production uses Alembic (`alembic upgrade head`); these patches
# remain for pre-existing SQLite dev databases only.
_SCHEMA_PATCHES = {
    "tasks": [
        ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("ci_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_ci_failure", "TEXT NOT NULL DEFAULT ''"),
        ("owner_id", "INTEGER REFERENCES users(id)"),
        ("claimed_by", "VARCHAR(64) NOT NULL DEFAULT ''"),
        ("claimed_at", "TIMESTAMP"),
        ("lease_expires_at", "TIMESTAMP"),
        ("queue_job_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
    ],
    "repositories": [
        ("owner_id", "INTEGER REFERENCES users(id)"),
    ],
    "memories": [
        ("owner_id", "INTEGER REFERENCES users(id)"),
    ],
}


def ensure_columns() -> None:
    """Additive, idempotent schema upgrade for existing databases.

    Gate 0: production schema authority is Alembic ONLY. In prod this is a
    no-op so a half-migrated database can never be silently patched into a
    half-working shape outside migrations. Dev/test SQLite keeps the patches
    for pre-existing local databases.
    """
    try:
        from app.config import settings as _settings

        if str(getattr(_settings, "ENV", "dev") or "dev").strip().lower() == "prod":
            return
    except Exception:
        pass
    from sqlalchemy import text as _text

    for table, columns in _SCHEMA_PATCHES.items():
        if engine.dialect.name == "sqlite":
            with engine.begin() as conn:
                existing = {
                    row[1]
                    for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
                }
                for name, ddl in columns:
                    if name not in existing:
                        conn.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
                        )
        else:
            with engine.begin() as conn:
                for name, ddl in columns:
                    conn.execute(
                        _text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}"
                        )
                    )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
