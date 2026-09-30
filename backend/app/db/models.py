"""Core tables: repositories, tasks, task_events, memories, pull_requests.

Plus webhook_deliveries for GitHub duplicate-delivery prevention.
Designed for SQLite dev and PostgreSQL prod (no PG-only DDL here).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    github_full_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # owner/repo
    installation_id: Mapped[str] = mapped_column(String(64), default="")
    default_branch: Mapped[str] = mapped_column(String(128), default="main")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository: Mapped[str] = mapped_column(String(255), index=True)  # denormalised owner/repo
    repository_id: Mapped[int | None] = mapped_column(ForeignKey("repositories.id"), nullable=True)
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
    status: Mapped[str] = mapped_column(String(16), default="RUNNING", index=True)  # RUNNING|COMPLETED|FAILED|BLOCKED
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    # JSON-encoded metadata (tool name, args summary, result bytes, no chain-of-thought)
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository: Mapped[str] = mapped_column(String(255), index=True)
    commit_sha: Mapped[str] = mapped_column(String(128), default="")
    path: Mapped[str] = mapped_column(String(512), default="")  # file/dir scope or "__overview__"
    summary: Mapped[str] = mapped_column(Text, default="")
    last_analyzed_rev: Mapped[str] = mapped_column(String(128), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class PullRequest(Base):
    __tablename__ = "pull_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    pr_number: Mapped[int] = mapped_column(Integer, default=0)
    pr_url: Mapped[str] = mapped_column(String(512), default="")
    branch: Mapped[str] = mapped_column(String(255), default="")
    commit_sha: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
