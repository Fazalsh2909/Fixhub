"""Poll GitHub App installations for issue and CI events when webhooks are unavailable.

GitHub App permissions alone do not push events into FixHub. This poller uses the
App's installation token to discover newly-created issues and failed Actions runs,
then routes them through the same task creation code used by webhook deliveries.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _utc_naive(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_github_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _utc_naive(parsed)
    except (TypeError, ValueError):
        return None


def _list_recent_open_issues(token: str, full_name: str, cutoff: datetime) -> list[dict]:
    """Return only non-PR issues created after the repository was connected."""
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/issues"
    response = httpx.get(
        url,
        headers=_headers(token),
        params={"state": "open", "sort": "created", "direction": "desc", "per_page": 50},
        timeout=30,
    )
    response.raise_for_status()
    items = response.json()
    out = []
    for item in items:
        if "pull_request" in item:
            continue
        created = _parse_github_time(item.get("created_at"))
        if created is None or created < cutoff:
            continue
        out.append(item)
    return out


def _list_recent_failed_runs(token: str, full_name: str, cutoff: datetime) -> list[dict]:
    """Return recently-created failed workflow runs; callers deduplicate by run ID."""
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/actions/runs"
    response = httpx.get(
        url,
        headers=_headers(token),
        params={
            "status": "completed",
            "per_page": 30,
            "created": f">={cutoff.strftime('%Y-%m-%d')}",
        },
        timeout=30,
    )
    response.raise_for_status()
    runs = response.json().get("workflow_runs", [])
    return [
        run for run in runs
        if run.get("conclusion") == "failure"
        and (_parse_github_time(run.get("created_at")) or datetime.min) >= cutoff
    ]


def poll_github_events() -> dict[str, int]:
    """Poll all connected repos once. Best-effort per repo; never raises globally."""
    from app.db.database import SessionLocal
    from app.db.models import Repository, Task
    from app.github import app_auth
    from app.github.webhook import _handle_issue, _handle_workflow_run

    result = {"repositories": 0, "issues_created": 0, "ci_tasks_created": 0, "errors": 0}
    db = SessionLocal()
    try:
        repositories = (
            db.query(Repository)
            .filter(Repository.installation_id != "")
            .order_by(Repository.id.asc())
            .all()
        )
        for repo in repositories:
            result["repositories"] += 1
            full_name = repo.github_full_name
            cutoff = _utc_naive(repo.created_at)
            try:
                token = app_auth.installation_token(repo.installation_id)
                for issue in _list_recent_open_issues(token, full_name, cutoff):
                    number = issue.get("number")
                    if not number:
                        continue
                    exists = db.query(Task.id).filter(
                        Task.repository == full_name,
                        Task.trigger_type == "issue",
                        Task.issue_number == number,
                    ).first()
                    if exists:
                        continue
                    payload = {
                        "action": "opened",
                        "repository": {"full_name": full_name, "default_branch": repo.default_branch},
                        "issue": {
                            "number": number,
                            "title": issue.get("title", ""),
                            "body": issue.get("body") or "",
                            "html_url": issue.get("html_url", ""),
                            "labels": issue.get("labels") or [],
                        },
                    }
                    outcome = _handle_issue(db, payload)
                    if outcome.get("task_id"):
                        result["issues_created"] += 1

                for run in _list_recent_failed_runs(token, full_name, cutoff):
                    run_id = str(run.get("id") or "")
                    if not run_id:
                        continue
                    exists = db.query(Task.id).filter(
                        Task.repository == full_name,
                        Task.trigger_type == "ci",
                        Task.ci_run_id == run_id,
                    ).first()
                    if exists:
                        continue
                    payload = {
                        "action": "completed",
                        "repository": {"full_name": full_name, "default_branch": repo.default_branch},
                        "workflow_run": run,
                    }
                    outcome = _handle_workflow_run(db, payload)
                    if outcome.get("task_id") and not outcome.get("duplicate"):
                        result["ci_tasks_created"] += 1
            except Exception:
                result["errors"] += 1
                log.exception("GitHub event poll failed for %s", full_name)
        return result
    finally:
        db.close()
