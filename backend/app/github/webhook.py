"""GitHub webhook: signature verify + dedupe + issue/CI trigger -> Task.

Events: issues.opened/reopened (+ optional `fixhub-fix` label gate) and
workflow_run.completed failures. `check_run` is opt-in (CI_TRIGGER_EVENTS)
because it fires per job and duplicates workflow_run.

Only repositories explicitly connected in FixHub (`repositories.installation_id`
set via POST /api/repositories/connect) may create tasks. Every created task is
enqueued immediately; one CI task per (repository, head_sha).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db.database import SessionLocal
from app.db.models import Repository, Task, TaskEvent, WebhookDelivery

router = APIRouter()
log = logging.getLogger("fixhub.webhook")


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature:
        return False
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + expected, signature)


def _event(
    db: Session, task_id: int, type_: str, data: dict, *, limit: int = 4000
) -> None:
    # CI_CONTEXT_LOADED carries the workflow YAML (up to 6000 chars); the
    # default 4000-char cap used to cut the JSON mid-object, so json.loads()
    # failed and the agent lost every workflow field. Callers that persist
    # larger payloads pass a limit that fits them.
    db.add(TaskEvent(task_id=task_id, type=type_, data_json=json.dumps(data)[:limit]))


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
        out = _gh.failed_log_tail(
            token=token,
            full_name=repo_name,
            run_id=run_id,
            job_name=check_name or "",
        )
        if not out:
            return ""
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
        "provider": "github",
        "workflow_name": "",
        "workflow_file": "",
        "workflow_content": "",
        "run_id": "",
        "commit_sha": "",
        "branch": "",
        "job": "",
        "step": "",
        "exit_code": "",
        "failure_logs": "",
        "annotations": "",
        "changed_files": "",
        "url": "",
    }
    try:
        token = _repo_token(repo_name)
        if event_type == "workflow_run":
            run = payload.get("workflow_run", {})
            ctx.update(
                {
                    "workflow_name": run.get("name", ""),
                    "workflow_file": run.get("path", ""),
                    "run_id": str(run.get("id", "")),
                    "commit_sha": run.get("head_sha", ""),
                    "branch": run.get("head_branch", ""),
                    "job": run.get("name", ""),
                    "url": run.get("html_url", ""),
                }
            )
            if token:
                try:
                    steps = _gh_steps(token, repo_name, ctx["run_id"])
                    if steps:
                        ctx["job"] = steps[0]["job"]
                        ctx["step"] = "; ".join(
                            f"{s['step']} ({s['conclusion']})" for s in steps[:4]
                        )
                except Exception:
                    pass
                if ctx["workflow_file"]:
                    try:
                        from app.github import client as _ghc

                        ctx["workflow_content"] = _ghc.get_workflow_content(
                            token=token,
                            full_name=repo_name,
                            path=ctx["workflow_file"],
                            ref=ctx["commit_sha"],
                        )[:6000]
                    except Exception:
                        pass
                pr_files = _pr_files_for_run(payload, token, repo_name)
                if pr_files:
                    ctx["changed_files"] = ", ".join(pr_files[:20])
        elif event_type == "check_run":
            run = payload.get("check_run", {})
            suite = run.get("check_suite") or {}
            run_id = _run_id_from_url(run.get("html_url", ""))
            ctx.update(
                {
                    "run_id": run_id,
                    "commit_sha": run.get("head_sha", ""),
                    "branch": run.get("head_branch") or suite.get("head_branch", ""),
                    "job": run.get("name", ""),
                    "url": run.get("html_url", ""),
                }
            )
            # A check_run names only the job. Fetch the parent workflow run so
            # the agent gets the workflow file/content and run-level sha/branch
            # instead of being told to "read the workflow first".
            if token and run_id:
                try:
                    from app.github import client as _ghc

                    wr = _ghc.get_workflow_run(
                        token=token, full_name=repo_name, run_id=run_id
                    )
                    ctx["workflow_name"] = wr.get("name", "")
                    ctx["workflow_file"] = wr.get("path", "")
                    ctx["commit_sha"] = ctx["commit_sha"] or wr.get("head_sha", "")
                    ctx["branch"] = ctx["branch"] or wr.get("head_branch", "")
                    if ctx["workflow_file"]:
                        ctx["workflow_content"] = _ghc.get_workflow_content(
                            token=token,
                            full_name=repo_name,
                            path=ctx["workflow_file"],
                            ref=ctx["commit_sha"],
                        )[:6000]
                except Exception:
                    pass
            output = run.get("output") or {}
            title = str(output.get("title") or "")
            summary = str(output.get("summary") or "")[:1500]
            if title or summary:
                ctx["step"] = (title + (" — " + summary if summary else ""))[:2000]
            if token and run_id and not ctx["step"]:
                try:
                    steps = _gh_steps(token, repo_name, run_id)
                    if steps:
                        ctx["job"] = steps[0]["job"] or ctx["job"]
                        ctx["step"] = "; ".join(
                            f"{s['step']} ({s['conclusion']})" for s in steps[:4]
                        )
                except Exception:
                    pass
            if token and run.get("id") is not None:
                try:
                    from app.github import client as _ghc

                    anns = _ghc.check_annotations(
                        token=token, full_name=repo_name, check_run_id=run.get("id")
                    )
                    if anns:
                        ctx["annotations"] = "; ".join(
                            f"{a['path']}:{a['line'] or '?'} [{a['level']}] "
                            f"{a['message'][:200]}"
                            for a in anns[:5]
                        )[:2000]
                except Exception:
                    pass
            prs = run.get("pull_requests") or []
            if token and prs and prs[0].get("number"):
                try:
                    from app.github import client as _ghc

                    files = _ghc.pull_files(
                        token=token, full_name=repo_name, number=prs[0]["number"]
                    )
                    ctx["changed_files"] = ", ".join(files[:20])
                except Exception:
                    pass
    except Exception:
        pass
    return ctx


def build_ci_context_from_task(task) -> dict:
    """Worker-side enrichment for a CI task (best-effort, never raises).

    Reconstructs the same evidence fields the webhook used to fetch inline,
    but without blocking the HTTP acknowledgement path. The revision comes
    only from the task row (`ci_sha`), so enrichment can never change which
    revision is being repaired.
    """
    ctx: dict = {
        "provider": "github",
        "workflow_name": getattr(task, "ci_workflow", "") or "",
        "workflow_file": "",
        "workflow_content": "",
        "run_id": getattr(task, "ci_run_id", "") or "",
        "commit_sha": getattr(task, "ci_sha", "") or "",
        "branch": getattr(task, "ci_head_branch", "") or "",
        "base_branch": getattr(task, "ci_base_branch", "") or "",
        "pr_number": getattr(task, "ci_pr_number", None),
        "is_fork": bool(getattr(task, "ci_is_fork", 0)),
        "job": getattr(task, "ci_job", "") or "",
        "step": "",
        "exit_code": "",
        "failure_logs": "",
        "annotations": "",
        "changed_files": "",
        "url": getattr(task, "ci_url", "") or "",
        "deferred": False,
    }
    try:
        repo_name = getattr(task, "repository", "") or ""
        token = _repo_token(repo_name)
        if not token:
            return ctx
        run_id = ctx["run_id"]
        if run_id and not ctx["workflow_file"]:
            try:
                from app.github import client as _ghc

                wr = _ghc.get_workflow_run(
                    token=token, full_name=repo_name, run_id=run_id
                )
                ctx["workflow_name"] = ctx["workflow_name"] or wr.get("name", "")
                ctx["workflow_file"] = wr.get("path", "")
            except Exception:
                pass
        if ctx["workflow_file"] and ctx["commit_sha"]:
            try:
                from app.github import client as _ghc

                ctx["workflow_content"] = _ghc.get_workflow_content(
                    token=token,
                    full_name=repo_name,
                    path=ctx["workflow_file"],
                    ref=ctx["commit_sha"],
                )[:6000]
            except Exception:
                pass
        if run_id:
            try:
                steps = _gh_steps(token, repo_name, run_id)
                if steps:
                    # Preserve the exact identified job; only fill when absent.
                    ctx["job"] = ctx["job"] or steps[0]["job"]
                    ctx["step"] = "; ".join(
                        f"{s['step']} ({s['conclusion']})" for s in steps[:4]
                    )
            except Exception:
                pass
        if ctx["pr_number"]:
            try:
                from app.github import client as _ghc

                files = _ghc.pull_files(
                    token=token, full_name=repo_name, number=int(ctx["pr_number"])
                )
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
        return _ghc.pull_files(
            token=token, full_name=repo_name, number=prs[0]["number"]
        )
    except Exception:
        return []


def _csv(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _ignored(event: str, reason: str, **fields) -> dict:
    """Structured no-op response (visible in GitHub's Recent Deliveries body)."""
    log.info(
        "webhook ignored event=%s reason=%s %s",
        event,
        reason,
        " ".join(f"{k}={v!r}" for k, v in fields.items()),
    )
    out = {"ok": True, "ignored": reason}
    out.update({k: v for k, v in fields.items() if v not in (None, "")})
    return out


def _start_task(db: Session, task_id: int) -> dict:
    """Enqueue immediately and make a failure visible on the task.

    Triggered tasks always start. A cap/usage-limited task is backlog, not a
    failure: it records `QUEUE_WAITING` and the reconciler retries when capacity
    frees. Anything else (Redis down, worker missing) records `QUEUE_FAILED`
    plus `task.error` instead of silently sitting in QUEUED. Never raises.
    """
    try:
        from app.tasks import queue as _queue
    except Exception:
        out: dict = {"enqueued": False, "error": "queue module unavailable"}
    else:
        try:
            out = _queue.enqueue_task(task_id)
        except Exception as exc:  # defensive: enqueue_task promises never to raise
            out = {"enqueued": False, "error": str(exc)[:200]}
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if out.get("enqueued"):
            if task is not None:
                task.queue_job_id = str(out.get("job_id") or "")[:128]
                task.error = ""
            _event(
                db,
                task_id,
                "QUEUED",
                {"job": out.get("job_id", ""), "duplicate": bool(out.get("duplicate"))},
            )
        else:
            err = str(out.get("error") or "queue unavailable")[:1000]
            if out.get("limited"):
                if task is not None:
                    task.error = ""
                _event(db, task_id, "QUEUE_WAITING", {"reason": err[:300]})
            else:
                if task is not None:
                    task.error = f"not started: {err}"[:1000]
                _event(db, task_id, "QUEUE_FAILED", {"error": err[:300]})
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return out


def _ci_failure(run: dict) -> str:
    """The run's failure conclusion, or "" when this run is not a break."""
    allowed = {c.lower() for c in _csv(settings.CI_FAILURE_CONCLUSIONS)}
    conclusion = str(run.get("conclusion") or "").lower()
    return conclusion if conclusion in allowed else ""


def _event_enabled(event_type: str) -> bool:
    return event_type.lower() in {e.lower() for e in _csv(settings.CI_TRIGGER_EVENTS)}


def _branch_ignored(branch: str) -> bool:
    """FixHub-owned branches never create new tasks (the watcher repairs them)."""
    name = (branch or "").strip()
    if not name:
        return False
    prefixes = [p.rstrip("/") for p in _csv(settings.CI_IGNORE_BRANCH_PREFIXES)]
    return any(name == p or name.startswith(p + "/") for p in prefixes)


def _branch_allowed(branch: str, default_branch: str) -> bool:
    policy = (settings.CI_BRANCH_POLICY or "all").strip().lower()
    if policy == "default_only":
        return bool(branch) and branch == (default_branch or "main")
    if policy == "allowlist":
        return branch in set(_csv(settings.CI_BRANCH_ALLOWLIST))
    return True


def _workflow_allowed(name: str, path: str) -> bool:
    """Optional workflow allowlist (empty = every workflow)."""
    allowed = {a.lower() for a in _csv(settings.CI_WORKFLOW_ALLOWLIST)}
    if not allowed:
        return True
    candidates = {
        (name or "").lower(),
        (path or "").lower(),
        (path or "").rsplit("/", 1)[-1].lower(),
    }
    candidates |= {c.rsplit(".", 1)[0] for c in candidates if c}
    return bool(candidates & allowed)


def _check_run_allowed(run: dict) -> tuple[bool, str]:
    """Gate `check_run` delivery: opt-in event + GitHub Actions checks only."""
    if not _event_enabled("check_run"):
        return False, "check_run_disabled"
    if not settings.CI_ACTIONS_CHECKS_ONLY:
        return True, ""
    app = run.get("app") or {}
    slug = str(app.get("slug") or "").lower()
    if slug != "github-actions":
        return False, "non_actions_check"
    return True, ""


def _dedupe_key(repo_name: str, sha: str) -> str:
    return f"ci:{repo_name.strip().lower()}:{sha.strip()}"[:512]


def _issue_key(repo_name: str, number: int | None) -> str:
    return f"issue:{repo_name.strip().lower()}:{int(number or 0)}"[:512]


_ACTIVE_STATUSES = ("QUEUED", "RUNNING", "AWAITING_CI", "NEEDS_REVIEW")


def _existing_ci_task(db: Session, *, repo_name: str, sha: str, key: str) -> Task | None:
    """An ACTIVE task for this CI unit of work (repo+sha).

    Terminal tasks are history, not a permanent veto: a genuinely new failing
    run on the same revision must be able to create a new repair task. The
    legacy `ci_sha` guard covers pre-dedupe rows whose key is NULL.
    """
    dup = (
        db.query(Task)
        .filter(Task.dedupe_key == key, Task.status.in_(_ACTIVE_STATUSES))
        .first()
    )
    if dup is None:
        dup = _active_ci_task(db, repo_name=repo_name, sha=sha)
    return dup


def _insert_task(db: Session, task: Task, *, dedupe_key: str = "") -> tuple[Task, str]:
    """Insert a task; a racing twin returns the winner instead of a 500.

    Terminal history rows holding the same key are released first: the unique
    index enforces one ACTIVE task per key, not one task ever.
    """
    from sqlalchemy.exc import IntegrityError

    if dedupe_key:
        stale = (
            db.query(Task)
            .filter(
                Task.dedupe_key == dedupe_key,
                Task.status.notin_(_ACTIVE_STATUSES),
            )
            .all()
        )
        for row in stale:
            row.dedupe_key = None
        if stale:
            db.flush()
        existing = (
            db.query(Task)
            .filter(
                Task.dedupe_key == dedupe_key,
                Task.status.in_(_ACTIVE_STATUSES),
            )
            .first()
        )
        if existing is not None:
            return existing, "dedupe_key"
        task.dedupe_key = dedupe_key
    db.add(task)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if dedupe_key:
            existing = (
                db.query(Task)
                .filter(
                    Task.dedupe_key == dedupe_key,
                    Task.status.in_(_ACTIVE_STATUSES),
                )
                .first()
            )
            if existing is not None:
                return existing, "dedupe_key"
        raise
    db.refresh(task)
    return task, ""


def _active_ci_task(db: Session, *, repo_name: str, sha: str) -> Task | None:
    """An already-live CI task for the same repo+sha (legacy-row guard).

    New rows are deduped by `dedupe_key`; this also catches tasks created
    before the dedupe migration, whose key is NULL.
    """
    if not sha:
        return None
    return (
        db.query(Task)
        .filter(
            Task.repository == repo_name,
            Task.trigger_type == "ci",
            Task.status.in_(["QUEUED", "RUNNING", "AWAITING_CI", "NEEDS_REVIEW"]),
            Task.ci_sha == sha,
        )
        .order_by(Task.id.desc())
        .first()
    )


def _active_issue_task(
    db: Session, *, repo_name: str, number: int | None
) -> Task | None:
    """Phase 4: an already-live task for the same repo+issue (issue dedupe)."""
    if not number:
        return None
    return (
        db.query(Task)
        .filter(
            Task.repository == repo_name,
            Task.trigger_type == "issue",
            Task.issue_number == number,
            Task.status.in_(["QUEUED", "RUNNING", "AWAITING_CI", "NEEDS_REVIEW"]),
        )
        .order_by(Task.id.desc())
        .first()
    )


def _connected_repo(
    db: Session, *, repo_name: str, installation_id: str = ""
) -> tuple[Repository | None, str]:
    """The explicitly connected repository row, or ("", rejection reason).

    A GitHub App installation is account-wide: the App delivers webhooks for
    every repository it can see. Only `POST /api/repositories/connect` marks a
    repo as connected (`repositories.installation_id`). Unknown repos are
    never auto-created, and a payload installation that contradicts the stored
    one is rejected (stale/foreign delivery).
    """
    row = (
        db.query(Repository)
        .filter(Repository.github_full_name == repo_name)
        .first()
    )
    if row is None:
        return None, "repository_not_connected"
    stored = str(row.installation_id or "").strip()
    if not stored:
        return None, "repository_not_connected"
    payload_inst = str(installation_id or "").strip()
    if payload_inst and payload_inst != stored:
        return None, "installation_mismatch"
    return row, ""


def _trigger_repo(
    db: Session, *, repo_name: str, installation_id: str
) -> tuple[Repository | None, int | None, str]:
    """Resolve the connected repo + owning user, or a rejection reason."""
    repo, reason = _connected_repo(
        db, repo_name=repo_name, installation_id=installation_id
    )
    if repo is None:
        return None, None, reason
    owner_id = repo.owner_id or _resolve_owner_id(
        db, installation_id=installation_id, repo_name=repo_name
    )
    if owner_id is None:
        return None, None, "unconnected_installation"
    if repo.owner_id is None:
        repo.owner_id = owner_id
        db.commit()
    return repo, owner_id, ""


def _resolve_owner_id(
    db: Session, *, installation_id: str = "", repo_name: str = ""
) -> int | None:
    """Phase 2: derive the owning FixHub user from trusted server-side state.

    Prefers the explicit user->installation mapping (github_connections);
    falls back to the repository row's owner. Returns None when the
    installation/repository is not connected to any user — callers must then
    refuse to create an orphaned task. Never trusts client-supplied user IDs.
    """
    from app.db.models import GitHubConnection

    inst = str(installation_id or "").strip()
    if inst:
        conn = (
            db.query(GitHubConnection)
            .filter(GitHubConnection.installation_id == inst)
            .first()
        )
        if conn:
            return conn.user_id
    if repo_name:
        row = (
            db.query(Repository)
            .filter(Repository.github_full_name == repo_name)
            .first()
        )
        if row and row.owner_id is not None:
            return row.owner_id
    return None


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
    # Parse BEFORE touching the inbox: a malformed payload must not consume
    # the delivery id (GitHub's retry could never succeed, but an operator
    # replay of a corrected payload should not be blocked by a burned row).
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"error": "malformed payload"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "malformed payload"})

    db = SessionLocal()
    try:
        row = (
            db.query(WebhookDelivery)
            .filter(WebhookDelivery.delivery_id == x_github_delivery)
            .first()
        )
        if row is not None:
            if row.state == "received":
                # A previous attempt crashed after the durable commit but
                # before its effects landed: process it now (idempotent).
                return _process_delivery(db, row)
            return {"ok": True, "duplicate": True}
        # Durable trigger first. Nothing is processed until this row exists,
        # so a crash can never lose a signed delivery.
        row = WebhookDelivery(
            delivery_id=x_github_delivery,
            state="received",
            event_type=x_github_event,
            # Store the payload verbatim: slicing the JSON string would make
            # large-but-valid deliveries unparseable on replay (the column is
            # Text, so there is no need to truncate).
            payload=json.dumps(payload),
            received_at=_utcnow(),
        )
        db.add(row)
        try:
            db.commit()
        except Exception as exc:
            from sqlalchemy.exc import IntegrityError

            db.rollback()
            if isinstance(exc, IntegrityError):
                return {"ok": True, "duplicate": True}
            raise
        db.refresh(row)
        return _process_delivery(db, row)
    finally:
        db.close()


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _process_delivery(db: Session, row: WebhookDelivery) -> dict:
    """Apply one durable delivery; mark it processed only after effects commit.

    Handlers are idempotent (active-task dedupe + unique keys), so replaying a
    `received` row after a crash is safe. A handler exception leaves the row in
    `received` (surfaced as 500 so GitHub retries; the recovery sweep also
    retries it) instead of silently swallowing the trigger.
    """
    from fastapi.responses import JSONResponse

    try:
        payload = json.loads(row.payload or "{}")
    except (TypeError, ValueError):
        row.state = "rejected"
        row.error = "malformed stored payload"
        row.processed_at = _utcnow()
        db.commit()
        return {"ok": False, "error": "malformed payload"}
    event = row.event_type or ""
    delivery_id = row.delivery_id
    try:
        if event == "issues":
            out = _handle_issue(db, payload)
        elif event == "workflow_run":
            out = _handle_workflow_run(db, payload)
        elif event == "check_run":
            out = _handle_check_run(db, payload)
        else:
            out = {"ok": True, "ignored": event}
        ok = bool(out.get("ok", True))
        row.state = "processed" if ok else "rejected"
        row.error = "" if ok else str(out.get("error") or out.get("ignored") or "")[:1000]
        row.processed_at = _utcnow()
        db.commit()
        return out
    except Exception as exc:
        # Keep state=received so the retry path can finish the effects.
        try:
            db.rollback()
        except Exception:
            pass
        try:
            fresh = (
                db.query(WebhookDelivery)
                .filter(WebhookDelivery.delivery_id == delivery_id)
                .first()
            )
            if fresh is not None:
                fresh.error = str(exc)[:1000]
                db.commit()
        except Exception:
            db.rollback()
        log.exception("webhook delivery %s failed", delivery_id)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "delivery processing failed; retry"},
        )


def _prune_deliveries(db: Session) -> int:
    """Delete terminal inbox rows past the retention window. Never raises."""
    from datetime import timedelta

    from app.db.database import utcnow

    hours = max(1, int(getattr(settings, "WEBHOOK_DELIVERY_RETENTION_HOURS", 168)))
    cutoff = utcnow() - timedelta(hours=hours)
    try:
        removed = (
            db.query(WebhookDelivery)
            .filter(
                WebhookDelivery.state.in_(("processed", "rejected")),
                WebhookDelivery.created_at.isnot(None),
                WebhookDelivery.created_at < cutoff,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(removed or 0)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def reprocess_pending_deliveries(*, stale_s: int | None = None, limit: int = 20) -> dict:
    """Recovery sweep: re-apply deliveries stuck in `received`. Never raises."""
    from datetime import timedelta

    from app.db.database import utcnow

    grace = int(settings.WEBHOOK_DELIVERY_STALE_S if stale_s is None else stale_s)
    db = SessionLocal()
    try:
        cutoff = utcnow() - timedelta(seconds=max(0, grace))
        rows = (
            db.query(WebhookDelivery)
            .filter(
                WebhookDelivery.state == "received",
                WebhookDelivery.received_at.isnot(None),
                WebhookDelivery.received_at < cutoff,
            )
            .order_by(WebhookDelivery.id.asc())
            .limit(max(1, int(limit)))
            .all()
        )
        done = 0
        failed = 0
        for row in rows:
            try:
                out = _process_delivery(db, row)
                if isinstance(out, dict) and out.get("ok", True):
                    done += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
                try:
                    db.rollback()
                except Exception:
                    pass
        pruned = _prune_deliveries(db)
        return {
            "checked": len(rows),
            "processed": done,
            "failed": failed,
            "pruned": pruned,
        }
    except Exception as exc:
        return {"checked": 0, "processed": 0, "failed": 0, "error": str(exc)[:300]}
    finally:
        db.close()


def _handle_issue(db: Session, payload: dict) -> dict:
    action = payload.get("action")
    if action not in ("opened", "reopened"):
        return _ignored("issues", f"issues.{action}")
    labels = [l.get("name", "") for l in (payload.get("issue", {}).get("labels") or [])]
    # Optional label gate: if the repo uses `fixhub-fix`, only those issues trigger.
    # We do NOT require it — plain opened/reopened triggers by default.
    _ = labels
    repo_name = payload.get("repository", {}).get("full_name", "")
    if not repo_name:
        return {"ok": False, "error": "missing repository"}
    issue = payload.get("issue", {})
    installation_id = str((payload.get("installation") or {}).get("id", ""))
    repo, owner_id, reason = _trigger_repo(
        db, repo_name=repo_name, installation_id=installation_id
    )
    if repo is None:
        # Connected repos only: an account-wide App install is not consent.
        return _ignored("issues", reason, repository=repo_name)
    dup = _active_issue_task(db, repo_name=repo_name, number=issue.get("number"))
    if dup is not None:
        return {"ok": True, "task_id": dup.id, "duplicate": "active_task"}
    task = Task(
        repository=repo_name,
        repository_id=repo.id,
        owner_id=owner_id,
        trigger_type="issue",
        issue_number=issue.get("number"),
        issue_title=issue.get("title", "")[:500],
        issue_body=(issue.get("body") or "")[:8000],
        issue_url=issue.get("html_url", ""),
        status="QUEUED",
        active_issue_key=_issue_key(repo_name, issue.get("number")),
    )
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    db.add(task)
    try:
        db.commit()
    except _IntegrityError:
        # Concurrent delivery/UI request won the active-issue key: return it.
        db.rollback()
        dup = _active_issue_task(db, repo_name=repo_name, number=issue.get("number"))
        if dup is not None:
            return {"ok": True, "task_id": dup.id, "duplicate": "active_task"}
        raise
    db.refresh(task)
    _event(
        db, task.id, "TASK_CREATED", {"trigger": "issue", "issue": task.issue_number}
    )
    db.commit()
    queue = _start_task(db, task.id)
    out = {"ok": True, "task_id": task.id, "queued": bool(queue.get("enqueued"))}
    if not queue.get("enqueued"):
        out["error"] = str(queue.get("error") or "queue unavailable")[:300]
    return out


def _handle_workflow_run(db: Session, payload: dict) -> dict:
    action = payload.get("action")
    run = payload.get("workflow_run", {})
    if action != "completed":
        return _ignored("workflow_run", f"workflow_run.{action}")
    if not _event_enabled("workflow_run"):
        return _ignored("workflow_run", "event_not_enabled")
    conclusion = _ci_failure(run)
    if not conclusion:
        return _ignored(
            "workflow_run", f"workflow_run.{run.get('conclusion') or 'unknown'}"
        )
    repo_name = payload.get("repository", {}).get("full_name", "")
    if not repo_name:
        return {"ok": False, "error": "missing repository"}
    installation_id = str((payload.get("installation") or {}).get("id", ""))
    repo, owner_id, reason = _trigger_repo(
        db, repo_name=repo_name, installation_id=installation_id
    )
    if repo is None:
        return _ignored("workflow_run", reason, repository=repo_name)
    branch = str(run.get("head_branch") or "")
    if _branch_ignored(branch):
        return _ignored(
            "workflow_run", "fixhub_branch", repository=repo_name, branch=branch
        )
    if not _branch_allowed(branch, repo.default_branch):
        return _ignored(
            "workflow_run", "branch_not_allowed", repository=repo_name, branch=branch
        )
    workflow = str(run.get("name") or "")
    if not _workflow_allowed(workflow, str(run.get("path") or "")):
        return _ignored(
            "workflow_run", "workflow_not_allowed", repository=repo_name,
            workflow=workflow,
        )
    sha = str(run.get("head_sha") or "")
    if not sha:
        return _ignored("workflow_run", "missing_sha", repository=repo_name)
    # Source context: which branch/PR produced this failure. Never assume the
    # failing revision came from the default branch.
    prs = run.get("pull_requests") or []
    pr = prs[0] if prs else {}
    pr_head = (pr.get("head") or {}) if isinstance(pr, dict) else {}
    pr_base = (pr.get("base") or {}) if isinstance(pr, dict) else {}
    head_repo = (
        (pr_head.get("repo") or {}).get("full_name")
        or (run.get("head_repository") or {}).get("full_name")
        or repo_name
    )
    head_branch = str(pr_head.get("ref") or branch or "")
    base_branch = str(pr_base.get("ref") or repo.default_branch or "main")
    pr_number = pr.get("number") if isinstance(pr, dict) else None
    is_fork = 1 if str(head_repo).lower() != repo_name.lower() else 0
    key = _dedupe_key(repo_name, sha)
    dup = _existing_ci_task(db, repo_name=repo_name, sha=sha, key=key)
    if dup is not None:
        return {"ok": True, "task_id": dup.id, "duplicate": "dedupe_key"}
    base_excerpt = f"workflow {workflow} failed on {branch}"
    task = Task(
        repository=repo_name,
        repository_id=repo.id,
        owner_id=owner_id,
        trigger_type="ci",
        ci_run_id=str(run.get("id", "")),
        ci_sha=sha,
        ci_workflow=workflow,
        ci_url=run.get("html_url", ""),
        ci_head_branch=head_branch,
        ci_base_branch=base_branch,
        ci_pr_number=pr_number,
        ci_is_fork=is_fork,
        # Fast webhook: one-line summary only. The worker fetches the real
        # failing-log tail (network + log download) before the agent starts.
        ci_excerpt=base_excerpt,
        status="QUEUED",
    )
    task, duplicate = _insert_task(db, task, dedupe_key=key)
    if duplicate:
        return {"ok": True, "task_id": task.id, "duplicate": duplicate}
    _event(
        db,
        task.id,
        "TASK_CREATED",
        {"trigger": "ci", "run": task.ci_run_id, "conclusion": conclusion},
    )
    # Identifiers only: workflow content/logs are fetched worker-side so the
    # webhook never blocks on GitHub API calls.
    _event(
        db,
        task.id,
        "CI_CONTEXT_LOADED",
        {
            "provider": "github",
            "run_id": task.ci_run_id,
            "commit_sha": sha,
            "branch": head_branch,
            "base_branch": base_branch,
            "pr_number": pr_number,
            "is_fork": bool(is_fork),
            "job": task.ci_job,
            "workflow_name": workflow,
            "url": task.ci_url,
            "deferred": True,
        },
        limit=20000,
    )
    db.commit()
    queue = _start_task(db, task.id)
    out = {"ok": True, "task_id": task.id, "queued": bool(queue.get("enqueued"))}
    if not queue.get("enqueued"):
        out["error"] = str(queue.get("error") or "queue unavailable")[:300]
    return out


def _handle_check_run(db: Session, payload: dict) -> dict:
    if payload.get("action") != "completed":
        return _ignored("check_run", "check_run.not_completed")
    run = payload.get("check_run", {})
    conclusion = _ci_failure(run)
    if not conclusion:
        return _ignored("check_run", "check_run.not_failure")
    allowed, reason = _check_run_allowed(run)
    if not allowed:
        return _ignored("check_run", reason)
    repo_name = payload.get("repository", {}).get("full_name", "")
    if not repo_name:
        return {"ok": False, "error": "missing repository"}
    installation_id = str((payload.get("installation") or {}).get("id", ""))
    repo, owner_id, reason = _trigger_repo(
        db, repo_name=repo_name, installation_id=installation_id
    )
    if repo is None:
        return _ignored("check_run", reason, repository=repo_name)
    suite = run.get("check_suite") or {}
    branch = str(run.get("head_branch") or suite.get("head_branch") or "")
    if _branch_ignored(branch):
        return _ignored(
            "check_run", "fixhub_branch", repository=repo_name, branch=branch
        )
    if not _branch_allowed(branch, repo.default_branch):
        return _ignored(
            "check_run", "branch_not_allowed", repository=repo_name, branch=branch
        )
    sha = str(run.get("head_sha") or "")
    if not sha:
        return _ignored("check_run", "missing_sha", repository=repo_name)
    prs = run.get("pull_requests") or suite.get("pull_requests") or []
    pr = prs[0] if prs else {}
    pr_head = (pr.get("head") or {}) if isinstance(pr, dict) else {}
    pr_base = (pr.get("base") or {}) if isinstance(pr, dict) else {}
    head_repo = (
        (pr_head.get("repo") or {}).get("full_name")
        or (suite.get("head_repository") or {}).get("full_name")
        or repo_name
    )
    head_branch = str(pr_head.get("ref") or branch or "")
    base_branch = str(pr_base.get("ref") or repo.default_branch or "main")
    pr_number = pr.get("number") if isinstance(pr, dict) else None
    is_fork = 1 if str(head_repo).lower() != repo_name.lower() else 0
    key = _dedupe_key(repo_name, sha)
    dup = _existing_ci_task(db, repo_name=repo_name, sha=sha, key=key)
    if dup is not None:
        return {"ok": True, "task_id": dup.id, "duplicate": "dedupe_key"}
    job = str(run.get("name") or "")
    run_id = _run_id_from_url(run.get("html_url", ""))
    base_excerpt = f"check {job} failed"
    task = Task(
        repository=repo_name,
        repository_id=repo.id,
        owner_id=owner_id,
        trigger_type="ci",
        ci_run_id=run_id,
        ci_sha=sha,
        ci_job=job,
        ci_url=run.get("html_url", ""),
        ci_head_branch=head_branch,
        ci_base_branch=base_branch,
        ci_pr_number=pr_number,
        ci_is_fork=is_fork,
        # Fast webhook: one-line summary only. The worker fetches the real
        # failing-log tail before the agent starts.
        ci_excerpt=base_excerpt,
        status="QUEUED",
    )
    task, duplicate = _insert_task(db, task, dedupe_key=key)
    if duplicate:
        return {"ok": True, "task_id": task.id, "duplicate": duplicate}
    _event(
        db,
        task.id,
        "TASK_CREATED",
        {"trigger": "ci-check", "conclusion": conclusion},
    )
    _event(
        db,
        task.id,
        "CI_CONTEXT_LOADED",
        {
            "provider": "github",
            "run_id": run_id,
            "commit_sha": sha,
            "branch": head_branch,
            "base_branch": base_branch,
            "pr_number": pr_number,
            "is_fork": bool(is_fork),
            "job": job,
            "url": task.ci_url,
            "deferred": True,
        },
        limit=20000,
    )
    db.commit()
    queue = _start_task(db, task.id)
    out = {"ok": True, "task_id": task.id, "queued": bool(queue.get("enqueued"))}
    if not queue.get("enqueued"):
        out["error"] = str(queue.get("error") or "queue unavailable")[:300]
    return out
