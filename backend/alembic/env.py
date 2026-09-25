"""Alembic environment. Database URL comes from the app settings so the
migration always targets the same DB the backend uses (sqlite dev,
Postgres prod). Fresh installs: Base.metadata.create_all() already creates
the full schema — migrations are guarded to skip existing columns."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from alembic import context  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from app.config import settings  # noqa: E402

connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)

config = context.config
engine = create_engine(settings.database_url, connect_args=connect_args)

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
