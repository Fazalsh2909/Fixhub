"""0002 phase2 auth: users, sessions, connections + owner columns.

Revision ID: 0002_phase2_auth
Revises: 0001_baseline
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_phase2_auth"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False, server_default=""),
        sa.Column("display_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("avatar_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_token_hash", "user_sessions", ["token_hash"],
                    unique=True)
    op.create_table(
        "github_connections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("installation_id", sa.String(64), nullable=False),
        sa.Column("github_account", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_github_connections_user_id", "github_connections", ["user_id"])
    op.create_index("ix_github_connections_installation_id", "github_connections",
                    ["installation_id"])
    for table in ("repositories", "tasks", "memories"):
        op.add_column(table, sa.Column("owner_id", sa.Integer(), nullable=True))
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])
    # Foreign keys are enforced on PostgreSQL only: SQLite ignores them
    # (the app never enables PRAGMA foreign_keys), so skip the ALTER there.
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        for table in ("repositories", "tasks", "memories"):
            op.create_foreign_key(f"fk_{table}_owner_id_users", table, "users",
                                  ["owner_id"], ["id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        for table in ("memories", "tasks", "repositories"):
            op.drop_constraint(f"fk_{table}_owner_id_users", table, type_="foreignkey")
    for table in ("memories", "tasks", "repositories"):
        op.drop_index(f"ix_{table}_owner_id", table)
        op.drop_column(table, "owner_id")
    op.drop_table("github_connections")
    op.drop_table("user_sessions")
    op.drop_table("users")
