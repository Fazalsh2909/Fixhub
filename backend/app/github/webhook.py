"""GitHub webhook: simple issue-opened trigger. HMAC → record → enqueue → 200.

Simple flow only: issue opened/reopened on a connected repo creates one
RUNNING issue run and enqueues {"run_id", "repository", "issue_number"}.
No expensive work here; the worker dequeues and runs the agent.
Security kept: raw-body HMAC first, idempotent on X-GitHub-Delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session

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
    """Simple trigger: issue opened/reopened, or explicit fix label/comment."""
    if event == "issues":
        action = payload.get("action", "")
        labels = [
            lbl.get("name", "") for lbl in payload.get("issue", {}).get("labels", [])
        ]
        if (
            action == "labeled"
            and (payload.get("label", {}) or {}).get("name") == TRIGGER_LABEL
        ):
            return True, "labeled"
        if TRIGGER_LABEL in labels and action in ("opened", "labeled", "reopened"):
            return True, "labeled" if action != "opened" else "labeled-on-open"
        if action in ("opened", "reopened"):
            # Auto-trigger for connected repos is decided below (needs DB);
            # signal intent here so the handler checks connectedness.
            return True, f"auto-{action}-intent"
    if event == "issue_comment":
        if _comment_author_is_bot(payload):
            return False, "ignored-bot"
        body = (payload.get("comment", {}).get("body", "") or "").strip()
        if body.startswith("/fix"):
            return True, "fix-comment"
        # A human reply on an issue we asked about re-triggers the agent;
        # the DB check below confirms we are actually awaiting a reply.
        if body:
            return True, "comment-intent"
    return False, ""


def _comment_author_is_bot(payload: dict) -> bool:
    """Our own ask-back comments (and any bot) must never re-trigger us."""
    user = (payload.get("comment", {}) or {}).get("user", {}) or {}
    login = str(user.get("login", ""))
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _is_followup(db: Session, repo_id: int, issue_number: int) -> bool:
    """True when FixHub asked a clarifying question on this issue and no PR
    has been recorded for it since — i.e. we are awaiting the human's reply."""
    from ..models import PullRequest

    task_ids = [
        t.id
        for t in db.query(Task)
        .filter_by(repo_id=repo_id, issue_number=issue_number)
        .all()
    ]
    if not task_ids:
        return False
    if db.query(PullRequest).filter(PullRequest.task_id.in_(task_ids)).count():
        return False
    return (
        db.query(TaskEvent)
        .filter(
            TaskEvent.task_id.in_(task_ids),
            TaskEvent.stage == "COMMENT_POSTED",
        )
        .count()
        > 0
    )


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

    db: Session = SessionLocal()
    try:
        # idempotency: duplicate deliveries ack 200 without side effects
        if db.query(WebhookDelivery).filter_by(delivery_id=x_github_delivery).first():
            return {"status": "duplicate"}
        ok, reason = _wants_fix(x_github_event, payload)
        skip_reason = ""
        if ok and x_github_event == "issues" and reason.endswith("-intent"):
            # Auto-trigger requires a connected repo; label/comment triggers
            # skip this check (explicit user intent).
            action = payload.get("action", "")
            repo_name = payload.get("repository", {}).get("full_name", "")
            if not settings.auto_trigger_on_issue:
                ok, reason = False, ""
                skip_reason = "auto-trigger off (AUTO_TRIGGER_ON_ISSUE=false)"
            else:
                repo_row = db.query(Repository).filter_by(full_name=repo_name).first()
                if repo_row is None:
                    ok, reason = False, ""
                    skip_reason = (
                        f"repo {repo_name} unknown to FixHub — connect it first"
                    )
                elif not repo_row.connected:
                    ok, reason = False, ""
                    skip_reason = (
                        f"repo {repo_name} is not connected — connect it first"
                    )
                else:
                    reason = f"auto-{action}-connected"
        if ok and x_github_event == "issue_comment" and reason == "comment-intent":
            # Reply re-trigger: only when we asked something and no PR exists
            # yet for this issue. Connected repo required, like auto-trigger.
            repo_name = payload.get("repository", {}).get("full_name", "")
            repo_row = db.query(Repository).filter_by(full_name=repo_name).first()
            if repo_row is None or not repo_row.connected:
                ok, reason = False, ""
                skip_reason = f"repo {repo_name} is not connected — connect it first"
            elif not _is_followup(
                db, repo_row.id, payload.get("issue", {}).get("number", 0)
            ):
                ok, reason = False, ""
                skip_reason = "comment is not a reply to a FixHub question — ignoring"
            else:
                reason = "comment-reply"
        if not ok and not skip_reason:
            skip_reason = reason or f"no trigger rule matched (event={x_github_event})"
        task_id = None
        if ok:
            repo_name = payload.get("repository", {}).get("full_name", "unknown")
            issue = payload.get("issue", {})
            if x_github_event == "issue_comment":
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
                title=issue.get("title", "") or "",
                state="RUNNING",
            )
            db.add(task)
            db.flush()
            # Store the issue body + triggering comment alongside the run
            # so the worker can build the prompt without another API call.
            try:
                from ..models import ChatMessage

                body = (issue.get("body", "") or "").strip()
                if body:
                    db.add(
                        ChatMessage(
                            task_id=task.id,
                            repo_id=repo.id,
                            role="user",
                            content=body[:4000],
                        )
                    )
                if x_github_event == "issue_comment":
                    comment = payload.get("comment", {}) or {}
                    cbody = (comment.get("body", "") or "").strip()
                    login = str((comment.get("user", {}) or {}).get("login", ""))
                    if cbody:
                        db.add(
                            ChatMessage(
                                task_id=task.id,
                                repo_id=repo.id,
                                role="user",
                                content=f"Reply by {login}: {cbody[:2000]}",
                            )
                        )
            except Exception:
                pass
            db.add(
                TaskEvent(task_id=task.id, stage="RUNNING", message=f"trigger={reason}")
            )
            task_id = task.id
            # Simple job payload: the worker only needs these three keys.
            # task_id is kept for back-compat consumers.
            enqueue(
                {
                    "run_id": task_id,
                    "task_id": task_id,
                    "repository": repo_name,
                    "repo": repo_name,
                    "issue_number": task.issue_number,
                    "issue": task.issue_number,
                }
            )
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
        # No sync launch here: the queue worker picks up the job. Webhook
        # returns 200 quickly by design.
        if task_id is None:
            # Silent no-ops are why missed triggers confuse users: always say why.
            log_event(
                logger, "webhook_skipped", reason=skip_reason or "no trigger matched"
            )
            return {
                "status": "ok",
                "task_id": None,
                "triggered": False,
                "reason": skip_reason,
            }
        return {"status": "ok", "task_id": task_id, "triggered": True}
    finally:
        db.close()
