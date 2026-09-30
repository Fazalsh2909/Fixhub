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


def _enrich_ci_logs(*, repository: str, installation_id: str, run_id: str,
                     job_name: str, check_url: str) -> str:
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
        tail = _gh.failed_log_tail(token=token, full_name=repository, run_id=rid,
                                   job_name=job_name or "")
        return f"\n--- failing log tail ---\n{tail}" if tail else ""
    except Exception:
        return ""


def _cleanup(task_id: int) -> None:
    """Best-effort removal of a per-task workspace (fresh per-task guarantee).

    Skipped when WORKSPACE_KEEP=1 for debugging. Never raises.
    """
    if settings.WORKSPACE_KEEP:
        return
    try:
        _ws.destroy_workspace(task_id)
    except Exception:
        pass


def cleanup_task_workspace(task_id: int) -> bool:
    """Manual wipe for one task workspace. Returns True if something was removed."""
    import os

    path = _ws.workspace_path(task_id)
    existed = os.path.isdir(path)
    _ws.destroy_workspace(task_id)
    return existed


def _repo_token(db, task: Task) -> str:
    """Installation token for a task's repo, or "" when unavailable. Never raises."""
    try:
        repo = db.query(Repository).filter(Repository.github_full_name == task.repository).first()
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
                files = set(_gh.pull_files(token=token, full_name=full_name, number=pr["number"]))
            except Exception:
                continue
            if not (want & files):
                continue
            try:
                if _gh.sha_check_conclusion(token=token, full_name=full_name,
                                            sha=pr["head_sha"]) == "success":
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
        body = (f"Automated fix for CI failure.\n\nJob: {job}\nRun: {task.ci_url}\n"
                f"Commit: {task.ci_sha}")
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


def _build_task_context(db, task: Task, *, path: str, default_branch: str,
                        ci_info: str, overview: str):
    """Authoritative TaskContext, established once per run.

    CI runs prefer the structured CI_CONTEXT_LOADED event captured at trigger
    time (workflow, job, step, logs, annotations, changed files); otherwise
    falls back to the legacy flat fields so old tasks keep working.
    """
    from app.agent import context as _ctxmod

    issue = _ctxmod.IssueContext(
        number=task.issue_number, title=task.issue_title or "",
        body=task.issue_body or "", url=task.issue_url or "",
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
            for field in ("provider", "workflow_name", "workflow_file", "workflow_content",
                          "run_id", "commit_sha", "branch", "job", "step", "exit_code",
                          "failure_logs", "annotations", "changed_files", "url"):
                setattr(ci, field, str(data.get(field, ""))[:6000])
        if not ci.failure_logs:
            ci.failure_logs = ci_info[:6000]
        if not ci.commit_sha:
            ci.commit_sha = task.ci_sha or ""
        if not ci.job:
            ci.job = task.ci_job or task.ci_workflow or ""
        if not ci.url:
            ci.url = task.ci_url or ""
    return _ctxmod.TaskContext(
        task_id=task.id, repository=task.repository, repository_id=task.repository_id,
        workspace_root=path, trigger_type=task.trigger_type, issue=issue, ci=ci,
        branch=task.branch or "", base_commit=_ws.head_sha(path),
        default_branch=default_branch, memory_overview=overview,
    )


def _relevant_memory(db, *, repository: str, task: Task) -> str:
    """Memory rows matching the failing area (paths/job/workflow), top 3.

    Returns "" when nothing matches. Never raises. Scoped retrieval keeps a
    stale generic overview from steering the agent toward already-fixed files.
    """
    try:
        query = " ".join(
            part for part in [
                task.ci_job or "", task.ci_workflow or "",
                " ".join(_likely_paths(
                    f"{task.ci_excerpt}\n{task.ci_job}\n{task.ci_workflow}")),
            ] if part
        )
        if not query.strip():
            return ""
        rows = _mem.search_memory(db, repository=repository, query=query, limit=3)
        if not rows:
            return ""
        lines = [f"Memory relevant to this failure ({r.path}): {r.summary[:600]}"
                 for r in rows]
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
        out = _publish_task(db, task, path, branch=branch, summary=summary,
                            title=title.strip() or default_title, body=body.strip() or default_body)
        # _publish_task commits and cleans up its own terminal states; a failed
        # approve needs an explicit re-run (workspace is wiped for freshness).
        return out
    finally:
        db.close()


def _publish_task(db, task: Task, path: str, *, branch: str, summary: str, title: str, body: str) -> dict:
    """Shared commit+push+PR. Caller commits. Returns result dict."""
    # Collision guard: never silently reuse another active task's branch.
    # (Branch names embed the task id, so this fires only on genuine misuse.)
    clash = (
        db.query(Task)
        .filter(Task.repository == task.repository,
                Task.branch == branch,
                Task.id != task.id,
                Task.status.in_(["RUNNING", "AWAITING_CI", "NEEDS_REVIEW"]))
        .first()
    )
    if clash is not None:
        _set(db, task, status="FAILED",
             error=f"branch collision: {branch} owned by active task #{clash.id}"[:1000])
        _event(db, task.id, "FAILED", {"reason": "branch_collision", "owner": clash.id})
        db.commit()
        _cleanup(task.id)
        return {"status": "FAILED", "error": task.error}
    repo = db.query(Repository).filter(Repository.github_full_name == task.repository).first()
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
        try:
            _local_commit_push(path, branch, title, start=start)
            sha = _ws.head_sha(path)
            _set(db, task, status="COMPLETED", branch=branch, commit_sha=sha, error="")
            _event(db, task.id, "COMMIT_CREATED", {"branch": branch, "sha": sha, "via": "approve"})
            _update_memory(db, task, path, summary, changed=changed)
            db.commit()
            _cleanup(task.id)
            return {"status": "COMPLETED", "branch": branch, "commit": sha, "summary": summary}
        except Exception as exc:
            _set(db, task, status="FAILED", error=f"git push failed: {exc}"[:1000])
            _event(db, task.id, "FAILED", {"reason": "push_failed"})
            db.commit()
            _cleanup(task.id)
            return {"status": "FAILED", "error": task.error}
    try:
        pub = _pub.publish(
            path=path, full_name=task.repository, base=default_branch,
            title=title, body=body, token=token, branch=branch, start=start,
        )
    except Exception as exc:
        _set(db, task, status="FAILED", error=f"publish failed: {exc}"[:1000])
        _event(db, task.id, "FAILED", {"reason": "publish_failed"})
        db.commit()
        _cleanup(task.id)
        return {"status": "FAILED", "error": task.error}
    if pub.get("no_changes"):
        _set(db, task, status="COMPLETED", error="")
        return {"status": "COMPLETED", "no_changes": True}
    _set(db, task, status="AWAITING_CI", branch=pub["branch"], commit_sha=pub["commit_sha"],
         pr_number=pub["pr_number"], pr_url=pub["pr_url"], error="")
    _event(db, task.id, "COMMIT_CREATED", {"branch": pub["branch"], "sha": pub["commit_sha"]})
    _event(db, task.id, "PR_CREATED", {"pr": pub["pr_number"], "url": pub["pr_url"]})
    _event(db, task.id, "AWAITING_CI", {"pr": pub["pr_url"], "sha": pub["commit_sha"]})
    db.add(PullRequest(task_id=task.id, pr_number=pub["pr_number"], pr_url=pub["pr_url"],
                       branch=pub["branch"], commit_sha=pub["commit_sha"]))
    _update_memory(db, task, path, summary, changed=changed)
    db.commit()
    _cleanup(task.id)
    return {"status": "AWAITING_CI", "branch": pub["branch"], "pr": pub["pr_url"]}


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


def run_task_inline(task_id: int, *, source: str = "", base: str = "main",
                    auto_publish: bool | None = None, repair: bool = False) -> dict:
    """Run one task synchronously. `source` overrides clone URL (tests).

    `repair=True` is a CI repair round: same task/branch/PR, workspace cloned
    at the standing fix branch, failure context from the last CI tail.
    """
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {"status": "FAILED", "error": "task not found"}

        repo = db.query(Repository).filter(Repository.github_full_name == task.repository).first()
        default_branch = repo.default_branch if repo else base
        installation_id = repo.installation_id if repo else ""

        # --- pre-agent stale check (CI only): fail fast without spending a run ---
        if task.trigger_type == "ci":
            early = _green_upstream_fix(
                token=_repo_token(db, task),
                full_name=task.repository,
                changed=_likely_paths(f"{task.ci_excerpt}\n{task.ci_job}\n{task.ci_workflow}"),
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
                    _event(db, task.id, "BLOCKED", {"reason": "installation_token_failed"})
                    db.commit()
                    return {"status": "BLOCKED", "error": task.error}
            if not clone_src:
                _set(db, task, status="BLOCKED", error="no clone source (connect repo via GitHub App)")
                _event(db, task.id, "BLOCKED", {"reason": "no_source"})
                db.commit()
                return {"status": "BLOCKED", "error": task.error}
            if repair:
                _event(db, task.id, "CI_REPAIR_WORKSPACE",
                       {"attempt": task.ci_attempt_count or 0, "branch": _pub.FIX_BRANCH})
                db.commit()
            if repair:
                # Same task => same branch: resume the task's own branch so a
                # repair round can never drift onto another task's branch.
                repair_branch = task.branch or _pending_title_body(task, "")[0]
            else:
                repair_branch = ""
            path = _ws.clone_repo(clone_src, task.id, branch=repair_branch)
            _set(db, task, workspace=path)
            # CI tasks work from the exact failing commit, not the default
            # branch: failures living only on a PR branch would otherwise be
            # invisible to the agent.
            if task.trigger_type == "ci" and task.ci_sha and not repair:
                if _ws.checkout_ref(path, task.ci_sha):
                    _event(db, task.id, "CI_CHECKOUT", {"sha": task.ci_sha})
                else:
                    _event(db, task.id, "CI_CHECKOUT",
                           {"sha": task.ci_sha, "fallback": "default-branch",
                            "reason": "sha unreachable (force-push/closed?)"})
            db.commit()
        except Exception as exc:
            _set(db, task, status="BLOCKED", error=f"sandbox/workspace unavailable: {exc}")
            _event(db, task.id, "BLOCKED", {"reason": "workspace_failed"})
            db.commit()
            _cleanup(task.id)
            return {"status": "BLOCKED", "error": task.error}

        # --- memory (prior knowledge) ---
        overview = _mem.get_repository_overview(db, repository=task.repository)
        if task.trigger_type == "ci":
            # Arbitration: a stale overview (e.g. describing already-fixed
            # issues) must not outrank the fresh CI failure. Scope retrieval
            # to the failing area and put it first; the generic overview stays
            # last as background only.
            scoped = _relevant_memory(
                db, repository=task.repository, task=task,
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
                repository=task.repository, installation_id=installation_id,
                run_id=task.ci_run_id, job_name=task.ci_job or task.ci_workflow,
                check_url=task.ci_url,
            )

        # --- agent (tool events persisted live so progress is visible
        # immediately and survives a mid-run crash) ---
        def _live(ev: dict) -> None:
            try:
                _event(db, task.id, "TOOL_CALL", ev)
                if ev.get("tool") in ("write_file", "edit_file"):
                    _event(db, task.id, "FILE_CHANGED", ev)
                if ev.get("tool") == "run_command":
                    _event(db, task.id, "COMMAND_RUN", ev)
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
        ctx = _build_task_context(
            db, task, path=path, default_branch=default_branch,
            ci_info=ci_info, overview=overview,
        )
        try:
            result = _loop.run_agent(
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
                _set(db, task, status="NEEDS_REVIEW", branch=branch,
                     error=f"agent crashed mid-run, work preserved: {exc}"[:1000])
                _event(db, task.id, "NEEDS_REVIEW", {"reason": "agent_crash", "branch": branch})
                _update_memory(db, task, path, f"crashed mid-run: {exc}"[:800],
                               changed=_pub.changed_files(path))
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

        # --- git (FixHub owns it) ---
        try:
            if not _pub.has_meaningful_changes(path):
                _set(db, task, status="COMPLETED", error="")
                _event(db, task.id, "AGENT_FINISHED", {"result": "no changes produced"})
                _update_memory(db, task, path, result.summary, changed=[])
                db.commit()
                _cleanup(task.id)
                return {"status": "COMPLETED", "summary": result.summary, "no_changes": True}
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
        green = _green_upstream_fix(token=_repo_token(db, task), full_name=task.repository,
                                    changed=changed)
        if green:
            _set(db, task, status="COMPLETED", branch=branch, error="")
            _event(db, task.id, "SUPERSEDED", {"by": green})
            _update_memory(db, task, path, result.summary, changed=changed)
            db.commit()
            _cleanup(task.id)
            return {"status": "COMPLETED", "superseded_by": green, "summary": result.summary}

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
            _event(db, task.id, "VALIDATION_FAILED",
                   {"round": gate_rounds, "output": gate_out[:1500]})
            db.commit()
            follow = _loop.run_agent(
                workspace=path,
                trigger_type=task.trigger_type,
                repository=task.repository,
                default_branch=default_branch,
                issue_title=task.issue_title,
                issue_body=task.issue_body,
                ci_info=ci_info + "\nLOCAL GATE FAILED — fix exactly this, then re-run the same gate commands:\n" + gate_out,
                memory_overview=overview,
                on_tool=_live,
                is_cancelled=_cancel_flag(task.id),
            )
            _event(db, task.id, "AGENT_FINISHED", {"summary": follow.summary[:2000],
                                                   "round": f"gate-{gate_rounds}"})
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
            changed = _pub.changed_files(path)
            branch, title, pr_body = _pending_title_body(task, result.summary)
            gate_names = _gates.detect_gates(path, changed)
            gate_ok, gate_out = _gates.run_gates(path, changed)
        if gate_ok:
            _event(db, task.id, "VALIDATION_PASSED", {"gates": gate_names})
            db.commit()
        if not gate_ok:
            _set(db, task, status="NEEDS_REVIEW", branch=branch,
                 error=f"local gates failing after {gate_rounds} fix round(s): {gate_out[:500]}")
            _event(db, task.id, "NEEDS_REVIEW", {"reason": "gate_failed", "branch": branch})
            _update_memory(db, task, path, result.summary, changed=changed)
            db.commit()
            return {"status": "NEEDS_REVIEW", "branch": branch, "files": changed,
                    "summary": result.summary, "gate": gate_out[:2000]}

        # --- review gate: stop here for IDE approval (no commit, keep workspace) ---
        if not _resolve_auto_publish(auto_publish):
            _set(db, task, status="NEEDS_REVIEW", branch=branch, error="")
            _event(db, task.id, "NEEDS_REVIEW", {"branch": branch, "files": changed[:20]})
            _update_memory(db, task, path, result.summary, changed=changed)
            db.commit()
            return {"status": "NEEDS_REVIEW", "branch": branch, "files": changed,
                    "summary": result.summary}

        out = _publish_task(db, task, path, branch=branch, summary=result.summary,
                            title=title, body=pr_body)
        # _publish_task commits and cleans up its own terminal states.
        return out
    finally:
        db.close()


def _local_commit_push(path: str, branch: str, title: str, start: str = "") -> None:
    """Test/local mode: same standing-branch semantics without the GitHub API."""
    from app.github import publisher as _pubmod

    _pubmod.ensure_branch(path, branch=branch, start=start)

    def _run(args: list[str]) -> None:
        proc = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout)[-500:]}")

    _run(["add", "-A"])
    _pubmod._unstage_bytecode(path)
    _run(["-c", "user.name=fixhub", "-c", "user.email=fixhub@fixhub.local",
          "commit", "-m", title])
    _run(["push", "-u", "origin", branch])


def _update_memory(db, task: Task, path: str, summary: str, changed: list[str]) -> None:
    """Memory comes from ACTUAL interactions: changed files + agent summary. Never faked."""
    rev = _ws.head_sha(path)
    prev = _mem.get_repository_overview(db, repository=task.repository)
    delta = f"Task #{task.id} ({task.trigger_type}): {summary[:800]}"
    if changed:
        delta += f"\nChanged: {', '.join(changed[:20])}"
    merged = (prev + "\n" + delta).strip()[-4000:] if prev else delta[:4000]
    _mem.update_repository_memory(db, repository=task.repository, summary=merged, rev=rev or task.ci_sha)
    for f in changed[:10]:
        _mem.save_memory(db, repository=task.repository, path=f, summary=f"touched by task #{task.id}: {summary[:300]}", rev=rev)
