"""0009 github install ownership: verified GitHub identity + connections.

Install-ownership fix, additive only. Existing rows keep working:

- `github_connections.github_user_id` NULL on every pre-migration row =
  legacy/unverified: user-facing GitHub endpoints refuse them until the
  owner completes the GitHub App user authorization once (reconcile then
  flips rows GitHub itself confirms). Webhooks and workers (server-side,
  no browser input) keep resolving them — no silent assignment happens.
- `repositories.github_user_id` / `verified_at` are NULL on legacy rows and
  behave exactly the same way.
- `github_account_type` defaults to "" on legacy rows (rendered as "").
- New `github_authorizations` table starts empty; each FixHub user gets a
  row on their first GitHub App authorization (encrypted user access token
  via the existing Fernet master key).

Revision ID: 0009_github_install_ownership
Revises: 0008_reliability
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_github_install_ownership"
down_revision = "0008_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Verified GitHub identity per FixHub user (tokens encrypted) ---
    op.create_table(
        "github_authorizations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=True),
        sa.Column("github_login", sa.String(255), nullable=False, server_default=""),
        sa.Column("encrypted_user_token", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "encrypted_refresh_token", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_github_authorization_user"),
    )
    op.create_index(
        "ix_github_authorizations_user_id",
        "github_authorizations",
        ["user_id"],
    )

    # --- Verification columns on connections + repositories ---
    op.add_column(
        "github_connections", sa.Column("github_user_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "github_connections",
        sa.Column("github_account_type", sa.String(32), nullable=False, server_default=""),
    )
    op.add_column(
        "github_connections", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "repositories", sa.Column("github_user_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "repositories", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("repositories", "verified_at")
    op.drop_column("repositories", "github_user_id")
    op.drop_column("github_connections", "verified_at")
    op.drop_column("github_connections", "github_account_type")
    op.drop_column("github_connections", "github_user_id")
    op.drop_index("ix_github_authorizations_user_id", table_name="github_authorizations")
    op.drop_table("github_authorizations")
