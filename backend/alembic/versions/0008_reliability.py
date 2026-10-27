"""0008 reliability: webhook inbox + CI source context + issue dedupe.

Additive only. Existing rows keep working:
- `tasks.ci_*` context columns default to ""/NULL = legacy default-branch
  publishing behavior.
- `tasks.active_issue_key` is NULL for every legacy row (unique index permits
  any number of NULLs on SQLite and PostgreSQL).
- `webhook_deliveries.state` defaults to 'processed' so pre-migration rows are
  treated as already handled (no replay storm on deploy).

Revision ID: 0008_reliability
Revises: 0007_task_dedupe
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008_reliability"
down_revision = "0007_task_dedupe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- CI source context (which revision/branch/PR the failure came from) ---
    op.add_column(
        "tasks", sa.Column("ci_head_branch", sa.String(255), nullable=False, server_default="")
    )
    op.add_column(
        "tasks", sa.Column("ci_base_branch", sa.String(255), nullable=False, server_default="")
    )
    op.add_column("tasks", sa.Column("ci_pr_number", sa.Integer(), nullable=True))
    op.add_column(
        "tasks", sa.Column("ci_is_fork", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "tasks",
        sa.Column("ci_unobservable_ticks", sa.Integer(), nullable=False, server_default="0"),
    )
    # --- issue dedupe (unique while active, NULL once terminal) ---
    op.add_column("tasks", sa.Column("active_issue_key", sa.String(512), nullable=True))
    op.create_index(
        "ix_tasks_active_issue_key", "tasks", ["active_issue_key"], unique=True
    )
    # --- durable webhook inbox ---
    op.add_column(
        "webhook_deliveries",
        sa.Column("state", sa.String(16), nullable=False, server_default="processed"),
    )
    op.add_column(
        "webhook_deliveries",
        sa.Column("event_type", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column("webhook_deliveries", sa.Column("payload", sa.Text(), nullable=True))
    op.add_column(
        "webhook_deliveries",
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "webhook_deliveries",
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "webhook_deliveries",
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_webhook_deliveries_state", "webhook_deliveries", ["state"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_state", table_name="webhook_deliveries")
    op.drop_column("webhook_deliveries", "processed_at")
    op.drop_column("webhook_deliveries", "received_at")
    op.drop_column("webhook_deliveries", "error")
    op.drop_column("webhook_deliveries", "payload")
    op.drop_column("webhook_deliveries", "event_type")
    op.drop_column("webhook_deliveries", "state")
    op.drop_index("ix_tasks_active_issue_key", table_name="tasks")
    op.drop_column("tasks", "active_issue_key")
    op.drop_column("tasks", "ci_unobservable_ticks")
    op.drop_column("tasks", "ci_is_fork")
    op.drop_column("tasks", "ci_pr_number")
    op.drop_column("tasks", "ci_base_branch")
    op.drop_column("tasks", "ci_head_branch")
