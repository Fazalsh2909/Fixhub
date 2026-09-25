"""P0-5: structured verification semantics on verification_runs.

Adds status (PASS|FAIL|SKIPPED|NOT_RUN|ERROR) and required columns.
Backfills existing rows: passed rows become PASS, failed become FAIL;
the `type` gate is optional (required=False), suite/lint required.
Guarded so fresh databases (create_all already has the columns) skip.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, inspect

revision = "0002_verification_status"
down_revision = "0001_task_workspace_columns"
branch_labels = None
depends_on = None


def _add_column_if_missing(table: str, column, type_) -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns(table)}
    if column not in cols:
        op.add_column(table, Column(column, type_))


def upgrade() -> None:
    import sqlalchemy as sa

    _add_column_if_missing("verification_runs", "status", sa.String(16))
    _add_column_if_missing("verification_runs", "required", sa.Boolean())
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE verification_runs SET status = "
            "CASE WHEN passed THEN 'PASS' ELSE 'FAIL' END "
            "WHERE status IS NULL OR status = ''"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE verification_runs SET required = "
            "CASE WHEN check = 'type' THEN 0 ELSE 1 END "
            "WHERE required IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("verification_runs") as batch:
        for col in ("required", "status"):
            try:
                batch.drop_column(col)
            except Exception:
                pass
