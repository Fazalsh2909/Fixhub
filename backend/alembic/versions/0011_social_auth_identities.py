"""0011 social auth identities: AuthIdentity table + unique constraint.

Revision ID: 0011_social_auth_identities
Revises: 0010_task_run_provenance
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011_social_auth_identities"
down_revision = "0010_task_run_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_identities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("provider_subject", sa.String(255), nullable=False),
        sa.Column("provider_email", sa.String(255)),
        sa.Column("provider_display_name", sa.String(128)),
        sa.Column("provider_avatar_url", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auth_identities_provider_subject", "auth_identities", ["provider", "provider_subject"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_auth_identities_provider_subject", table_name="auth_identities")
    op.drop_table("auth_identities")
