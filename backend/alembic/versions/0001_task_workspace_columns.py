"""P0-1: per-task / per-session isolated workspace columns.

Adds tasks.workspace_path, tasks.base_sha, agent_sessions.workspace_path.
Guarded with inspector checks so the migration is safe on fresh databases
(where create_all already created the columns) and on legacy dev DBs.
Existing rows keep empty strings = "not provisioned".
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column
from sqlalchemy import inspect

revision = "0001_task_workspace_columns"
down_revision = None
branch_labels = None
depends_on = None


def _add_column_if_missing(table: str, column, type_) -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns(table)}
    if column not in cols:
        op.add_column(table, Column(column, type_))


def upgrade() -> None:
    import sqlalchemy as sa

    _add_column_if_missing("tasks", "workspace_path", sa.String(1024))
    _add_column_if_missing("tasks", "base_sha", sa.String(64))
    for table in ("agent_sessions",):
        try:
            _add_column_if_missing(table, "workspace_path", sa.String(1024))
        except Exception:
            # Table may not exist on DBs created before sessions shipped.
            pass


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        try:
            batch.drop_column("base_sha")
        except Exception:
            pass
        try:
            batch.drop_column("workspace_path")
        except Exception:
            pass
    try:
        with op.batch_alter_table("agent_sessions") as batch:
            batch.drop_column("workspace_path")
    except Exception:
        pass
