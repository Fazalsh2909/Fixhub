"""0004 phase4 claims: QUEUED birth, lease columns, connection unique, indexes.

- tasks: claimed_by / claimed_at / lease_expires_at / queue_job_id,
  status default QUEUED (was RUNNING).
- github_connections: unique (user_id, installation_id).
- Production indexes for owner/status, repo/status, event polling.

Revision ID: 0004_phase4_claims
Revises: 0003_phase3_byok
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_phase4_claims"
down_revision = "0003_phase3_byok"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("claimed_by", sa.String(64), nullable=False,
                                     server_default=""))
    op.add_column("tasks", sa.Column("claimed_at", sa.DateTime(timezone=True),
                                     nullable=True))
    op.add_column("tasks", sa.Column("lease_expires_at", sa.DateTime(timezone=True),
                                     nullable=True))
    op.add_column("tasks", sa.Column("queue_job_id", sa.String(128), nullable=False,
                                     server_default=""))
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        # QUEUED birth: alter the column default (existing rows keep their
        # status). SQLite has no ALTER COLUMN; ORM Python-side defaults apply.
        op.alter_column("tasks", "status", server_default="QUEUED")
    # Unique expressed as an index: supported on SQLite and PostgreSQL alike
    # (functionally identical enforcement for the app's purposes).
    op.create_index("uq_gh_connection_user_install", "github_connections",
                    ["user_id", "installation_id"], unique=True)
    op.create_index("ix_tasks_status_updated", "tasks", ["status", "updated_at"])
    op.create_index("ix_tasks_owner_status", "tasks", ["owner_id", "status"])
    op.create_index("ix_tasks_repo_status", "tasks", ["repository", "status"])
    op.create_index("ix_task_events_task_poll", "task_events", ["task_id", "id"])
    op.create_index("ix_tasks_repository_id", "tasks", ["repository_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_repository_id", "tasks")
    op.drop_index("ix_task_events_task_poll", "task_events")
    op.drop_index("ix_tasks_repo_status", "tasks")
    op.drop_index("ix_tasks_owner_status", "tasks")
    op.drop_index("ix_tasks_status_updated", "tasks")
    op.drop_index("uq_gh_connection_user_install", "github_connections")
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.alter_column("tasks", "status", server_default="RUNNING")
    op.drop_column("tasks", "queue_job_id")
    op.drop_column("tasks", "lease_expires_at")
    op.drop_column("tasks", "claimed_at")
    op.drop_column("tasks", "claimed_by")
