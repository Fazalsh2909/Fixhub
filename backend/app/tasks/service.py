"""Task orchestration: workspace -> memory -> agent -> git -> PR.

FixHub owns git. Statuses: RUNNING | COMPLETED | FAILED | BLOCKED.
No hidden retries: one attempt, explicit POST /api/tasks/{id}/run to retry.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from app.agent import loop as _loop
from app.config import settings
from app.db.database import SessionLocal
from app.db.models import PullRequest, Repository, Task, TaskEvent
from app.github import app_auth as _app_auth
from app.github import publisher as _pub
from app.memory import store as _mem
from app.repo import workspace as _ws


def _event(db, task_id: int, type_: str, data: dict) -> None:
    db.add(TaskEvent(task_id=task_id, type=type_, data_json=json.dumps(data)[:4000]))


def _set(db, task: Task, **fields) -> None:
    for k, v in fields.items():
        setattr(task, k, v)
    task.updated_at = datetime.now(timezone.utc)


def _enrich_ci_logs(
    *, repository: str, installation_id: str, run_id: str, job_name: str, check_url: str
) -> str:
    """Fetch the failing-job log tail so the agent sees the exact error.

    Best-effort: returns "" on any failure (no token, no Actions read,
    network). Never raises. Runs worker-side so the webhook stays fast.
    """
    try:
        import re as _re

        from app.github import app_auth as _app_auth
        from app.github import client as _gh

        rid = (run_id or "").strip()
        if not rid:
            m = _re.search(r"/runs/(\d+)", check_url or "")
            rid = m.group(1) if m else ""
        if not rid or not installation_id:
            return ""
        token = _app_auth.installation_token(installation_id)
        tail = _gh.failed_log_tail(
            token=token, full_name=repository, run_id=rid, job_name=job_name or ""
        )
        return f"\n--- failing log tail ---\n{tail}" if tail else ""
    except Exception:
        return ""


def _destroy_vm_resources(task_id: int) -> None:
    """Phase 5: halt the task microVM + release jail/netns/overlay. Never raises.

    Called on EVERY terminal path (alongside workspace removal) so a worker
    interruption or lease loss can never leave an uncontrolled VM running.
    """
    try:
        from app.sandbox import firecracker as _fc

        _fc.destroy(int(task_id))
    except Exception:
        pass


def _cleanup(task_id: int) -> None:
    """Best-effort removal of a per-task workspace (fresh per-task guarantee).

    Skipped when WORKSPACE_KEEP=1 for debugging. Never raises.
    Phase 5: VM resources are ALWAYS destroyed even when the workspace is kept
    (a kept host directory must never imply a kept live VM).
    """
    try:
        _destroy_vm_resources(int(task_id))
    except Exception:
        pass
    if settings.WORKSPACE_KEEP:
        return
    try:
        _ws.destroy_workspace(task_id)
    except Exception:
        pass


def cleanup_task_workspace(task_id: int) -> bool:
    """Manual wipe for one task workspace. Returns True if something was removed."""
    import os

    try:
        _destroy_vm_resources(int(task_id))
    except Exception:
        pass
    path = _ws.workspace_path(task_id)
    existed = os.path.isdir(path)
    _ws.destroy_workspace(task_id)
    return existed


def _repo_token(db, task: Task) -> str:
    """Installation token for a task's repo, or "" when unavailable. Never raises."""
    try:
        repo = (
            db.query(Repository)
            .filter(Repository.github_full_name == task.repository)
            .first()
        )
        installation_id = repo.installation_id if repo else ""
        if not installation_id:
            return ""
        return _app_auth.installation_token(installation_id)
    except Exception:
        return ""


def _green_upstream_fix(*, token: str, full_name: str, changed: list[str]) -> str:
    """URL of an open GREEN fixhub PR touching the same files, else "".

    Publishing a competing rewrite when the fix already landed is how red
    PR #8 happened on top of green PR #7. With one standing branch, the
    overlapping PR is our own branch's PR from an earlier run. Never raises.
    """
    try:
        from app.github import client as _gh

        if not token or not changed:
            return ""
        want = set(changed)
        for pr in _gh.list_open_pulls(token=token, full_name=full_name):
            if not pr["head_branch"].startswith("fixhub-fixes"):
                continue
            try:
                files = set(
                    _gh.pull_files(
                        token=token, full_name=full_name, number=pr["number"]
                    )
                )
            except Exception:
                continue
            if not (want & files):
                continue
            try:
                if (
                    _gh.sha_check_conclusion(
                        token=token, full_name=full_name, sha=pr["head_sha"]
                    )
                    == "success"
                ):
                    return pr["url"]
            except Exception:
                continue
        return ""
    except Exception:
        return ""


def _resolve_auto_publish(auto_publish: bool | None) -> bool:
    if auto_publish is not None:
        return bool(auto_publish)
    try:
        return bool(settings.AUTO_PUBLISH)
    except Exception:
        return True


def _pending_title_body(task: Task, summary: str) -> tuple[str, str, str]:
    """Deterministic branch/title/body shared by run + approve + repair paths.

    ONE TASK = ONE BRANCH = ONE PR: the branch embeds the task id, so the
    initial run, gate fix rounds, CI repair rounds, and manual approve all
    resolve the identical branch and reuse the same PR.
    """
    from app.github import publisher as _pubmod

    if task.trigger_type == "ci":
        branch = _pubmod.branch_for_ci(task.ci_sha or "head", task.id)
        job = task.ci_workflow or task.ci_job or "ci"
        title = f"Fix CI failure ({job})"
        body = (
            f"Automated fix for CI failure.\n\nJob: {job}\nRun: {task.ci_url}\n"
            f"Commit: {task.ci_sha}"
        )
    else:
        branch = _pubmod.branch_for_issue(task.issue_number or 0, task.id)
        title = f"Fix #{task.issue_number}: {(task.issue_title or '')[:80]}"
        body = f"Automated fix for #{task.issue_number}.\n\n{(summary or '')[:2000]}"
    return branch, title, body


def _likely_paths(text: str) -> list[str]:
    """Repo-relative *.py paths mentioned in text (CI excerpts/logs).

    Used for the pre-agent stale check: if a green fixhub PR already touches
    these files, running a full agent loop would just burn tokens to redo it.
    """
    import re as _re

    paths = []
    for m in _re.finditer(r"[`'\"]?((?:[\w.\-]+/)+[\w.\-]+\.py)[`'\"]?", text or ""):
        p = m.group(1).lstrip("./")
        if p and p not in paths:
            paths.append(p)
    return paths[:20]


def _build_task_context(
    db, task: Task, *, path: str, default_branch: str, ci_info: str, overview: str
):
    """Authoritative TaskContext, established once per run.

    CI runs prefer the structured CI_CONTEXT_LOADED event captured at trigger
    time (workflow, job, step, logs, annotations, changed files); otherwise
    falls back to the legacy flat fields so old tasks keep working.
    """
    from app.agent import context as _ctxmod

    issue = _ctxmod.IssueContext(
        number=task.issue_number,
        title=task.issue_title or "",
        body=task.issue_body or "",
        url=task.issue_url or "",
    )
    ci = _ctxmod.CIContext()
    if task.trigger_type == "ci":
        row = (
            db.query(TaskEvent)
            .filter(TaskEvent.task_id == task.id, TaskEvent.type == "CI_CONTEXT_LOADED")
            .order_by(TaskEvent.id.desc())
            .first()
        )
        if row:
            try:
                data = json.loads(row.data_json or "{}")
            except ValueError:
                data = {}
            for field in (
                "provider",
                "workflow_name",
                "workflow_file",
                "workflow_content",
                "run_id",
                "commit_sha",
                "branch",
                "job",
                "step",
                "exit_code",
                "failure_logs",
                "annotations",
                "changed_files",
                "url",
            ):
                setattr(ci, field, str(data.get(field, ""))[:6000])
        if not ci.failure_logs:
            ci.failure_logs = ci_info[:6000]
        if not ci.commit_sha:
            ci.commit_sha = task.ci_sha or ""
        if not ci.job:
            ci.job = task.ci_job or task.ci_workflow or ""
        if not ci.url:
            ci.url = task.ci_url or ""
    # Phase 1: deterministic trigger -> skill routing (context + event, no migration).
    # Unknown/unsupported triggers raise UnknownSkill; the caller fails safe to BLOCKED.
    from app.agent.skills import registry as _skills

    skill = _skills.resolve_skill(task.trigger_type)
    try:
        skill_instructions = _skills.skill_prompt(skill)
    except Exception:
        skill_instructions = ""
    return _ctxmod.TaskContext(
        task_id=task.id,
        repository=task.repository,
        repository_id=task.repository_id,
        workspace_root=path,
        trigger_type=task.trigger_type,
        issue=issue,
        ci=ci,
        branch=task.branch or "",
        base_commit=_ws.head_sha(path),
        default_branch=default_branch,
        memory_overview=overview,
        skill=skill.name,
        skill_instructions=skill_instructions,
    )


def _relevant_memory(db, *, repository: str, task: Task) -> str:
    """Memory rows matching the failing area (paths/job/workflow), top 3.

    Returns "" when nothing matches. Never raises. Scoped retrieval keeps a
    stale generic overview from steering the agent toward already-fixed files.
    """
    try:
        query = " ".join(
            part
            for part in [
                task.ci_job or "",
                task.ci_workflow or "",
                " ".join(
                    _likely_paths(
                        f"{task.ci_excerpt}\n{task.ci_job}\n{task.ci_workflow}"
                    )
                ),
            ]
            if part
        )
        if not query.strip():
            return ""
        rows = _mem.search_memory(
            db, repository=repository, query=query, limit=3, owner_id=task.owner_id
        )
        if not rows:
            return ""
        lines = [
            f"Memory relevant to this failure ({r.path}): {r.summary[:600]}"
            for r in rows
        ]
        return "\n\n".join(lines)[:2000]
    except Exception:
        return ""


def _last_summary(db, task_id: int) -> str:
    row = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id, TaskEvent.type == "AGENT_FINISHED")
        .order_by(TaskEvent.id.desc())
        .first()
    )
    if not row:
        return ""
    try:
        return str(json.loads(row.data_json).get("summary", ""))[:2000]
    except Exception:
        return row.data_json[:2000]


def approve_task(task_id: int, *, title: str = "", body: str = "") -> dict:
    """Publish a NEEDS_REVIEW task (ReviewPanel Approve & Commit). Never raises."""
    import os

    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {"status": "FAILED", "error": "task not found"}
        if task.status not in ("NEEDS_REVIEW", "FAILED", "RUNNING"):
            if task.pr_url:
                return {"status": task.status, "pr": task.pr_url, "branch": task.branch}
        path = task.workspace
        if not path or not os.path.isdir(path):
            _set(db, task, status="FAILED", error="workspace expired (re-run the task)")
            _event(db, task.id, "FAILED", {"reason": "workspace_expired"})
            db.commit()
            return {"status": "FAILED", "error": task.error}
        try:
            if not _pub.has_meaningful_changes(path):
                _set(db, task, status="COMPLETED", error="")
                db.commit()
                _cleanup(task.id)
                return {"status": "COMPLETED", "no_changes": True}
        except _pub.PublishError as exc:
            _set(db, task, status="FAILED", error=str(exc)[:1000])
            db.commit()
            return {"status": "FAILED", "error": task.error}
        summary = _last_summary(db, task.id)
        branch, default_title, default_body = _pending_title_body(task, summary)
        out = _publish_task(
            db,
            task,
            path,
            branch=branch,
            summary=summary,
            title=title.strip() or default_title,
            body=body.strip() or default_body,
            check_lease=False,  # user-authorized approve, not a worker claim
        )
        # _publish_task commits and cleans up its own terminal states; a failed
        # approve needs an explicit re-run (workspace is wiped for freshness).
        return out
    finally:
        db.close()


def _publish_task(
    db,
    task: Task,
    path: str,
    *,
    branch: str,
    summary: str,
    title: str,
    body: str,
    check_lease: bool = True,
) -> dict:
    """Shared commit+push+PR. Caller commits. Returns result dict.

    check_lease (Phase 4.5 fence): worker-driven publishes verify live lease
    ownership before touching GitHub; user-driven approve calls pass False
    (NEEDS_REVIEW carries user authorization, never a worker lease).
    """
    # Collision guard: never silently reuse another active task's branch.
    # (Branch names embed the task id, so this fires only on genuine misuse.)
    clash = (
        db.query(Task)
        .filter(
            Task.repository == task.repository,
            Task.branch == branch,
            Task.id != task.id,
            Task.status.in_(["RUNNING", "AWAITING_CI", "NEEDS_REVIEW"]),
        )
        .first()
    )
    if clash is not None:
        _set(
            db,
            task,
            status="FAILED",
            error=f"branch collision: {branch} owned by active task #{clash.id}"[:1000],
        )
        _event(db, task.id, "FAILED", {"reason": "branch_collision", "owner": clash.id})
        db.commit()
        _cleanup(task.id)
        return {"status": "FAILED", "error": task.error}
    # Phase 4.5 lease fence: a worker that lost its lease (reaped while slow)
    # must not perform external side effects. Refresh + verify ownership now,
    # immediately before the first git mutation below. Skipped for explicit
    # user-driven approve publishes (no worker lease exists by design).
    if check_lease and not _holds_lease(db, task):
        _set(
            db,
            task,
            status="FAILED",
            error="lease lost before publish; not retrying side effects"[:1000],
        )
        _event(db, task.id, "FAILED", {"reason": "lease_lost_at_publish"})
        db.commit()
        _cleanup(task.id)
        return {"status": "FAILED", "error": task.error}
    repo = (
        db.query(Repository)
        .filter(Repository.github_full_name == task.repository)
        .first()
    )
    default_branch = repo.default_branch if repo else "main"
    installation_id = repo.installation_id if repo else ""
    token = ""
    if installation_id:
        try:
            token = _app_auth.installation_token(installation_id)
        except Exception:
            token = ""
    changed = _pub.changed_files(path)
    # New fix branches build on the failing commit when this is a CI task, so
    # the PR diff is scoped to the actual failure (not rebased onto main).
    start = task.ci_sha if task.trigger_type == "ci" else ""
    if not token:
        # Final hardening: re-fence the local push path too (time passed since
        # the entry check while resolving repo/token/changed files).
        if check_lease and not _holds_lease(db, task):
            return _fail_lease_lost(db, task, at="at publish (local push fence)")
        try:
            _local_commit_push(path, branch, title, start=start)
            sha = _ws.head_sha(path)
            _set(db, task, status="COMPLETED", branch=branch, commit_sha=sha, error="")
            _event(
                db,
                task.id,
                "COMMIT_CREATED",
                {"branch": branch, "sha": sha, "via": "approve"},
            )
            _update_memory(db, task, path, summary, changed=changed)
            db.commit()
            _cleanup(task.id)
            return {
                "status": "COMPLETED",
                "branch": branch,
                "commit": sha,
                "summary": summary,
            }
        except Exception as exc:
            _set(db, task, status="FAILED", error=f"git push failed: {exc}"[:1000])
            _event(db, task.id, "FAILED", {"reason": "push_failed"})
            db.commit()
            _cleanup(task.id)
            return {"status": "FAILED", "error": task.error}

    def _pub_lease() -> bool:
        # User-driven approve publishes carry no worker lease by design.
        return True if not check_lease else _holds_lease(db, task)

    try:
        pub = _pub.publish(
            path=path,
            full_name=task.repository,
            base=default_branch,
            title=title,
            body=body,
            token=token,
            branch=branch,
            start=start,
            lease_check=_pub_lease if check_lease else None,
        )
    except Exception as exc:
        msg = str(exc)
        if "lease lost" in msg.lower():
            return _fail_lease_lost(db, task, at="at publish (push/PR fence)")
        _set(db, task, status="FAILED", error=f"publish failed: {exc}"[:1000])
        _event(db, task.id, "FAILED", {"reason": "publish_failed"})
        db.commit()
        _cleanup(task.id)
        return {"status": "FAILED", "error": task.error}
    if pub.get("no_changes"):
        _set(db, task, status="COMPLETED", error="")
        db.commit()
        return {"status": "COMPLETED", "no_changes": True}
    _set(
        db,
        task,
        status="AWAITING_CI",
        branch=pub["branch"],
        commit_sha=pub["commit_sha"],
        pr_number=pub["pr_number"],
        pr_url=pub["pr_url"],
        error="",
    )
    _event(
        db,
        task.id,
        "COMMIT_CREATED",
        {"branch": pub["branch"], "sha": pub["commit_sha"]},
    )
    _event(db, task.id, "PR_CREATED", {"pr": pub["pr_number"], "url": pub["pr_url"]})
    _event(db, task.id, "AWAITING_CI", {"pr": pub["pr_url"], "sha": pub["commit_sha"]})
    # Phase 4.5: upsert (not blind insert) — a crash between PR creation and
    # DB persistence must reconcile, never duplicate, the PR row on recovery.
    existing_pr = db.query(PullRequest).filter(PullRequest.task_id == task.id).first()
    if existing_pr is not None:
        existing_pr.pr_number = pub["pr_number"] or 0
        existing_pr.pr_url = pub["pr_url"] or ""
        existing_pr.branch = pub["branch"]
        existing_pr.commit_sha = pub["commit_sha"]
    else:
        db.add(
            PullRequest(
                task_id=task.id,
                pr_number=pub["pr_number"] or 0,
                pr_url=pub["pr_url"] or "",
                branch=pub["branch"],
                commit_sha=pub["commit_sha"],
            )
        )
    _update_memory(db, task, path, summary, changed=changed)
    db.commit()
    _cleanup(task.id)
    return {"status": "AWAITING_CI", "branch": pub["branch"], "pr": pub["pr_url"]}


def _renew_lease(db, task_id: int) -> bool:
    """Phase 4.5 heartbeat: extend a live RUNNING lease. Returns False when
    the task is no longer ours (reaped/completed) — callers must stop work."""
    from datetime import timedelta

    from app.config import settings
    from app.db.database import utcnow

    now = utcnow()
    rows = (
        db.query(Task)
        .filter(
            Task.id == task_id,
            Task.status == "RUNNING",
            Task.lease_expires_at.isnot(None),
            Task.lease_expires_at >= now,
        )
        .update(
            {
                "lease_expires_at": now + timedelta(seconds=settings.TASK_LEASE_S),
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    return rows == 1


def _holds_lease(db, task: Task) -> bool:
    """Phase 4.5 fence: True only while OUR claim is the live lease holder.

    Checked immediately before external side effects (push/branch/PR). A
    worker that lost its lease (reaped, superseded) must not touch GitHub.
    """
    from app.db.database import utcnow

    try:
        db.refresh(task)
    except Exception:
        return False
    if task.status != "RUNNING":
        return False
    exp = task.lease_expires_at
    if exp is None:
        return True  # legacy/sync execution without a lease
    if exp.tzinfo is None:
        from datetime import timezone

        exp = exp.replace(tzinfo=timezone.utc)
    return exp >= utcnow() and bool(task.claimed_by)


def _cancel_flag(task_id: int):
    """Fresh-session poll of Task.cancel_requested for the agent loop."""

    def _check() -> bool:
        try:
            from app.db.database import SessionLocal as _SessionLocal

            s = _SessionLocal()
            try:
                row = s.query(Task.cancel_requested).filter(Task.id == task_id).first()
                return bool(row and row[0])
            finally:
                s.close()
        except Exception:
            return False

    return _check


# Phase 4: valid status transitions (concurrency correctness). Anything not
# listed is rejected by guards; terminal states never leave terminally.
_VALID_TRANSITIONS = {
    "QUEUED": {"RUNNING", "CANCELLED", "FAILED"},
    "RUNNING": {
        "COMPLETED",
        "FAILED",
        "BLOCKED",
        "NEEDS_REVIEW",
        "CANCELLED",
        "AWAITING_CI",
        "QUEUED",
    },
    "AWAITING_CI": {"COMPLETED", "RUNNING", "FAILED", "QUEUED"},
    "NEEDS_REVIEW": {"COMPLETED", "FAILED", "CANCELLED", "RUNNING"},
    "FAILED": {"RUNNING", "QUEUED"},
    "BLOCKED": {"RUNNING", "QUEUED"},
    "CANCELLED": set(),
    "COMPLETED": set(),
}


def cas_status(db, task_id: int, expect: set[str], new: str, **fields) -> bool:
    """Phase 4: atomic compare-and-set status transition. Returns True iff the
    single row moved. Exactly-one-winner claiming and duplicate suppression
    both reduce to this primitive (works on SQLite and PostgreSQL)."""
    from app.db.database import utcnow

    now = utcnow()
    values = {"status": new, "updated_at": now}
    values.update(fields)
    rows = (
        db.query(Task)
        .filter(Task.id == task_id, Task.status.in_(sorted(expect)))
        .update(values, synchronize_session=False)
    )
    db.commit()
    return rows == 1


def claim_task(
    db, task_id: int, *, worker_id: str, job_id: str = "", repair: bool = False
) -> Task | None:
    """Phase 4: atomically claim a task for execution (QUEUED -> RUNNING).

    Also adopts legacy RUNNING rows that hold no live lease (pre-Phase-4 and
    direct-DB test tasks), and AWAITING_CI rows for explicit repair rounds.
    A second concurrent claim sees RUNNING with a live lease and loses
    (returns None) — exactly one worker executes.
    """
    from datetime import timedelta

    from sqlalchemy import and_, or_

    from app.config import settings
    from app.db.database import utcnow

    now = utcnow()
    lease_until = now + timedelta(seconds=settings.TASK_LEASE_S)
    claimable = or_(
        Task.status == "QUEUED",
        and_(
            Task.status == "RUNNING",
            or_(Task.lease_expires_at.is_(None), Task.lease_expires_at < now),
        ),
    )
    if repair:
        claimable = or_(claimable, Task.status == "AWAITING_CI")
    rows = (
        db.query(Task)
        .filter(Task.id == task_id, claimable)
        .update(
            {
                "status": "RUNNING",
                "claimed_by": worker_id[:64],
                "claimed_at": now,
                "lease_expires_at": lease_until,
                "queue_job_id": (job_id or "")[:128],
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if rows != 1:
        db.rollback()
        return None
    db.expire_all()
    return db.query(Task).filter(Task.id == task_id).first()


class _LLMScope:
    """Phase 3 BYOK execution scope: holds one task's resolved credential.

    Enter to activate the runtime LLM client for agent execution; exit clears
    it. Re-enterable (gate-fix rounds) without re-decrypting. When `secret`
    is None the scope is a no-op and the development/test environment client
    applies. Never logged, never persisted.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str = "",
        secret: str | None = None,
    ):
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self._secret = secret
        self._exit = None

    def __enter__(self) -> "_LLMScope":
        from app.llm import client as _llm_client

        if self._secret:
            cm = _llm_client.use_runtime_credential(
                self.base_url, self._secret, self.model
            )
            cm.__enter__()
            self._exit = cm
        return self

    def __exit__(self, *exc_info) -> bool:
        if self._exit is not None:
            try:
                self._exit.__exit__(*exc_info)
            finally:
                self._exit = None
        return False


def _resolve_llm_scope(db, task: Task):
    """Phase 3: resolve the LLM credential for this task's owner.

    Returns an _LLMScope, or a {"error","reason"} dict when the task must
    fail fast (no credential / production ownerless). Ownerless tasks keep
    the environment client ONLY outside production (explicit dev/test path).
    """
    from app.llm import credentials as _creds

    if task.owner_id is None:
        if settings.ENV.strip().lower() == "prod":
            return {
                "error": "no owning user for this task; refusing environment LLM credential",
                "reason": "no_owner_in_prod",
            }
        return _LLMScope(provider="environment", model="")
    try:
        provider = _creds.default_provider_for_user(db, user_id=task.owner_id)
        if not provider:
            return {
                "error": "No LLM provider is configured for this account. "
                "Add one in Settings.",
                "reason": "no_credential",
            }
        base, secret, model = _creds.decrypt_for_runtime(
            db, user_id=task.owner_id, provider=provider
        )
    except Exception as exc:
        from app.llm import credentials as _credsmod

        if isinstance(exc, _credsmod.NoCredential):
            return {
                "error": "No LLM provider is configured for this account. "
                "Add one in Settings.",
                "reason": "no_credential",
            }
        return {
            "error": f"LLM credential unavailable: {exc}"[:500],
            "reason": "credential_error",
        }
    try:
        from app.sandbox import sandbox as _sandbox

        _sandbox.register_secret(secret)  # defense-in-depth redaction
    except Exception:
        pass
    return _LLMScope(provider=provider, model=model, base_url=base, secret=secret)


def _record_llm_usage(
    db,
    task: Task,
    *,
    provider: str,
    model: str,
    started,
    records: list[dict],
    tool_calls: int,
    success: bool,
    error_category: str = "",
) -> None:
    """Persist one safe usage row per agent execution. Never raises."""
    try:
        from app.db.database import utcnow
        from app.db.models import LLMUsage

        def _sum(key: str) -> tuple[int, int | None]:
            vals = [r.get(key) for r in records if r.get(key) is not None]
            return len(records), (sum(vals) if vals else None)

        _, total_in = _sum("input_tokens")
        _, total_out = _sum("output_tokens")
        _, total_all = _sum("total_tokens")
        db.add(
            LLMUsage(
                user_id=task.owner_id,
                task_id=task.id,
                provider=provider or "",
                model=model or "",
                started_at=started,
                completed_at=utcnow(),
                latency_ms=sum(int(r.get("latency_ms") or 0) for r in records),
                request_count=len(records),
                tool_calls=tool_calls,
                input_tokens=total_in,
                output_tokens=total_out,
                total_tokens=total_all,
                success=1 if success else 0,
                error_category=(error_category or "")[:64],
            )
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _lease_guard_for(db, task_id: int):
    """Final hardening: fresh-session lease ownership check for the agent loop.

    Uses a short-lived session (never the long-lived run session) so a reap
    by another worker/sweep is visible immediately, even mid-run.
    """

    def _check() -> bool:
        try:
            from app.db.database import SessionLocal as _SessionLocal
            from app.db.models import Task as _Task

            s = _SessionLocal()
            try:
                row = s.query(_Task).filter(_Task.id == task_id).first()
                if row is None or row.status != "RUNNING":
                    return False
                exp = row.lease_expires_at
                if exp is None:
                    return True  # legacy/sync execution without a lease
                if exp.tzinfo is None:
                    from datetime import timezone

                    exp = exp.replace(tzinfo=timezone.utc)
                from app.db.database import utcnow as _utcnow

                return exp >= _utcnow() and bool(row.claimed_by)
            finally:
                s.close()
        except Exception:
            return False

    return _check


def _run_agent_tracked(db, task: Task, scope: _LLMScope, **kwargs):
    """Run the agent loop once with usage tracking. Exceptions propagate.

    Final hardening: callers pass lease_guard (see _lease_guard_for); a
    lease_lost result is usage-tracked as a failure and returned for the
    caller to fail the task without side effects.
    """
    from app.db.database import utcnow
    from app.llm import client as _llm_client

    buf = _llm_client.start_usage_collection()
    started = utcnow()

    def _request_guard() -> str:
        # Fresh-session read so concurrent rounds see committed totals.
        try:
            from app.db.database import SessionLocal as _SessionLocal

            s = _SessionLocal()
            try:
                row = s.query(Task).filter(Task.id == task.id).first()
                if row is None:
                    return "task gone"
                return _usage_limit_reason(s, row, buf)
            finally:
                s.close()
        except Exception:
            return ""

    kwargs.setdefault("request_guard", _request_guard)
    try:
        result = _loop.run_agent(**kwargs)
    except Exception as exc:
        _record_llm_usage(
            db,
            task,
            provider=scope.provider,
            model=scope.model,
            started=started,
            records=_llm_client.stop_usage_collection(buf),
            tool_calls=0,
            success=False,
            error_category=type(exc).__name__[:64],
        )
        raise
    limited = bool(getattr(result, "usage_limited", False))
    _record_llm_usage(
        db,
        task,
        provider=scope.provider,
        model=scope.model,
        started=started,
        records=_llm_client.stop_usage_collection(buf),
        tool_calls=result.tool_calls,
        success=bool(result.finished)
        and not result.cancelled
        and not getattr(result, "lease_lost", False)
        and not limited,
        error_category=(
            "lease_lost"
            if getattr(result, "lease_lost", False)
            else (
                "usage_limit" if limited else ("cancelled" if result.cancelled else "")
            )
        ),
    )
    return result


def _fail_lease_lost(db, task: Task, *, at: str) -> dict:
    """Final hardening: terminal failure for a fenced worker. No side effects."""
    _set(
        db,
        task,
        status="FAILED",
        error=f"lease lost {at}; worker fenced before side effects"[:1000],
    )
    _event(db, task.id, "LEASE_LOST", {"at": at})
    db.commit()
    _cleanup(task.id)
    return {"status": "FAILED", "error": task.error, "lease_lost": True}


# --- Final hardening: per-user LLM usage limits --------------------------------
# Ownership-aware accounting keyed by (user_id, task_id) from the
# authenticated task owner. Never a global provider counter (BYOK safety:
# User A can never consume User B's budget). Token fields are nullable:
# providers that omit usage are NOT fabricated — non-token limits
# (requests, duration, concurrency, daily count) still bind.


def _task_request_total(db, task_id: int) -> int:
    """Committed LLM request count for one task (all rounds so far)."""
    from sqlalchemy import func as _func

    from app.db.models import LLMUsage

    total = (
        db.query(_func.sum(LLMUsage.request_count))
        .filter(LLMUsage.task_id == task_id)
        .scalar()
    )
    return int(total or 0)


def _task_token_totals(db, task_id: int) -> tuple[int | None, int | None]:
    """Committed (input, output) token sums for one task. None when the
    provider reported no token data (never fabricated)."""
    from sqlalchemy import func as _func

    from app.db.models import LLMUsage

    inp = (
        db.query(_func.sum(LLMUsage.input_tokens))
        .filter(LLMUsage.task_id == task_id)
        .scalar()
    )
    outp = (
        db.query(_func.sum(LLMUsage.output_tokens))
        .filter(LLMUsage.task_id == task_id)
        .scalar()
    )
    return (None if inp is None else int(inp), None if outp is None else int(outp))


def _daily_task_count(db, owner_id: int | None) -> int:
    """Tasks this owner created since UTC midnight (execution-budget basis)."""
    if owner_id is None:
        return 0
    from app.db.database import utcnow
    from app.db.models import Task as _Task

    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(_Task)
        .filter(_Task.owner_id == owner_id, _Task.created_at >= day_start)
        .count()
    )


def _usage_limit_reason(db, task: Task, buf: list[dict] | None = None) -> str:
    """Return "" (allowed) or a limit reason. Evaluated against the task's
    OWNER (task.owner_id), never provider name alone."""
    from app.config import settings as _settings

    max_req = int(_settings.MAX_LLM_REQUESTS_PER_TASK or 0)
    cap_in = int(_settings.MAX_INPUT_TOKENS_PER_TASK or 0)
    cap_out = int(_settings.MAX_OUTPUT_TOKENS_PER_TASK or 0)
    pending = len(buf or [])
    if max_req > 0 and _task_request_total(db, task.id) + pending >= max_req:
        return f"max LLM requests per task reached ({max_req})"
    if cap_in > 0 or cap_out > 0:
        committed_in, committed_out = _task_token_totals(db, task.id)
        buf_in = sum(
            int(r.get("input_tokens") or 0)
            for r in (buf or [])
            if r.get("input_tokens") is not None
        )
        buf_out = sum(
            int(r.get("output_tokens") or 0)
            for r in (buf or [])
            if r.get("output_tokens") is not None
        )
        # Only enforce when the provider actually reported tokens; missing
        # data never fabricates counts (non-token limits still bind).
        if cap_in > 0 and (committed_in is not None or buf_in):
            if (committed_in or 0) + buf_in >= cap_in:
                return f"max input tokens per task reached ({cap_in})"
        if cap_out > 0 and (committed_out is not None or buf_out):
            if (committed_out or 0) + buf_out >= cap_out:
                return f"max output tokens per task reached ({cap_out})"
    return ""


def _fail_usage_limit(db, task: Task, reason: str) -> dict:
    """Final hardening: terminal failure on budget exhaustion. Records a safe
    usage/limit event (owner + reason only, never secrets) and makes no
    further provider calls."""
    _set(db, task, status="FAILED", error=f"usage limit: {reason}"[:1000])
    _event(
        db,
        task.id,
        "USAGE_LIMIT_HIT",
        {"reason": reason[:200], "owner_id": task.owner_id},
    )
    db.commit()
    _cleanup(task.id)
    return {"status": "FAILED", "error": task.error, "usage_limited": True}


def run_task_inline(
    task_id: int,
    *,
    source: str = "",
    base: str = "main",
    auto_publish: bool | None = None,
    repair: bool = False,
    worker_id: str = "",
    job_id: str = "",
) -> dict:
    """Run one task synchronously. `source` overrides clone URL (tests).

    `repair=True` is a CI repair round: same task/branch/PR, workspace cloned
    at the standing fix branch, failure context from the last CI tail.

    Phase 4: the caller must hold the claim (QUEUED -> RUNNING via claim_task).
    When invoked without a prior claim (sync/dev path), this function claims
    with worker_id="sync" (or the given worker_id). A lost claim race returns
    {"status": ..., "already_claimed": True} with zero side effects.
    """
    db = SessionLocal()
    try:
        claimed = claim_task(
            db, task_id, worker_id=worker_id or "sync", job_id=job_id, repair=repair
        )
        if claimed is None:
            row = db.query(Task).filter(Task.id == task_id).first()
            if not row:
                return {"status": "FAILED", "error": "task not found"}
            return {"status": row.status, "already_claimed": True}
        task = claimed
        if task.cancel_requested:
            cas_status(db, task.id, {"RUNNING"}, "CANCELLED")
            _event(db, task.id, "CANCELLED", {"at": "claim"})
            db.commit()
            _cleanup(task.id)
            return {"status": "CANCELLED"}
        # Final hardening: pre-run budget check (defense in depth behind the
        # enqueue caps; closes the sync/inline path which bypasses the queue).
        if task.owner_id is not None:
            try:
                daily_max = int(settings.MAX_TASKS_PER_USER_PER_DAY or 0)
            except (TypeError, ValueError):
                daily_max = 0
            if daily_max > 0 and _daily_task_count(db, task.owner_id) > daily_max:
                return _fail_usage_limit(
                    db,
                    task,
                    f"daily task limit reached ({daily_max}/day)",
                )
        pre_reason = _usage_limit_reason(db, task)
        if pre_reason:
            return _fail_usage_limit(db, task, pre_reason)

        repo = (
            db.query(Repository)
            .filter(Repository.github_full_name == task.repository)
            .first()
        )
        default_branch = repo.default_branch if repo else base
        installation_id = repo.installation_id if repo else ""

        # --- pre-agent stale check (CI only): fail fast without spending a run ---
        if task.trigger_type == "ci":
            early = _green_upstream_fix(
                token=_repo_token(db, task),
                full_name=task.repository,
                changed=_likely_paths(
                    f"{task.ci_excerpt}\n{task.ci_job}\n{task.ci_workflow}"
                ),
            )
            if early:
                _set(db, task, status="COMPLETED", error="")
                _event(db, task.id, "SUPERSEDED", {"by": early, "stage": "pre_agent"})
                db.commit()
                return {"status": "COMPLETED", "superseded_by": early}

        _event(db, task.id, "AGENT_STARTED", {"trigger": task.trigger_type})
        db.commit()

        # --- workspace ---
        try:
            clone_src = source
            token = ""
            if not clone_src and installation_id:
                try:
                    token = _app_auth.installation_token(installation_id)
                    clone_src = _app_auth.clone_url_with_token(task.repository, token)
                except Exception as exc:
                    _set(db, task, status="BLOCKED", error=f"repo inaccessible: {exc}")
                    _event(
                        db, task.id, "BLOCKED", {"reason": "installation_token_failed"}
                    )
                    db.commit()
                    return {"status": "BLOCKED", "error": task.error}
            if not clone_src:
                _set(
                    db,
                    task,
                    status="BLOCKED",
                    error="no clone source (connect repo via GitHub App)",
                )
                _event(db, task.id, "BLOCKED", {"reason": "no_source"})
                db.commit()
                return {"status": "BLOCKED", "error": task.error}
            if repair:
                _event(
                    db,
                    task.id,
                    "CI_REPAIR_WORKSPACE",
                    {"attempt": task.ci_attempt_count or 0, "branch": _pub.FIX_BRANCH},
                )
                db.commit()
            if repair:
                # Same task => same branch: resume the task's own branch so a
                # repair round can never drift onto another task's branch.
                repair_branch = task.branch or _pending_title_body(task, "")[0]
            else:
                repair_branch = ""
            path = _ws.clone_repo(clone_src, task.id, branch=repair_branch)
            _set(db, task, workspace=path)
            # Phase 5: provision the task microVM (fail closed to BLOCKED).
            # Host clone keeps the token; the guest receives a tokenless copy
            # via provision()->sync_repo_to_guest(). No secrets cross vsock.
            try:
                from app.sandbox.backend import active_backend_name as _active_backend

                if _active_backend() == "firecracker":
                    from app.sandbox import firecracker as _fc

                    _fc.provision(task.id, path)
                    _event(db, task.id, "VM_PROVISIONED", {"backend": "firecracker"})
            except Exception as exc:
                reason = str(exc)[:500]
                _set(db, task, status="BLOCKED", error=f"sandbox unavailable: {reason}")
                _event(db, task.id, "BLOCKED", {"reason": "sandbox_unavailable"})
                _event(db, task.id, "SANDBOX_UNAVAILABLE", {"error": reason[:300]})
                db.commit()
                _cleanup(task.id)
                return {"status": "BLOCKED", "error": task.error}
            # CI tasks work from the exact failing commit, not the default
            # branch: failures living only on a PR branch would otherwise be
            # invisible to the agent.
            if task.trigger_type == "ci" and task.ci_sha and not repair:
                if _ws.checkout_ref(path, task.ci_sha):
                    _event(db, task.id, "CI_CHECKOUT", {"sha": task.ci_sha})
                else:
                    _event(
                        db,
                        task.id,
                        "CI_CHECKOUT",
                        {
                            "sha": task.ci_sha,
                            "fallback": "default-branch",
                            "reason": "sha unreachable (force-push/closed?)",
                        },
                    )
            db.commit()
        except Exception as exc:
            _set(
                db,
                task,
                status="BLOCKED",
                error=f"sandbox/workspace unavailable: {exc}",
            )
            _event(db, task.id, "BLOCKED", {"reason": "workspace_failed"})
            db.commit()
            _cleanup(task.id)
            return {"status": "BLOCKED", "error": task.error}

        # --- memory (prior knowledge) ---
        overview = _mem.get_repository_overview(
            db, repository=task.repository, owner_id=task.owner_id
        )
        if task.trigger_type == "ci":
            # Arbitration: a stale overview (e.g. describing already-fixed
            # issues) must not outrank the fresh CI failure. Scope retrieval
            # to the failing area and put it first; the generic overview stays
            # last as background only.
            scoped = _relevant_memory(
                db,
                repository=task.repository,
                task=task,
            )
            if scoped:
                overview = scoped + "\n\n---\n\n" + overview if overview else scoped
        ci_info = ""
        if task.trigger_type == "ci":
            ci_info = (
                f"workflow={task.ci_workflow} job={task.ci_job} sha={task.ci_sha} "
                f"run={task.ci_url}\nexcerpt:\n{task.ci_excerpt[:4000]}"
            )
            ci_info += _enrich_ci_logs(
                repository=task.repository,
                installation_id=installation_id,
                run_id=task.ci_run_id,
                job_name=task.ci_job or task.ci_workflow,
                check_url=task.ci_url,
            )

        # --- agent (tool events persisted live so progress is visible
        # immediately and survives a mid-run crash) ---
        # Phase 4.5 heartbeat: every tool call also renews the claim lease
        # (interval-capped via LEASE_RENEW_EVERY_S), so a slow-but-alive
        # worker is never reaped while it is actively executing.
        _last_renew = {"at": 0.0}

        lease_guard = _lease_guard_for(db, task.id)

        def _live(ev: dict) -> None:
            try:
                _event(db, task.id, "TOOL_CALL", ev)
                if ev.get("tool") in ("write_file", "edit_file"):
                    _event(db, task.id, "FILE_CHANGED", ev)
                if ev.get("tool") == "run_command":
                    _event(db, task.id, "COMMAND_RUN", ev)
                import time as _time

                from app.config import settings as _settings

                now_mono = _time.monotonic()
                # Final hardening: after any long-running tool/command,
                # renew/revalidate before continuing (not just on the
                # periodic heartbeat). Long = exceeded the cleanup grace.
                long_tool = (
                    ev.get("tool") == "run_command"
                    and isinstance(ev.get("duration_ms"), int)
                    and ev["duration_ms"] >= int(_settings.TOOL_CLEANUP_GRACE_S) * 1000
                ) or bool(ev.get("timed_out"))
                if long_tool or (
                    now_mono - _last_renew["at"] >= _settings.LEASE_RENEW_EVERY_S
                ):
                    ok = _renew_lease(db, task.id)
                    _last_renew["at"] = now_mono
                    _event(
                        db,
                        task.id,
                        "LEASE_RENEWED" if ok else "LEASE_LOST",
                        {
                            "at": "post_tool" if long_tool else "heartbeat",
                            "tool": ev.get("tool", ""),
                            "ok": ok,
                        },
                    )
                db.commit()
            except Exception:
                db.rollback()

        if repair:
            attempt = task.ci_attempt_count or 0
            ci_info = (
                f"CI REPAIR attempt {attempt}/3 for {task.pr_url or task.branch} "
                f"at commit {task.commit_sha}. The previous fix is already committed "
                f"on the standing branch; repair it in place on the SAME branch "
                f"(never open another one).\n"
                f"Fresh CI failure:\n{(task.last_ci_failure or task.ci_excerpt)[:6000]}\n\n"
                f"{ci_info}"
            )
        try:
            ctx = _build_task_context(
                db,
                task,
                path=path,
                default_branch=default_branch,
                ci_info=ci_info,
                overview=overview,
            )
        except Exception as exc:
            # Unknown/unsupported trigger: fail safe, never default to wrong skill.
            from app.agent.skills.registry import UnknownSkill as _UnknownSkill

            reason = (
                "unknown_skill" if isinstance(exc, _UnknownSkill) else "context_failed"
            )
            _set(
                db, task, status="BLOCKED", error=f"unsupported trigger ({exc})"[:1000]
            )
            _event(db, task.id, "BLOCKED", {"reason": reason, "error": str(exc)[:500]})
            db.commit()
            _cleanup(task.id)
            return {"status": "BLOCKED", "error": task.error}
        _event(
            db,
            task.id,
            "SKILL_SELECTED",
            {"skill": ctx.skill, "trigger": task.trigger_type},
        )
        if not ctx.skill_instructions:
            # Phase 4.5: the loader promises this event when skill docs are
            # missing (baseline prompt still runs, but degraded — audit it).
            _event(db, task.id, "SKILL_LOAD_FALLBACK", {"skill": ctx.skill})
        db.commit()
        # --- Phase 3 BYOK: resolve the task owner's LLM credential. The raw key
        # is decrypted here, registered for redaction, and scoped to this thread
        # for the agent execution only (never enters prompts, events, or logs).
        llm_scope = _resolve_llm_scope(db, task)
        if isinstance(llm_scope, dict):
            # Fail-fast BLOCKED payload (no credential / prod ownerless).
            _set(db, task, status="BLOCKED", error=llm_scope["error"])
            _event(db, task.id, "BLOCKED", {"reason": llm_scope["reason"]})
            db.commit()
            _cleanup(task.id)
            return {"status": "BLOCKED", "error": task.error}
        # Phase 4.5: one cumulative agent budget shared by the initial run and
        # gate-fix rounds (per-invocation LLM_MAX_RUNTIME_S still applies).
        # Coherence: per-run(900) <= budget(2100) < RQ kill(2400) < lease(3600).
        import time as _time

        agent_deadline = _time.monotonic() + settings.AGENT_BUDGET_S
        try:
            with llm_scope:
                result = _run_agent_tracked(
                    db,
                    task,
                    llm_scope,
                    workspace=path,
                    trigger_type=task.trigger_type,
                    repository=task.repository,
                    default_branch=default_branch,
                    issue_title=task.issue_title,
                    issue_body=task.issue_body,
                    ci_info=ci_info,
                    memory_overview=overview,
                    on_tool=_live,
                    is_cancelled=_cancel_flag(task.id),
                    ctx=ctx,
                    deadline_mono=agent_deadline,
                    lease_guard=lease_guard,
                )
                if getattr(result, "lease_lost", False):
                    return _fail_lease_lost(db, task, at="during agent run")
                if getattr(result, "usage_limited", False):
                    return _fail_usage_limit(
                        db,
                        task,
                        getattr(result, "usage_limit_reason", "")
                        or "per-task budget spent",
                    )
        except Exception as exc:
            from app.llm.client import LLMBlockedError

            if isinstance(exc, LLMBlockedError):
                _set(db, task, status="BLOCKED", error=str(exc)[:1000])
                _event(db, task.id, "BLOCKED", {"reason": "llm_unavailable"})
                db.commit()
                _cleanup(task.id)
                return {"status": task.status, "error": task.error}
            # Crash with uncommitted work (e.g. transient network drop after a
            # productive run): keep the workspace for review/resume instead of
            # wiping it.
            try:
                crashed_with_changes = _pub.has_meaningful_changes(path)
            except Exception:
                crashed_with_changes = False
            if crashed_with_changes:
                branch, _, _ = _pending_title_body(task, "")
                _set(
                    db,
                    task,
                    status="NEEDS_REVIEW",
                    branch=branch,
                    error=f"agent crashed mid-run, work preserved: {exc}"[:1000],
                )
                _event(
                    db,
                    task.id,
                    "NEEDS_REVIEW",
                    {"reason": "agent_crash", "branch": branch},
                )
                _update_memory(
                    db,
                    task,
                    path,
                    f"crashed mid-run: {exc}"[:800],
                    changed=_pub.changed_files(path),
                )
                db.commit()
                return {"status": "NEEDS_REVIEW", "branch": branch}
            _set(db, task, status="FAILED", error=f"agent crashed: {exc}"[:1000])
            _event(db, task.id, "FAILED", {"reason": "agent_crash"})
            db.commit()
            _cleanup(task.id)
            return {"status": task.status, "error": task.error}

        # Tool events were already persisted live via on_tool; only nudge
        # markers (emitted without on_tool... none — all go through _live)
        # could remain. AGENT_FINISHED is recorded once here.
        _event(db, task.id, "AGENT_FINISHED", {"summary": result.summary[:2000]})
        db.commit()

        if result.cancelled:
            _set(db, task, status="CANCELLED", error="cancelled by user")
            _event(db, task.id, "CANCELLED", {"at": "agent_run"})
            db.commit()
            _cleanup(task.id)
            return {"status": "CANCELLED"}

        if not result.finished:
            _set(db, task, status="FAILED", error=result.summary[:1000])
            _event(db, task.id, "FAILED", {"reason": result.summary[:500]})
            db.commit()
            _cleanup(task.id)
            return {"status": "FAILED", "error": task.error}

        # Phase 5: pull guest results back to the host workspace BEFORE any
        # git/gate/publish step. Publish stays on the trusted host; the guest
        # never holds a push-capable remote.
        try:
            from app.sandbox.backend import active_backend_name as _active_backend2

            if _active_backend2() == "firecracker":
                from app.sandbox import firecracker as _fc2

                synced = _fc2.sync_guest_to_host(task_id=task.id, workspace=path)
                _event(db, task.id, "VM_SYNCED", synced)
                db.commit()
        except Exception as exc:
            _set(db, task, status="FAILED", error=f"result sync failed: {exc}"[:1000])
            _event(db, task.id, "FAILED", {"reason": "vm_sync_failed"})
            db.commit()
            _cleanup(task.id)
            return {"status": "FAILED", "error": task.error}

        # --- git (FixHub owns it) ---
        try:
            if not _pub.has_meaningful_changes(path):
                _set(db, task, status="COMPLETED", error="")
                _event(db, task.id, "AGENT_FINISHED", {"result": "no changes produced"})
                _update_memory(db, task, path, result.summary, changed=[])
                db.commit()
                _cleanup(task.id)
                return {
                    "status": "COMPLETED",
                    "summary": result.summary,
                    "no_changes": True,
                }
        except _pub.PublishError as exc:
            _set(db, task, status="FAILED", error=str(exc)[:1000])
            _event(db, task.id, "FAILED", {"reason": "git_status_failed"})
            db.commit()
            _cleanup(task.id)
            return {"status": "FAILED", "error": task.error}

        changed = _pub.changed_files(path)
        branch, title, pr_body = _pending_title_body(task, result.summary)

        # --- stale-guard: a green fixhub PR already touching these files wins;
        # never publish a competing rewrite on top of a landed fix ---
        green = _green_upstream_fix(
            token=_repo_token(db, task), full_name=task.repository, changed=changed
        )
        if green:
            _set(db, task, status="COMPLETED", branch=branch, error="")
            _event(db, task.id, "SUPERSEDED", {"by": green})
            _update_memory(db, task, path, result.summary, changed=changed)
            db.commit()
            _cleanup(task.id)
            return {
                "status": "COMPLETED",
                "superseded_by": green,
                "summary": result.summary,
            }

        # --- local gate verification (pinned toolchain): fix red locally with
        # up to 2 follow-up rounds; never push knowingly-red ---
        from app.verify import gates as _gates

        gate_rounds = 0
        gate_names = _gates.detect_gates(path, changed)
        _event(db, task.id, "VALIDATION_STARTED", {"gates": gate_names})
        db.commit()
        gate_ok, gate_out = _gates.run_gates(path, changed)
        while not gate_ok and gate_rounds < 2:
            gate_rounds += 1
            _event(
                db,
                task.id,
                "VALIDATION_FAILED",
                {"round": gate_rounds, "output": gate_out[:1500]},
            )
            db.commit()
            with llm_scope:
                follow = _run_agent_tracked(
                    db,
                    task,
                    llm_scope,
                    workspace=path,
                    trigger_type=task.trigger_type,
                    repository=task.repository,
                    default_branch=default_branch,
                    issue_title=task.issue_title,
                    issue_body=task.issue_body,
                    ci_info=ci_info
                    + "\nLOCAL GATE FAILED — fix exactly this, then re-run the same gate commands:\n"
                    + gate_out,
                    memory_overview=overview,
                    on_tool=_live,
                    is_cancelled=_cancel_flag(task.id),
                    deadline_mono=agent_deadline,
                    lease_guard=lease_guard,
                )
                if getattr(follow, "lease_lost", False):
                    return _fail_lease_lost(db, task, at="during gate-fix round")
                if getattr(follow, "usage_limited", False):
                    return _fail_usage_limit(
                        db,
                        task,
                        getattr(follow, "usage_limit_reason", "")
                        or "per-task budget spent",
                    )
            _event(
                db,
                task.id,
                "AGENT_FINISHED",
                {"summary": follow.summary[:2000], "round": f"gate-{gate_rounds}"},
            )
            db.commit()
            if follow.cancelled:
                _set(db, task, status="CANCELLED", error="cancelled by user")
                _event(db, task.id, "CANCELLED", {"at": "gate_round"})
                db.commit()
                _cleanup(task.id)
                return {"status": "CANCELLED"}
            if not follow.finished:
                break
            result = follow
            # Phase 5: gate-fix rounds also execute in the guest; sync before
            # re-reading host git state.
            try:
                from app.sandbox.backend import active_backend_name as _ab3

                if _ab3() == "firecracker":
                    from app.sandbox import firecracker as _fc3

                    _fc3.sync_guest_to_host(task_id=task.id, workspace=path)
            except Exception as exc:
                _set(db, task, status="FAILED", error=f"result sync failed: {exc}"[:1000])
                _event(db, task.id, "FAILED", {"reason": "vm_sync_failed"})
                db.commit()
                _cleanup(task.id)
                return {"status": "FAILED", "error": task.error}
            changed = _pub.changed_files(path)
            branch, title, pr_body = _pending_title_body(task, result.summary)
            gate_names = _gates.detect_gates(path, changed)
            gate_ok, gate_out = _gates.run_gates(path, changed)
        if gate_ok:
            _event(db, task.id, "VALIDATION_PASSED", {"gates": gate_names})
            db.commit()
        if not gate_ok:
            _set(
                db,
                task,
                status="NEEDS_REVIEW",
                branch=branch,
                error=f"local gates failing after {gate_rounds} fix round(s): {gate_out[:500]}",
            )
            _event(
                db, task.id, "NEEDS_REVIEW", {"reason": "gate_failed", "branch": branch}
            )
            _update_memory(db, task, path, result.summary, changed=changed)
            db.commit()
            return {
                "status": "NEEDS_REVIEW",
                "branch": branch,
                "files": changed,
                "summary": result.summary,
                "gate": gate_out[:2000],
            }

        # --- review gate: stop here for IDE approval (no commit, keep workspace) ---
        if not _resolve_auto_publish(auto_publish):
            _set(db, task, status="NEEDS_REVIEW", branch=branch, error="")
            _event(
                db, task.id, "NEEDS_REVIEW", {"branch": branch, "files": changed[:20]}
            )
            _update_memory(db, task, path, result.summary, changed=changed)
            db.commit()
            return {
                "status": "NEEDS_REVIEW",
                "branch": branch,
                "files": changed,
                "summary": result.summary,
            }

        out = _publish_task(
            db,
            task,
            path,
            branch=branch,
            summary=result.summary,
            title=title,
            body=pr_body,
        )
        # _publish_task commits and cleans up its own terminal states.
        return out
    finally:
        db.close()


def _local_commit_push(path: str, branch: str, title: str, start: str = "") -> None:
    """Test/local mode: same standing-branch semantics without the GitHub API."""
    from app.github import publisher as _pubmod

    _pubmod.ensure_branch(path, branch=branch, start=start)

    def _run(args: list[str]) -> None:
        proc = subprocess.run(
            ["git", *args], cwd=path, capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout)[-500:]}"
            )

    _run(["add", "-A"])
    _pubmod._unstage_bytecode(path)
    _run(
        [
            "-c",
            "user.name=fixhub",
            "-c",
            "user.email=fixhub@fixhub.local",
            "commit",
            "-m",
            title,
        ]
    )
    _run(["push", "-u", "origin", branch])


def _update_memory(db, task: Task, path: str, summary: str, changed: list[str]) -> None:
    """Memory comes from ACTUAL interactions: changed files + agent summary. Never faked."""
    rev = _ws.head_sha(path)
    prev = _mem.get_repository_overview(
        db, repository=task.repository, owner_id=task.owner_id
    )
    delta = f"Task #{task.id} ({task.trigger_type}): {summary[:800]}"
    if changed:
        delta += f"\nChanged: {', '.join(changed[:20])}"
    merged = (prev + "\n" + delta).strip()[-4000:] if prev else delta[:4000]
    _mem.update_repository_memory(
        db,
        repository=task.repository,
        summary=merged,
        rev=rev or task.ci_sha,
        owner_id=task.owner_id,
    )
    for f in changed[:10]:
        _mem.save_memory(
            db,
            repository=task.repository,
            path=f,
            summary=f"touched by task #{task.id}: {summary[:300]}",
            rev=rev,
            owner_id=task.owner_id,
        )
