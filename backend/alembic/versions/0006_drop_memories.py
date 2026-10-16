"""0006 drop memories: remove the per-repository engineering memory store.

Agent runs no longer read or write prior-run summaries (stale knowledge was
steering fresh runs and burning prompt tokens on every request). The
`memories` table and its indexes/constraints are dropped; downgrade recreates
the 0001 baseline shape plus the 0002 owner column so a rollback restores the
schema (rows are not recoverable).

Revision ID: 0006_drop_memories
Revises: 0005_phase45_hardening
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_drop_memories"
down_revision = "0005_phase45_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        try:
            op.drop_constraint("fk_memories_owner_id_users", "memories", type_="foreignkey")
        except Exception:
            pass
    try:
        op.drop_index("ix_memories_owner_id", table_name="memories")
    except Exception:
        pass
    op.drop_index("ix_memories_repository", table_name="memories")
    op.drop_table("memories")


def downgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("commit_sha", sa.String(128), nullable=False, server_default=""),
        sa.Column("path", sa.String(512), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_analyzed_rev", sa.String(128), nullable=False, server_default=""),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memories_repository", "memories", ["repository"])
    op.create_index("ix_memories_owner_id", "memories", ["owner_id"])
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_memories_owner_id_users", "memories", "users", ["owner_id"], ["id"]
        )
