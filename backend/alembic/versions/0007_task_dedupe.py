"""0007 task dedupe: one task per external CI event.

Adds `tasks.dedupe_key` plus a unique index. CI tasks use
`ci:<owner/repo>:<head_sha>`, so a failing commit creates exactly one task no
matter how many workflows/jobs report it (or how many deliveries race).
Existing rows stay NULL — the unique index permits any number of NULLs on
both SQLite and PostgreSQL, so no backfill is required.

Revision ID: 0007_task_dedupe
Revises: 0006_drop_memories
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_task_dedupe"
down_revision = "0006_drop_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("dedupe_key", sa.String(512), nullable=True))
    op.create_index("ix_tasks_dedupe_key", "tasks", ["dedupe_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tasks_dedupe_key", table_name="tasks")
    op.drop_column("tasks", "dedupe_key")
