"""Core tables: repositories, tasks, task_events, memories, pull_requests.

Plus webhook_deliveries for GitHub duplicate-delivery prevention.
Designed for SQLite dev and PostgreSQL prod (no PG-only DDL here).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    github_full_name: Mapped[str] = mapped_column(
        String(255), unique=True, index=True
    )  # owner/repo
    installation_id: Mapped[str] = mapped_column(String(64), default="")
    default_branch: Mapped[str] = mapped_column(String(128), default="main")
    # Phase 2: owning FixHub user. Nullable so pre-auth rows stay readable by
    # nobody (fail-safe 404) instead of breaking existing databases.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository: Mapped[str] = mapped_column(
        String(255), index=True
    )  # denormalised owner/repo
    repository_id: Mapped[int | None] = mapped_column(
        ForeignKey("repositories.id"), nullable=True, index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(16), default="issue")  # issue | ci
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_title: Mapped[str] = mapped_column(Text, default="")
    issue_body: Mapped[str] = mapped_column(Text, default="")
    issue_url: Mapped[str] = mapped_column(String(512), default="")
    ci_run_id: Mapped[str] = mapped_column(String(128), default="")
    ci_sha: Mapped[str] = mapped_column(String(128), default="")
    ci_workflow: Mapped[str] = mapped_column(String(255), default="")
    ci_job: Mapped[str] = mapped_column(String(255), default="")
    ci_url: Mapped[str] = mapped_column(String(512), default="")
    ci_excerpt: Mapped[str] = mapped_column(Text, default="")
    # Phase 4: QUEUED birth + atomic worker claim (QUEUED -> RUNNING).
    # Lifecycle: QUEUED -> RUNNING -> COMPLETED|FAILED|BLOCKED|NEEDS_REVIEW|CANCELLED|AWAITING_CI;
    # AWAITING_CI -> COMPLETED | RUNNING (repair) | FAILED.
    status: Mapped[str] = mapped_column(String(16), default="QUEUED", index=True)
    workspace: Mapped[str] = mapped_column(String(512), default="")
    branch: Mapped[str] = mapped_column(String(255), default="")
    commit_sha: Mapped[str] = mapped_column(String(128), default="")
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str] = mapped_column(String(512), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    # CI repair lifecycle (req 12): attempts so far, latest failure tail,
    # user cancellation flag polled by the agent loop.
    ci_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_ci_failure: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0)
    # Phase 2: owning FixHub user (set at creation from trusted server context).
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    # Phase 4: worker claim/lease. claimed_by identifies the worker (or "sync");
    # lease_expires_at bounds crash recovery; queue_job_id tracks the RQ job.
    claimed_by: Mapped[str] = mapped_column(String(64), default="")
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    queue_job_id: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_tasks_status_updated", "status", "updated_at"),
        Index("ix_tasks_owner_status", "owner_id", "status"),
        Index("ix_tasks_repo_status", "repository", "status"),
    )


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    # JSON-encoded metadata (tool name, args summary, result bytes, no chain-of-thought)
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (Index("ix_task_events_task_poll", "task_id", "id"),)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository: Mapped[str] = mapped_column(String(255), index=True)
    commit_sha: Mapped[str] = mapped_column(String(128), default="")
    path: Mapped[str] = mapped_column(
        String(512), default=""
    )  # file/dir scope or "__overview__"
    summary: Mapped[str] = mapped_column(Text, default="")
    last_analyzed_rev: Mapped[str] = mapped_column(String(128), default="")
    # Phase 2: owning FixHub user (copied from the task's owner at write time).
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class PullRequest(Base):
    __tablename__ = "pull_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    pr_number: Mapped[int] = mapped_column(Integer, default=0)
    pr_url: Mapped[str] = mapped_column(String(512), default="")
    branch: Mapped[str] = mapped_column(String(255), default="")
    commit_sha: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class User(Base):
    """Phase 2: FixHub local user (email/password auth; no GitHub OAuth).

    Never selected into API responses wholesale — auth endpoints project only
    safe fields (id/email/display_name/avatar_url).
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    display_name: Mapped[str] = mapped_column(String(128), default="")
    avatar_url: Mapped[str] = mapped_column(String(512), default="")
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    # Phase 4.5: minimal admin flag (USER/ADMIN only, no RBAC framework).
    is_admin: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class UserSession(Base):
    """Phase 2: opaque browser session. Only the SHA-256 hash is stored; the
    raw token travels solely as an HttpOnly cookie value."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class LoginAttempt(Base):
    """Phase 4.5: durable login rate-limit buckets (per-account + per-IP).

    Privacy-conscious: IPs are stored as truncated SHA-256 hashes only, and
    rows are pruned to the active window (no indefinite retention).
    """

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Exactly one of these is set per row ("email:<addr>" or "ip:<hash>").
    bucket: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GitHubConnection(Base):
    """Phase 2: mapping of FixHub user -> GitHub App installation.

    Minimum metadata to associate user/install/repositories. No tokens or
    secrets are stored here (installation tokens stay in-memory only).
    """

    __tablename__ = "github_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    installation_id: Mapped[str] = mapped_column(String(64), index=True)
    github_account: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "installation_id", name="uq_gh_connection_user_install"
        ),
    )


class LLMCredential(Base):
    """Phase 3 BYOK: one encrypted LLM credential per user + provider.

    The raw API key is NEVER stored: only the Fernet-encrypted blob plus a
    display-only key_hint (last 4 chars). Decryption happens exclusively in
    the key-management service for runtime use; deleting the row removes all
    secret-derived material for that credential.
    """

    __tablename__ = "llm_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="")
    base_url: Mapped[str] = mapped_column(
        String(512), default=""
    )  # custom endpoints only
    encrypted_secret: Mapped[str] = mapped_column(Text, default="")
    key_hint: Mapped[str] = mapped_column(String(16), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default="configured")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_llm_credential_user_provider"),
    )


class LLMUsage(Base):
    """Phase 3: safe per-run LLM usage metadata. Token counts are nullable
    (providers may not report them). Never stores prompts or API keys."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[int] = mapped_column(Integer, default=0)
    error_category: Mapped[str] = mapped_column(String(64), default="")
