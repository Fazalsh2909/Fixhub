"""DB engine/session. SQLite dev, Postgres prod — same models."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import settings
from .models import Base

connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_columns()


def _ensure_columns() -> None:
    """Add missing columns to pre-existing dev DBs (create_all is a no-op
    on existing tables). Keeps data; mirrors alembic 0001/0002 so tests
    and long-lived dev DBs never drift from models."""
    from sqlalchemy import inspect, text

    wanted: dict[str, list[str]] = {
        "tasks": [
            "ALTER TABLE tasks ADD COLUMN workspace_path VARCHAR(1024)",
            "ALTER TABLE tasks ADD COLUMN base_sha VARCHAR(64)",
        ],
        "agent_sessions": [
            "ALTER TABLE agent_sessions ADD COLUMN workspace_path VARCHAR(1024)",
        ],
        "verification_runs": [
            "ALTER TABLE verification_runs ADD COLUMN status VARCHAR(16)",
            "ALTER TABLE verification_runs ADD COLUMN required BOOLEAN",
            "ALTER TABLE verification_runs ADD COLUMN phase VARCHAR(16)",
            "ALTER TABLE verification_runs ADD COLUMN signature TEXT",
            "ALTER TABLE verification_runs ADD COLUMN attribution VARCHAR(32)",
            "ALTER TABLE verification_runs ADD COLUMN duration_ms INTEGER",
        ],
        "task_events": [
            "ALTER TABLE task_events ADD COLUMN prev_state VARCHAR(32)",
            "ALTER TABLE task_events ADD COLUMN reason VARCHAR(1024)",
        ],
        "pull_requests": [
            "ALTER TABLE pull_requests ADD COLUMN commit_sha VARCHAR(64)",
        ],
    }
    try:
        with engine.begin() as conn:
            existing_tables = set(inspect(conn).get_table_names())
            for table, stmts in wanted.items():
                if table not in existing_tables:
                    continue
                cols = {c["name"] for c in inspect(conn).get_columns(table)}
                for stmt in stmts:
                    col = stmt.split("ADD COLUMN", 1)[1].strip().split()[0]
                    if col not in cols:
                        conn.execute(text(stmt))
                        cols.add(col)
    except Exception:
        pass


def get_db():  # FastAPI dependency
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
