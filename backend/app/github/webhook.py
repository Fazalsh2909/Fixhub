"""GitHub webhook: signature verify + dedupe + issue/CI trigger -> Task.

Events: issues.opened/reopened (+ optional `fixhub-fix` label gate),
workflow_run.completed/failed, check_run.completed/failure.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Header, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db.database import SessionLocal
from app.db.models import Repository, Task, TaskEvent, WebhookDelivery

router = APIRouter()


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + expected, signature)


def _event(db: Session, task_id: int, type_: str, data: dict) -> None:
    db.add(TaskEvent(task_id=task_id, type=type_, data_json=json.dumps(data)[:4000]))


def _ci_log_excerpt(*, repo_name: str, run_id: str, check_name: str) -> str:
    """Fetch the failing-job log excerpt so the agent sees the real error.

    Best-effort: any failure (no installation id, no Actions read, network)
    returns "" and callers fall back to the one-line excerpt. Never raises.
    """
    if not run_id:
        return ""
    try:
        from app.db.models import Repository as _Repo
        from app.github import app_auth as _app_auth
        from app.github import client as _gh
        from app.db.database import SessionLocal as _SessionLocal
    except Exception:
        return ""
    try:
        s = _SessionLocal()
        try:
            row = s.query(_Repo).filter(_Repo.github_full_name == repo_name).first()
            installation_id = row.installation_id if row else ""
        finally:
            s.close()
        if not installation_id:
            return ""
        token = _app_auth.installation_token(installation_id)
        out = _gh.failing_logs_excerpt(token=token, full_name=repo_name, run_id=run_id)
        if check_name and check_name not in out:
            out = f"check {check_name} failed\n{out}"
        return out[:6000]
    except Exception:
        return ""


def _run_id_from_url(url: str) -> str:
    """Extract the workflow-run id from a check_run html_url (.../runs/123/job/456)."""
    import re as _re

    m = _re.search(r"/runs/(\d+)", url or "")
    return m.group(1) if m else ""


def _repo_token(repo_name: str) -> str:
    """Installation token for a repo, or "" when unavailable. Never raises."""
    try:
        from app.db.models import Repository as _Repo
        from app.github import app_auth as _app_auth
        from app.db.database import SessionLocal as _SessionLocal

        s = _SessionLocal()
        try:
            row = s.query(_Repo).filter(_Repo.github_full_name == repo_name).first()
            installation_id = row.installation_id if row else ""
        finally:
            s.close()
        if not installation_id:
            return ""
        return _app_auth.installation_token(installation_id)
    except Exception:
        return ""


def build_ci_context(*, event_type: str, payload: dict, repo_name: str) -> dict:
    """Structured CI context for the agent (best-effort enrichment, never raises).

    Fields: provider, workflow_name, workflow_file, workflow_content,
    run_id, commit_sha, branch, job, step, exit_code, failure_logs,
    annotations, changed_files, url. Missing pieces stay "".
    """
    ctx: dict = {
        "provider": "github", "workflow_name": "", "workflow_file": "",
        "workflow_content": "", "run_id": "", "commit_sha": "", "branch": "",
        "job": "", "step": "", "exit_code": "", "failure_logs": "",
        "annotations": "", "changed_files": "", "url": "",
    }
    try:
        token = _repo_token(repo_name)
        if event_type == "workflow_run":
            run = payload.get("workflow_run", {})
            ctx.update({
                "workflow_name": run.get("name", ""),
                "workflow_file": run.get("path", ""),
                "run_id": str(run.get("id", "")),
                "commit_sha": run.get("head_sha", ""),
                "branch": run.get("head_branch", ""),
                "job": run.get("name", ""),
                "url": run.get("html_url", ""),
            })
            if token:
                try:
                    steps = _gh_steps(token, repo_name, ctx["run_id"])
                    if steps:
                        ctx["job"] = steps[0]["job"]
                        ctx["step"] = "; ".join(
                            f"{s['step']} ({s['conclusion']})" for s in steps[:4])
                except Exception:
                    pass
                if ctx["workflow_file"]:
                    try:
                        from app.github import client as _ghc
                        ctx["workflow_content"] = _ghc.get_workflow_content(
                            token=token, full_name=repo_name,
                            path=ctx["workflow_file"], ref=ctx["commit_sha"])[:6000]
                    except Exception:
                        pass
                pr_files = _pr_files_for_run(payload, token, repo_name)
                if pr_files:
                    ctx["changed_files"] = ", ".join(pr_files[:20])
        elif event_type == "check_run":
            run = payload.get("check_run", {})
            suite = run.get("check_suite") or {}
            ctx.update({
                "run_id": _run_id_from_url(run.get("html_url", "")),
                "commit_sha": run.get("head_sha", ""),
                "branch": suite.get("head_branch", ""),
                "job": run.get("name", ""),
                "url": run.get("html_url", ""),
            })
            output = run.get("output") or {}
            title = str(output.get("title") or "")
            summary = str(output.get("summary") or "")[:1500]
            if title or summary:
                ctx["step"] = (title + (" — " + summary if summary else ""))[:2000]
            if token and run.get("id") is not None:
                try:
                    from app.github import client as _ghc
                    anns = _ghc.check_annotations(
                        token=token, full_name=repo_name, check_run_id=run.get("id"))
                    if anns:
                        ctx["annotations"] = "; ".join(
                            f"{a['path']}:{a['line'] or '?'} [{a['level']}] "
                            f"{a['message'][:200]}" for a in anns[:5])[:2000]
                except Exception:
                    pass
            prs = run.get("pull_requests") or []
            if token and prs and prs[0].get("number"):
                try:
                    from app.github import client as _ghc
                    files = _ghc.pull_files(token=token, full_name=repo_name,
                                            number=prs[0]["number"])
                    ctx["changed_files"] = ", ".join(files[:20])
                except Exception:
                    pass
    except Exception:
        pass
    return ctx


def _gh_steps(token: str, repo_name: str, run_id: str) -> list[dict]:
    from app.github import client as _ghc

    if not run_id:
        return []
    return _ghc.failing_steps(token=token, full_name=repo_name, run_id=run_id)


def _pr_files_for_run(payload: dict, token: str, repo_name: str) -> list[str]:
    try:
        from app.github import client as _ghc

        run = payload.get("workflow_run", {})
        prs = run.get("pull_requests") or []
        if not prs or not prs[0].get("number"):
            return []
        return _ghc.pull_files(token=token, full_name=repo_name, number=prs[0]["number"])
    except Exception:
        return []


def _maybe_enqueue(db: Session, task_id: int) -> dict:
    """Auto-enqueue when AUTO_RUN_ON_WEBHOOK=1. Never raises; records QUEUED/QUEUE_FAILED."""
    if not settings.AUTO_RUN_ON_WEBHOOK:
        return {"enqueued": False, "reason": "disabled"}
    try:
        from app.tasks import queue as _queue
    except Exception:
        return {"enqueued": False, "reason": "queue module unavailable"}
    try:
        out = _queue.enqueue_task(task_id)
    except Exception as exc:
        out = {"enqueued": False, "error": str(exc)[:200]}
    try:
        if out.get("enqueued"):
            _event(db, task_id, "QUEUED", {"job": out.get("job_id", "")})
        else:
            _event(db, task_id, "QUEUE_FAILED", {"error": out.get("error", "unavailable")[:300]})
        db.commit()
    except Exception:
        pass
    return out


def _active_ci_task(db: Session, *, repo_name: str, sha: str, job: str) -> Task | None:
    """An already-RUNNING CI task for the same repo+sha+job.

    Statuses flip to terminal only when a run ends, so RUNNING also covers
    queued-not-started jobs. Creating another task for the same failure would
    just burn a second full agent run for the same fix.
    """
    if not sha:
        return None
    q = db.query(Task).filter(
        Task.repository == repo_name,
        Task.trigger_type == "ci",
        Task.status == "RUNNING",
        Task.ci_sha == sha,
    )
    if job:
        q = q.filter((Task.ci_job == job) | (Task.ci_workflow == job))
    return q.order_by(Task.id.desc()).first()


def _get_or_create_repo(db: Session, full_name: str, default_branch: str = "main") -> Repository:
    repo = db.query(Repository).filter(Repository.github_full_name == full_name).first()
    if not repo:
        repo = Repository(github_full_name=full_name, default_branch=default_branch)
        db.add(repo)
        db.commit()
        db.refresh(repo)
    return repo


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_github_event: str = Header(default=""),
):
    from fastapi.responses import JSONResponse

    body = await request.body()
    if not verify_signature(settings.GITHUB_WEBHOOK_SECRET, body, x_hub_signature_256):
        return JSONResponse(status_code=401, content={"error": "bad signature"})
    if not x_github_delivery:
        return JSONResponse(status_code=400, content={"error": "missing delivery id"})

    db = SessionLocal()
    try:
        if db.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == x_github_delivery).first():
            return {"ok": True, "duplicate": True}
        db.add(WebhookDelivery(delivery_id=x_github_delivery))
        db.commit()

        payload = json.loads(body.decode("utf-8") or "{}")
        if x_github_event == "issues":
            return _handle_issue(db, payload)
        if x_github_event == "workflow_run":
            return _handle_workflow_run(db, payload)
        if x_github_event == "check_run":
            return _handle_check_run(db, payload)
        return {"ok": True, "ignored": x_github_event}
    finally:
        db.close()


def _handle_issue(db: Session, payload: dict) -> dict:
    action = payload.get("action")
    if action not in ("opened", "reopened"):
        return {"ok": True, "ignored": f"issues.{action}"}
    labels = [l.get("name", "") for l in (payload.get("issue", {}).get("labels") or [])]
    # Optional label gate: if the repo uses `fixhub-fix`, only those issues trigger.
    # We do NOT require it — plain opened/reopened triggers by default.
    _ = labels
    repo_name = payload.get("repository", {}).get("full_name", "")
    if not repo_name:
        return {"ok": False, "error": "missing repository"}
    issue = payload.get("issue", {})
    _get_or_create_repo(db, repo_name)
    task = Task(
        repository=repo_name,
        trigger_type="issue",
        issue_number=issue.get("number"),
        issue_title=issue.get("title", "")[:500],
        issue_body=(issue.get("body") or "")[:8000],
        issue_url=issue.get("html_url", ""),
        status="RUNNING",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    _event(db, task.id, "TASK_CREATED", {"trigger": "issue", "issue": task.issue_number})
    db.commit()
    queue = _maybe_enqueue(db, task.id)
    return {"ok": True, "task_id": task.id, "queued": bool(queue.get("enqueued"))}


def _handle_workflow_run(db: Session, payload: dict) -> dict:
    action = payload.get("action")
    run = payload.get("workflow_run", {})
    if action != "completed" or run.get("conclusion") != "failure":
        return {"ok": True, "ignored": f"workflow_run.{action}/{run.get('conclusion')}"}
    repo_name = payload.get("repository", {}).get("full_name", "")
    if not repo_name:
        return {"ok": False, "error": "missing repository"}
    _get_or_create_repo(db, repo_name)
    dup = _active_ci_task(db, repo_name=repo_name, sha=run.get("head_sha", ""),
                          job=run.get("name", ""))
    if dup is not None:
        return {"ok": True, "task_id": dup.id, "duplicate": "active_task"}
    base_excerpt = f"workflow {run.get('name')} failed on {run.get('head_branch')}"
    logs = _ci_log_excerpt(repo_name=repo_name, run_id=str(run.get("id", "")),
                           check_name=run.get("name", ""))
    task = Task(
        repository=repo_name,
        trigger_type="ci",
        ci_run_id=str(run.get("id", "")),
        ci_sha=run.get("head_sha", ""),
        ci_workflow=run.get("name", ""),
        ci_url=run.get("html_url", ""),
        ci_excerpt=(f"{base_excerpt}\n--- failing logs ---\n{logs}" if logs else base_excerpt),
        status="RUNNING",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    _event(db, task.id, "TASK_CREATED", {"trigger": "ci", "run": task.ci_run_id})
    _event(db, task.id, "CI_CONTEXT_LOADED",
           build_ci_context(event_type="workflow_run", payload=payload, repo_name=repo_name))
    db.commit()
    queue = _maybe_enqueue(db, task.id)
    return {"ok": True, "task_id": task.id, "queued": bool(queue.get("enqueued"))}


def _handle_check_run(db: Session, payload: dict) -> dict:
    if payload.get("action") != "completed":
        return {"ok": True, "ignored": "check_run.not_completed"}
    run = payload.get("check_run", {})
    if run.get("conclusion") != "failure":
        return {"ok": True, "ignored": "check_run.not_failure"}
    repo_name = payload.get("repository", {}).get("full_name", "")
    if not repo_name:
        return {"ok": False, "error": "missing repository"}
    _get_or_create_repo(db, repo_name)
    dup = _active_ci_task(db, repo_name=repo_name, sha=run.get("head_sha", ""),
                          job=run.get("name", ""))
    if dup is not None:
        return {"ok": True, "task_id": dup.id, "duplicate": "active_task"}
    base_excerpt = f"check {run.get('name')} failed"
    logs = _ci_log_excerpt(repo_name=repo_name,
                           run_id=_run_id_from_url(run.get("html_url", "")),
                           check_name=run.get("name", ""))
    task = Task(
        repository=repo_name,
        trigger_type="ci",
        ci_sha=run.get("head_sha", ""),
        ci_job=run.get("name", ""),
        ci_url=run.get("html_url", ""),
        ci_excerpt=(f"{base_excerpt}\n--- failing logs ---\n{logs}" if logs else base_excerpt),
        status="RUNNING",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    _event(db, task.id, "TASK_CREATED", {"trigger": "ci-check"})
    _event(db, task.id, "CI_CONTEXT_LOADED",
           build_ci_context(event_type="check_run", payload=payload, repo_name=repo_name))
    db.commit()
    queue = _maybe_enqueue(db, task.id)
    return {"ok": True, "task_id": task.id, "queued": bool(queue.get("enqueued"))}
