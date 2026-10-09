"""0005 phase4.5 hardening: login_attempts table + users.is_admin.

Revision ID: 0005_phase45_hardening
Revises: 0004_phase4_claims
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_phase45_hardening"
down_revision = "0004_phase4_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_admin", sa.Integer(), nullable=False,
                                     server_default="0"))
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bucket", sa.String(128), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_login_attempts_bucket", "login_attempts", ["bucket"],
                    unique=True)


def downgrade() -> None:
    op.drop_index("ix_login_attempts_bucket", "login_attempts")
    op.drop_table("login_attempts")
    op.drop_column("users", "is_admin")
