"""Phase 7/9: publish audit columns.

Adds task_events.prev_state/reason (explicit transition audit) and
pull_requests.commit_sha (verified remote SHA). Guarded so fresh databases
(create_all already has the columns) skip, and legacy dev DBs migrate
without data loss.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, inspect

revision = "0003_publish_audit_columns"
down_revision = "0002_verification_status"
branch_labels = None
depends_on = None


def _add_column_if_missing(table: str, column, type_) -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns(table)}
    if column not in cols:
        op.add_column(table, Column(column, type_))


def upgrade() -> None:
    import sqlalchemy as sa

    _add_column_if_missing("task_events", "prev_state", sa.String(32))
    _add_column_if_missing("task_events", "reason", sa.String(1024))
    _add_column_if_missing("pull_requests", "commit_sha", sa.String(64))


def downgrade() -> None:
    with op.batch_alter_table("task_events") as batch:
        for col in ("reason", "prev_state"):
            try:
                batch.drop_column(col)
            except Exception:
                pass
    try:
        with op.batch_alter_table("pull_requests") as batch:
            batch.drop_column("commit_sha")
    except Exception:
        pass
