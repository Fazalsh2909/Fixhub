"""0001 baseline: core tables (pre-auth schema).

Repositories, tasks (RUNNING birth, no owner/claim columns), task_events,
memories (no owner), pull_requests, webhook_deliveries. Later phases layer
auth (0002), BYOK (0003), and claims/indexes (0004) on top.

Revision ID: 0001_baseline
Revises:
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("github_full_name", sa.String(255), nullable=False),
        sa.Column("installation_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("default_branch", sa.String(128), nullable=False, server_default="main"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_repositories_github_full_name", "repositories", ["github_full_name"],
                    unique=True)
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=True),
        sa.Column("trigger_type", sa.String(16), nullable=False, server_default="issue"),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("issue_title", sa.Text(), nullable=False, server_default=""),
        sa.Column("issue_body", sa.Text(), nullable=False, server_default=""),
        sa.Column("issue_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("ci_run_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("ci_sha", sa.String(128), nullable=False, server_default=""),
        sa.Column("ci_workflow", sa.String(255), nullable=False, server_default=""),
        sa.Column("ci_job", sa.String(255), nullable=False, server_default=""),
        sa.Column("ci_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("ci_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="RUNNING"),
        sa.Column("workspace", sa.String(512), nullable=False, server_default=""),
        sa.Column("branch", sa.String(255), nullable=False, server_default=""),
        sa.Column("commit_sha", sa.String(128), nullable=False, server_default=""),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("ci_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_ci_failure", sa.Text(), nullable=False, server_default=""),
        sa.Column("cancel_requested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_repository", "tasks", ["repository"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_index("ix_task_events_type", "task_events", ["type"])
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("commit_sha", sa.String(128), nullable=False, server_default=""),
        sa.Column("path", sa.String(512), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_analyzed_rev", sa.String(128), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memories_repository", "memories", ["repository"])
    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pr_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("branch", sa.String(255), nullable=False, server_default=""),
        sa.Column("commit_sha", sa.String(128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pull_requests_task_id", "pull_requests", ["task_id"])
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_deliveries_delivery_id", "webhook_deliveries",
                    ["delivery_id"], unique=True)


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("pull_requests")
    op.drop_table("memories")
    op.drop_table("task_events")
    op.drop_table("tasks")
    op.drop_table("repositories")
