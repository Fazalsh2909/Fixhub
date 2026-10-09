"""0003 phase3 byok: llm_credentials (+ unique user/provider) and llm_usage.

Revision ID: 0003_phase3_byok
Revises: 0002_phase2_auth
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_phase3_byok"
down_revision = "0002_phase2_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_credentials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("encrypted_secret", sa.Text(), nullable=False, server_default=""),
        sa.Column("key_hint", sa.String(16), nullable=False, server_default=""),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="configured"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_llm_credential_user_provider"),
    )
    op.create_index("ix_llm_credentials_user_id", "llm_credentials", ["user_id"])
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default=""),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("success", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.String(64), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_usage_user_id", "llm_usage", ["user_id"])
    op.create_index("ix_llm_usage_task_id", "llm_usage", ["task_id"])


def downgrade() -> None:
    op.drop_table("llm_usage")
    op.drop_table("llm_credentials")
