"""Attribution architecture on verification_runs.

Adds phase (BASELINE|AFTER), signature, attribution, duration_ms columns.
Backfills existing rows as AFTER-phase evidence: PASS rows carry NONE,
FAIL rows carry UNKNOWN (legacy path routes them exactly as before —
DEBUGGING on required FAIL). Guarded so fresh databases (create_all
already has the columns) skip.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, inspect

revision = "0004_verification_attribution"
down_revision = "0003_publish_audit_columns"
branch_labels = None
depends_on = None


def _add_column_if_missing(table: str, column, type_) -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns(table)}
    if column not in cols:
        op.add_column(table, Column(column, type_))


def upgrade() -> None:
    import sqlalchemy as sa

    _add_column_if_missing("verification_runs", "phase", sa.String(16))
    _add_column_if_missing("verification_runs", "signature", sa.Text())
    _add_column_if_missing("verification_runs", "attribution", sa.String(32))
    _add_column_if_missing("verification_runs", "duration_ms", sa.Integer())
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE verification_runs SET phase = 'AFTER' "
            "WHERE phase IS NULL OR phase = ''"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE verification_runs SET attribution = "
            "CASE WHEN status = 'PASS' THEN 'NONE' ELSE 'UNKNOWN' END "
            "WHERE attribution IS NULL OR attribution = ''"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE verification_runs SET duration_ms = 0 WHERE duration_ms IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("verification_runs") as batch:
        for col in ("duration_ms", "attribution", "signature", "phase"):
            try:
                batch.drop_column(col)
            except Exception:
                pass
