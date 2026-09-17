"""GitHub webhook validation + gateway. Raw body first, then HMAC, then parse."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..automation import launch_task
from ..config import settings
from ..db import SessionLocal
from ..logging import get_logger, log_event
from ..models import GitHubAccount, Repository, Task, TaskEvent, WebhookDelivery
from ..queue import enqueue

router = APIRouter()
logger = get_logger("fixhub.webhook")
TRIGGER_LABEL = "fixhub-fix"


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(signature, expected)


def _wants_fix(event: str, payload: dict) -> tuple[bool, str]:
    """Decide if this webhook should create a task. Extensible for future triggers."""
    if event == "issues":
        action = payload.get("action", "")
        labels = [
            lbl.get("name", "") for lbl in payload.get("issue", {}).get("labels", [])
        ]
        if action == "opened" and TRIGGER_LABEL in labels:
            return True, "labeled-on-open"
        if (
            action == "labeled"
            and payload.get("label", {}).get("name") == TRIGGER_LABEL
        ):
            return True, "labeled"
        # future: reopened/assigned handling lands here
    if event == "issue_comment":  # stub: /fix comments (parsed by orchestrator later)
        body = (payload.get("comment", {}).get("body", "") or "").strip()
        if body.startswith("/fix"):
            return True, "fix-comment"
    return False, ""


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(default=""),
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
) -> dict:
    raw = await request.body()  # MUST read raw bytes before parsing
    if not verify_signature(raw, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    launch_after_commit: int | None = None
    db: Session = SessionLocal()
    try:
        # idempotency: duplicate deliveries ack 200 without side effects
        if db.query(WebhookDelivery).filter_by(delivery_id=x_github_delivery).first():
            return {"status": "duplicate"}
        ok, reason = _wants_fix(x_github_event, payload)
        if not ok and settings.auto_trigger_on_issue and x_github_event == "issues":
            # Stay-connected mode: any opened/reopened issue on a connected repo.
            action = payload.get("action", "")
            repo_name = payload.get("repository", {}).get("full_name", "")
            if action in ("opened", "reopened"):
                repo_row = db.query(Repository).filter_by(full_name=repo_name).first()
                if repo_row is not None and repo_row.connected:
                    ok, reason = True, f"auto-{action}-connected"
        task_id = None
        if ok:
            repo_name = payload.get("repository", {}).get("full_name", "unknown")
            issue = payload.get("issue", {})
            repo = db.query(Repository).filter_by(full_name=repo_name).first()
            if repo is None:
                repo = Repository(full_name=repo_name)
                db.add(repo)
                db.flush()
            inst_id_task = str((payload.get("installation", {}) or {}).get("id", ""))
            if inst_id_task and not repo.installation_id:
                repo.installation_id = inst_id_task
            task = Task(
                repo_id=repo.id,
                issue_number=issue.get("number", 0),
                title=issue.get("title", ""),
                state="CREATED",
            )
            db.add(task)
            db.flush()
            db.add(
                TaskEvent(task_id=task.id, stage="CREATED", message=f"trigger={reason}")
            )
            task_id = task.id
            enqueue({"task_id": task_id, "repo": repo_name, "issue": task.issue_number})
            if settings.auto_run:
                launch_after_commit = task_id
            log_event(
                logger, "task_enqueued", task_id=task_id, repo=repo_name, reason=reason
            )
        db.add(
            WebhookDelivery(
                delivery_id=x_github_delivery,
                event_type=x_github_event,
                task_id=task_id,
            )
        )
        # Remember the installation so /api/github/status shows the connection.
        inst = payload.get("installation", {}) or {}
        inst_id = str(inst.get("id", ""))
        if inst_id:
            acct = db.query(GitHubAccount).filter_by(installation_id=inst_id).first()
            if acct is None:
                repo_full = payload.get("repository", {}).get("full_name", "")
                login = repo_full.split("/")[0] if "/" in repo_full else ""
                db.add(
                    GitHubAccount(
                        login=login, installation_id=inst_id, account_type="User"
                    )
                )
        db.commit()
        if launch_after_commit is not None:
            launch_task(launch_after_commit)
        return {"status": "ok", "task_id": task_id}
    finally:
        db.close()
