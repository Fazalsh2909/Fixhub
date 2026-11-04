"""0010 task run provenance: browser-initiated vs trusted automation.

The existing schema could not distinguish a browser-created issue task
(`POST /api/tasks/from-issue`) from a signature-validated issue webhook task:
both persisted `trigger_type='issue'`. Task 1.8 requires an explicit,
persisted provenance decision before a worker mints a GitHub App
installation token or publishes, so this migration adds:

- `tasks.run_origin`  'automation' | 'user' (NOT NULL, default 'automation')
- `tasks.run_actor_id` the authenticated FixHub user who initiated the
  current browser run/approval (NULL for automation).

Backfill policy: every pre-migration row becomes 'automation'. Legacy rows
were created before provenance existed and cannot be classified reliably
(the exact defect this task fixes); defaulting them to 'user' would stop
every in-flight webhook task from running. The browser entry points
(from-issue, manual rerun, manual approve) write 'user' + the authenticated
actor at the moment of the action, including for rows that predate this
migration, so the only unclassified window is a task already queued by the
browser before deployment — the same run that would have executed anyway.

Revision ID: 0010_task_run_provenance
Revises: 0009_github_install_ownership
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010_task_run_provenance"
down_revision = "0009_github_install_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "run_origin",
            sa.String(16),
            nullable=False,
            server_default="automation",
        ),
    )
    op.add_column("tasks", sa.Column("run_actor_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_tasks_run_actor_id_users",
        "tasks",
        "users",
        ["run_actor_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_tasks_run_actor_id_users", "tasks", type_="foreignkey"
    )
    op.drop_column("tasks", "run_actor_id")
    op.drop_column("tasks", "run_origin")
