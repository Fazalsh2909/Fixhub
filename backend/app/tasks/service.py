"""Task orchestration: workspace -> agent -> git -> PR.

FixHub owns git. Statuses: RUNNING | COMPLETED | FAILED | BLOCKED.
No hidden retries: one attempt, explicit POST /api/tasks/{id}/run to retry.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

from app.agent import loop as _loop
from app.config import settings
from app.db.database import SessionLocal
from app.db.models import PullRequest, Repository, Task, TaskEvent
from app.github import app_auth as _app_auth
from app.github import publisher as _pub
from app.repo import workspace as _ws


def _event(db, task_id: int, type_: str, data: dict, *, limit: int = 4000) -> None:
    db.add(TaskEvent(task_id=task_id, type=type_, data_json=json.dumps(data)[:limit]))


_TERMINAL_STATUSES = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED"}


def _release_active_keys(task: Task) -> None:
    """Free dedupe identity on terminal states.

    One ACTIVE task per CI revision / issue; terminal tasks are history. This
    is what lets a genuinely new failing run (or an explicit retry) create a
    new task without weakening concurrent-delivery protection.
    """
    task.dedupe_key = None
    task.active_issue_key = None


def _active_keys_for(task: Task) -> dict:
    """Dedupe identity a retried task must re-take, as plain values.

    A terminal transition released the key; an explicit retry of the SAME task
    must take it back, otherwise a concurrent webhook could create a second
    active task for the same issue/revision (two branches, two PRs). The unique
    index remains the arbiter: a genuine conflict raises IntegrityError and the
    retry is refused instead of duplicating work.

    Returns values instead of mutating the ORM row: the caller passes them to
    `cas_status`, and mutating a loaded row first would let autoflush commit
    the keys even when the CAS loses.
    """
    repo = (task.repository or "").strip().lower()
    if str(task.trigger_type or "") == "ci" and task.ci_sha:
        return {
            "dedupe_key": f"ci:{repo}:{str(task.ci_sha).strip()}"[:512],
            "active_issue_key": None,
        }
    if str(task.trigger_type or "") == "issue" and task.issue_number:
        return {
            "active_issue_key": f"issue:{repo}:{int(task.issue_number)}"[:512],
            "dedupe_key": None,
        }
    return {
        "dedupe_key": task.dedupe_key,
        "active_issue_key": task.active_issue_key,
    }


def _is_file_change_event(ev: dict) -> bool:
    """True only for a write/edit that actually changed the workspace.

    Blocked (prohibited/gated) and rejected edits (e.g. "oldString not
    found") return ``ok=False``; they must not be reported as file changes.
    """
    return (
        ev.get("tool") in ("write_file", "edit_file")
        and bool(ev.get("ok"))
        and not ev.get("blocked")
    )


def _sha_matches(actual: str, expected: str) -> bool:
    """True when HEAD is the requested commit (full or abbreviated SHA)."""
    a = (actual or "").strip().lower()
    e = (expected or "").strip().lower()
    if not a or not e:
        return False
    return a == e or (len(e) >= 7 and a.startswith(e))


def _pin_ci_workspace(path: str, sha: str) -> dict:
    """Pin a CI workspace to the failing commit and verify HEAD.

    Must run before the guest is provisioned: provision() streams the host
    tree into the VM, so pinning afterwards leaves the agent reading the
    default branch. Returns {ok, sha, actual, reason}; never falls back to
    the default branch on an unreachable SHA.
    """
    wanted = (sha or "").strip()
    if not wanted or not os.path.isdir(path):
        return {
            "ok": False,
            "sha": wanted,
            "actual": "",
            "reason": "ci_sha_unreachable",
        }
    pinned = _ws.checkout_ref(path, wanted) or (
        _ws.fetch_ref(path, wanted) and _ws.checkout_ref(path, wanted)
    )
    actual = _ws.head_sha(path) if pinned else ""
    if not pinned:
        return {
            "ok": False,
            "sha": wanted,
            "actual": actual,
            "reason": "ci_sha_unreachable",
        }
    if not _sha_matches(actual, wanted):
        return {
            "ok": False,
            "sha": wanted,
            "actual": actual,
            "reason": "context_mismatch",
        }
    return {"ok": True, "sha": wanted, "actual": actual, "reason": ""}


def _set(db, task: Task, **fields) -> None:
    for k, v in fields.items():
        setattr(task, k, v)
    if str(fields.get("status") or "") in _TERMINAL_STATUSES:
        _release_active_keys(task)
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


def _ci_weakening_signal(path: str) -> tuple[bool, str]:
    """Diff-only check for a "fix" that disables required CI or deletes tests.

    Returns (workflow_edited, weakening_reason). Runs no repository commands
    (safe for arbitrary toolchains). Any workflow edit is surfaced as a
    warning; deleting a workflow, removing a test invocation, or stripping
    every run/uses step is a weakening signal that the gate may block.
    """
    try:
        out = _pub.diff_paths(path, ".github/workflows")
    except Exception:
        return False, ""
    if not out:
        return False, ""
    if "deleted file mode" in out:
        return True, "a workflow file was deleted"
    removed = [
        ln[1:].strip()
        for ln in out.splitlines()
        if ln.startswith("-") and not ln.startswith("---")
    ]
    added = [
        ln[1:].strip()
        for ln in out.splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    ]
    if not removed:
        return True, ""
    tokens = ("pytest", "npm test", "yarn test", "go test", "cargo test", "tox", "coverage")
    removed_tokens = {t for t in tokens if any(t in ln.lower() for ln in removed)}
    added_tokens = {t for t in tokens if any(t in ln.lower() for ln in added)}
    gone = sorted(removed_tokens - added_tokens)
    if gone:
        return True, "test invocation removed from workflow: " + ", ".join(gone)
    if any(ln.lower().startswith(("run:", "uses:")) for ln in removed) and not any(
        ln.lower().startswith(("run:", "uses:")) for ln in added
    ):
        return True, "workflow run/uses steps removed"
    return True, ""


def _no_changes_outcome(db, task: Task) -> dict:
    """Honest outcome when the agent produced no diff.

    Issue tasks: nothing to change can be legitimate completion. CI tasks:
    COMPLETED only when the failing revision is observably green; otherwise
    NEEDS_REVIEW with an explicit reason — never a false "repair succeeded".
    """
    if task.trigger_type != "ci":
        _set(db, task, status="COMPLETED", error="")
        db.commit()
        _cleanup(task.id)
        return {"status": "COMPLETED", "no_changes": True}
    conclusion = ""
    try:
        from app.github import client as _gh

        token = _repo_token(db, task)
        if token and task.ci_sha:
            conclusion = _gh.sha_check_conclusion(
                token=token, full_name=task.repository, sha=task.ci_sha
            )
    except Exception:
        conclusion = ""
    if conclusion == "success":
        _set(db, task, status="COMPLETED", error="")
        _event(db, task.id, "CI_ALREADY_GREEN", {"sha": task.ci_sha})
        db.commit()
        _cleanup(task.id)
        return {"status": "COMPLETED", "no_changes": True, "ci": "success"}
    reason = (
        "no changes produced but CI is not green "
        f"(conclusion={conclusion or 'unknown'})"
    )
    _set(db, task, status="NEEDS_REVIEW", error=reason[:1000])
    _event(
        db,
        task.id,
        "NEEDS_REVIEW",
        {"reason": "no_changes_ci_red", "conclusion": conclusion},
    )
    db.commit()
    return {"status": "NEEDS_REVIEW", "error": task.error, "no_changes": True}


def _ci_publish_target(task: Task, default_branch: str, fix_branch: str) -> dict:
    """Decide where a CI fix lands (never blindly against the default branch).

    - Same-repository PR failure on a non-default head branch: append the fix
      to that PR's head branch and reuse the PR (`repair_pr=True`).
    - Fork PR failure: never push to the fork; publish the task's own fix
      branch against the base repo's default branch (the caller adds a note).
    - Legacy rows without PR context: keep the default-branch behavior.
    """
    head = (task.ci_head_branch or "").strip()
    base = (task.ci_base_branch or "").strip()
    repair_pr = bool(
        task.trigger_type == "ci"
        and not int(task.ci_is_fork or 0)
        and task.ci_pr_number
        and head
        and head != default_branch
    )
    return {
        "repair_pr": repair_pr,
        "branch": head if repair_pr else fix_branch,
        "base": (base or default_branch) if repair_pr else default_branch,
    }


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
    db, task: Task, *, path: str, default_branch: str, ci_info: str
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
            # Keep the diagnostic tail: a head-only slice cut the failing
            # lines (and therefore the PRIMARY FAILURE SIGNAL) out of the log.
            ci.failure_logs = _ctxmod.clip_logs(ci_info)
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
        skill=skill.name,
        skill_instructions=skill_instructions,
    )


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
                return _no_changes_outcome(db, task)
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
    # --- CI source targeting: never assume the failure came from `main` ---
    # Same-repository PR failure -> append the fix to the originating PR's head
    # branch and reuse that PR. Fork PR failure -> never push to the fork; open
    # a fix PR against the base repo's default branch with an explicit note.
    target = _ci_publish_target(task, default_branch, branch)
    repair_pr = bool(target["repair_pr"])
    publish_branch = str(target["branch"])
    pr_base = str(target["base"])
    if task.trigger_type == "ci" and int(task.ci_is_fork or 0) and task.ci_pr_number:
        body = (
            f"{body}\n\n> Note: the failing CI run came from fork PR "
            f"#{task.ci_pr_number}. FixHub never pushes to forks, so this fix "
            f"targets `{default_branch}`."
        )
    # --- CI weakening gate (diff-only; runs no repo commands) ---
    gate = str(getattr(settings, "CI_WEAKENING_GATE", "block") or "block").lower()
    if task.trigger_type == "ci" and gate != "off":
        edited, weaken = _ci_weakening_signal(path)
        if edited:
            _event(
                db,
                task.id,
                "CI_WEAKENING_WARNING" if weaken else "CI_WORKFLOW_EDITED",
                {"reason": weaken[:500], "gate": gate},
            )
            db.commit()
            if weaken and gate == "block":
                _set(
                    db,
                    task,
                    status="BLOCKED",
                    error=f"ci_weakening_detected: {weaken}"[:1000],
                )
                _event(
                    db,
                    task.id,
                    "BLOCKED",
                    {"reason": "ci_weakening_detected", "detail": weaken[:300]},
                )
                db.commit()
                _cleanup(task.id)
                return {"status": "BLOCKED", "error": task.error}
    if not token:
        # Final hardening: re-fence the local push path too (time passed since
        # the entry check while resolving repo/token/changed files).
        if check_lease and not _holds_lease(db, task):
            return _fail_lease_lost(db, task, at="at publish (local push fence)")
        try:
            _local_commit_push(path, publish_branch, title, start=start)
            sha = _ws.head_sha(path)
            _set(
                db,
                task,
                status="COMPLETED",
                branch=publish_branch,
                commit_sha=sha,
                error="",
            )
            _event(
                db,
                task.id,
                "COMMIT_CREATED",
                {"branch": publish_branch, "sha": sha, "via": "approve"},
            )
            db.commit()
            _cleanup(task.id)
            return {
                "status": "COMPLETED",
                "branch": publish_branch,
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
        if repair_pr:
            pub = _pub.publish_to_existing_branch(
                path=path,
                full_name=task.repository,
                branch=publish_branch,
                base=pr_base,
                title=title,
                body=body,
                token=token,
                lease_check=_pub_lease if check_lease else None,
            )
        else:
            pub = _pub.publish(
                path=path,
                full_name=task.repository,
                base=pr_base,
                title=title,
                body=body,
                token=token,
                branch=publish_branch,
                start=start,
                lease_check=_pub_lease if check_lease else None,
            )
    except Exception as exc:
        msg = str(exc)
        if "lease lost" in msg.lower():
            return _fail_lease_lost(db, task, at="at publish (push/PR fence)")
        if "originating PR is no longer open" in msg:
            _set(
                db,
                task,
                status="BLOCKED",
                error=f"originating_pr_unavailable: {exc}"[:1000],
            )
            _event(
                db,
                task.id,
                "BLOCKED",
                {"reason": "originating_pr_unavailable", "pr": task.ci_pr_number},
            )
            db.commit()
            _cleanup(task.id)
            return {"status": "BLOCKED", "error": task.error}
        _set(db, task, status="FAILED", error=f"publish failed: {exc}"[:1000])
        _event(db, task.id, "FAILED", {"reason": "publish_failed"})
        db.commit()
        _cleanup(task.id)
        return {"status": "FAILED", "error": task.error}
    if pub.get("no_changes"):
        return _no_changes_outcome(db, task)
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
    both reduce to this primitive (works on SQLite and PostgreSQL).

    Terminal transitions also release the task's active dedupe identity, so a
    new event for the same issue/revision can create a new task.
    """
    from app.db.database import utcnow

    now = utcnow()
    values = {"status": new, "updated_at": now}
    if new in _TERMINAL_STATUSES:
        values["dedupe_key"] = None
        values["active_issue_key"] = None
    values.update(fields)
    rows = (
        db.query(Task)
        .filter(Task.id == task_id, Task.status.in_(sorted(expect)))
        .update(values, synchronize_session=False)
    )
    db.commit()
    return rows == 1


def _capacity_reason(db, task: Task) -> str:
    """Global/user/repo live-capacity check (0 = unlimited).

    Mirrors the enqueue-time fast check but is called inside the claim
    transaction (serialized by a PostgreSQL advisory lock) so two API
    instances can never oversubscribe the caps. A RUNNING row whose lease has
    already expired is reclaimable and does not hold a slot.
    """
    from sqlalchemy import or_

    from app.db.database import utcnow

    now = utcnow()
    live = ["RUNNING", "AWAITING_CI"]
    base = db.query(Task).filter(Task.status.in_(live), Task.id != task.id)
    base = base.filter(
        or_(
            Task.status != "RUNNING",
            Task.lease_expires_at.is_(None),
            Task.lease_expires_at >= now,
        )
    )
    if settings.MAX_RUNNING_TASKS_GLOBAL > 0:
        if base.count() >= settings.MAX_RUNNING_TASKS_GLOBAL:
            return "global concurrency limit reached"
    if task.owner_id is not None and settings.MAX_RUNNING_PER_USER > 0:
        n = base.filter(Task.owner_id == task.owner_id).count()
        if n >= settings.MAX_RUNNING_PER_USER:
            return "per-user concurrency limit reached"
    if settings.MAX_RUNNING_PER_REPO > 0:
        n = base.filter(Task.repository == task.repository).count()
        if n >= settings.MAX_RUNNING_PER_REPO:
            return "per-repository concurrency limit reached"
    return ""


def claim_task(
    db, task_id: int, *, worker_id: str, job_id: str = "", repair: bool = False
) -> Task | None:
    """Phase 4: atomically claim a task for execution (QUEUED -> RUNNING).

    Also adopts legacy RUNNING rows that hold no live lease (pre-Phase-4 and
    direct-DB test tasks), and AWAITING_CI rows for explicit repair rounds.
    A second concurrent claim sees RUNNING with a live lease and loses
    (returns None) — exactly one worker executes.

    Capacity is reserved inside this transaction: a claim that would exceed a
    global/user/repo cap returns None and leaves the row QUEUED (eligible for
    a later reconciler retry), never FAILED and never oversubscribed.
    """
    from datetime import timedelta

    from sqlalchemy import and_, or_, text

    from app.config import settings
    from app.db.database import utcnow

    now = utcnow()
    row = db.query(Task).filter(Task.id == task_id).first()
    if row is None:
        return None
    # Serialize capacity reservation across workers/API instances. The lock is
    # transaction-scoped; the claim commit (or rollback) releases it.
    try:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(6842101)"))
    except Exception:
        pass
    if _capacity_reason(db, row):
        db.rollback()
        return None
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
    it. Re-enterable without re-decrypting. When `secret`
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
        elif self.model:
            # CI model override on the environment credential path: keep the
            # env provider/key but scope a stronger model to this task only.
            try:
                from app.config import active_llm

                base_url, api_key, _env_model = active_llm()
                if api_key:
                    cm = _llm_client.use_runtime_credential(
                        base_url, api_key, self.model
                    )
                    cm.__enter__()
                    self._exit = cm
            except Exception:
                self._exit = None
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

    _ci_model = (
        (settings.LLM_CI_MODEL or "").strip()
        if (task.trigger_type or "") == "ci"
        else ""
    )

    if task.owner_id is None:
        if settings.ENV.strip().lower() == "prod":
            return {
                "error": "no owning user for this task; refusing environment LLM credential",
                "reason": "no_owner_in_prod",
            }
        return _LLMScope(provider="environment", model=_ci_model)
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
    return _LLMScope(
        provider=provider,
        model=_ci_model or model,
        base_url=base,
        secret=secret,
    )


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
        # Fail CLOSED: if the budget cannot be verified, no further paid
        # provider request is allowed. (The lease guard already behaves this
        # way; treating an unverifiable budget as permission was the bug.)
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
        except Exception as exc:
            return (
                "budget check failed "
                f"({type(exc).__name__}); refusing further provider calls"
            )

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


def _binding_limits(task: Task) -> tuple[int, int, int]:
    """Effective (max_requests, max_input_tokens, max_output_tokens) for a task.

    CI tasks get their own request/token caps so a repair (initial fix plus
    gate-repair rounds) is bounded as one budget, regardless of a larger
    legacy global value in the deployment .env. 0 means "no limit" for the
    token caps; request caps are always positive in production (validated at
    startup).
    """
    from app.config import settings as _settings

    max_req = int(_settings.MAX_LLM_REQUESTS_PER_TASK or 0)
    cap_in = int(_settings.MAX_INPUT_TOKENS_PER_TASK or 0)
    if task.trigger_type == "ci":
        ci_max = int(getattr(_settings, "LLM_CI_MAX_REQUESTS_PER_TASK", 30) or 30)
        max_req = min(max_req, ci_max) if max_req > 0 else ci_max
        # CI override: a repair needs room for ~30 stateless requests. The old
        # 100k global cap fired before the first edit in production.
        ci_tok = int(getattr(_settings, "MAX_INPUT_TOKENS_PER_CI_TASK", 0) or 0)
        if ci_tok > 0:
            cap_in = ci_tok
    cap_out = int(_settings.MAX_OUTPUT_TOKENS_PER_TASK or 0)
    return max_req, cap_in, cap_out


def _usage_limit_reason(db, task: Task, buf: list[dict] | None = None) -> str:
    """Return "" (allowed) or a limit reason. Evaluated against the task's
    OWNER (task.owner_id), never provider name alone."""
    max_req, cap_in, cap_out = _binding_limits(task)
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


def _usage_fraction(db, task: Task, buf: list[dict] | None = None) -> float:
    """0..1 fraction of the binding task budget already consumed.

    Advisory only: it drives the loop's converge nudge. The hard stop remains
    ``_usage_limit_reason``. Returns 0 when no budget is configured.
    """
    max_req, cap_in, _ = _binding_limits(task)
    frac = 0.0
    if max_req > 0:
        frac = max(
            frac,
            (_task_request_total(db, task.id) + len(buf or [])) / max_req,
        )
    if cap_in > 0:
        committed_in, _ = _task_token_totals(db, task.id)
        buf_in = sum(
            int(r.get("input_tokens") or 0)
            for r in (buf or [])
            if r.get("input_tokens") is not None
        )
        frac = max(frac, ((committed_in or 0) + buf_in) / cap_in)
    return max(0.0, min(1.0, frac))


def _preserve_partial_work(db, task: Task, path: str, reason: str) -> dict | None:
    """NEEDS_REVIEW instead of FAILED+wipe when the workspace holds real work.

    A stall-stop, budget exhaustion, or crash can happen after the model
    already made the correct edit (production task #68: the fix landed at
    step 8, then the run burned its remaining budget exploring and the
    workspace was wiped along with it). Preserving the workspace keeps that
    work actionable; the user still approves the publish, so nothing
    unreviewed reaches GitHub.
    """
    # Firecracker: guest results reach the host only on the success path, so
    # sync them back before judging whether anything was produced.
    try:
        from app.sandbox.backend import active_backend_name as _active_backend

        if _active_backend() == "firecracker":
            from app.sandbox import firecracker as _fc

            _fc.sync_guest_to_host(task_id=task.id, workspace=path)
    except Exception:
        pass
    try:
        if not _pub.has_meaningful_changes(path):
            return None
    except Exception:
        return None
    branch, _, _ = _pending_title_body(task, "")
    _set(
        db,
        task,
        status="NEEDS_REVIEW",
        branch=branch,
        error=f"work preserved ({reason}); review before publishing"[:1000],
    )
    _event(db, task.id, "NEEDS_REVIEW", {"reason": reason[:200], "branch": branch})
    db.commit()
    return {"status": "NEEDS_REVIEW", "branch": branch}


class _DeclaredEditMissing(RuntimeError):
    """Pipeline verification found a declared edit absent from host git-state."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        super().__init__(
            "workspace_sync_incomplete: declared edits "
            + ", ".join(self.missing[:3])
            + ("..." if len(self.missing) > 3 else "")
            + " never appeared in the host workspace git state"
        )


def _norm_repo_name(name: str) -> str:
    """Normalize one git-status/`changed_files` name for declared-edit
    matching (quote strip, rename `old -> new` -> new, separators)."""
    name = str(name).strip().strip('"')
    if " -> " in name:  # renames: the agent declared the new path
        name = name.split(" -> ", 1)[1]
    name = name.replace("\\", "/").strip().strip('"')
    return name[2:] if name.startswith("./") else name


def _declared_edit_paths(events) -> list[str]:
    """Repo-relative paths the agent SUCCESSFULLY edited this run.

    `events` are the LoopResult TOOL_CALL metadata dicts ({tool, args, ok}).
    Only ok=True write_file/edit_file calls count — a failed edit attempt is
    not a claim that a file changed. This is the contract _verify_declared_edits
    enforces: an agent-declared edit must be visible to host git, or the sync
    broke and publish must not proceed.
    """
    seen: list[str] = []
    for ev in events or ():
        if not isinstance(ev, dict) or not ev.get("ok"):
            continue
        if ev.get("tool") not in ("write_file", "edit_file"):
            continue
        args = ev.get("args") or {}
        rel = str(args.get("path", "")).replace("\\", "/").strip()
        rel = rel[2:] if rel.startswith("./") else rel
        if rel and rel not in seen:
            seen.append(rel)
    return seen


def _workspace_sync_incomplete(db, task: Task, missing: list[str]) -> dict:
    """NEEDS_REVIEW with the workspace PRESERVED (never wiped).

    The workspace may still hold the agent's other correct work; the missing
    list tells the reviewer exactly which claim could not be trusted. A new
    run or approve-publish re-syncs from the VM; nothing unpublished of the
    declared set can reach GitHub.
    """
    branch, _, _ = _pending_title_body(task, "")
    reason = (
        "workspace_sync_incomplete: declared agent edits missing from host "
        "workspace git state: "
        + ", ".join(missing[:5])
        + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
    )
    _set(db, task, status="NEEDS_REVIEW", branch=branch, error=reason[:1000])
    _event(
        db,
        task.id,
        "WORKSPACE_SYNC_INCOMPLETE",
        {"missing": missing[:20], "count": len(missing), "branch": branch},
    )
    db.commit()
    return {"status": "NEEDS_REVIEW", "branch": branch, "missing_files": missing}


def _verify_declared_edits(
    db, task: Task, path: str, events, *, summary: str = ""
) -> dict | None:
    """Gate: every declared agent edit must appear in the HOST git state.

    Tasks #70/#71: the guest reported success editing `.github/workflows/
    ci.yml`, the buggy sync silently dropped the file, and `git add` happily
    published everything EXCEPT the fix. After a full (non-truncated)
    manifest sync, any declared edit still absent from `git status` is a
    broken transfer — NEEDS_REVIEW, workspace preserved, publish blocked.
    Extra host changes the agent never declared are only reported as a
    SYNC_ANOMALY event (guests can also produce artifacts via commands);
    they never block a run.

    Returns None when staging may proceed; an outcome dict otherwise.
    """
    from app.github import publisher as _pubmod
    from app.sandbox.backend import active_backend_name as _active_backend

    if _active_backend() != "firecracker":
        return None  # host backend edits land directly; nothing crosses vsock
    declared = _declared_edit_paths(events)
    proc = _pubmod._git(path, "status", "--porcelain=v1", "-uall")
    if proc.returncode != 0:
        return None  # status-verified failures handled by the git gate below
    present = {
        _norm_repo_name(ln[3:]) for ln in proc.stdout.splitlines() if len(ln) >= 4
    }
    missing = [rel for rel in declared if rel and rel not in present]
    if missing:
        raise _DeclaredEditMissing(missing)
    undeclared = sorted(rel for rel in present if rel and rel not in declared)
    if undeclared:
        _event(
            db,
            task.id,
            "SYNC_ANOMALY",
            {"extra": undeclared[:20], "declared": len(declared)},
        )
    return None


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

        # Production incident fix (#70/#71): the workspace root must be
        # usable by THIS process identity before any repo is cloned or a
        # VM booted into it. Fail BLOCKED with a pointed message here
        # instead of a publish-time `git add` Permission denied later.
        _root_failures: list[str] = []
        try:
            from app.repo import workspace_preflight as _preflight

            _root_failures = _preflight.preflight_workspace_root()
        except Exception as exc:  # preflight must not crash the pipeline
            _root_failures = [f"<preflight-error: {str(exc)[:120]}>"]
        if _root_failures:
            _set(
                db,
                task,
                status="BLOCKED",
                error=(
                    "workspace root unusable: " + "; ".join(_root_failures[:3])
                )[:1000],
            )
            _event(
                db,
                task.id,
                "BLOCKED",
                {"reason": "workspace_root_preflight"},
            )
            db.commit()
            return {"status": "BLOCKED", "error": task.error}

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
            # CI tasks must be pinned to the exact failing commit BEFORE the
            # guest is provisioned. provision() streams the host tree into the
            # VM, so a checkout after it leaves the agent reading the default
            # branch while the prompt describes a different revision (the
            # production trace could not find its own workflow step). An
            # unreachable SHA is a blocked task, never a silent fallback.
            if task.trigger_type == "ci" and task.ci_sha and not repair:
                pin = _pin_ci_workspace(path, task.ci_sha)
                if not pin["ok"]:
                    reason = (
                        f"{pin['reason']}: {pin['sha']} "
                        "(force-push/closed PR or wrong revision)"
                    )
                    _set(db, task, status="BLOCKED", error=reason)
                    _event(
                        db,
                        task.id,
                        "CI_CHECKOUT",
                        {
                            "sha": pin["sha"],
                            "ok": False,
                            "reason": pin["reason"],
                            "actual": pin["actual"],
                        },
                    )
                    _event(
                        db,
                        task.id,
                        "CONTEXT_MISMATCH"
                        if pin["reason"] == "context_mismatch"
                        else "BLOCKED",
                        {
                            "reason": pin["reason"],
                            "expected": pin["sha"],
                            "actual": pin["actual"],
                        },
                    )
                    db.commit()
                    _cleanup(task.id)
                    return {"status": "BLOCKED", "error": task.error}
                _event(
                    db,
                    task.id,
                    "CI_CHECKOUT",
                    {"sha": pin["sha"], "verified": True, "actual": pin["actual"]},
                )
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

        # --- CI failure evidence (the trigger is the ground truth) ---
        ci_info = ""
        if task.trigger_type == "ci":
            from app.agent import context as _ctxmod

            excerpt = task.ci_excerpt or ""
            has_signal = _ctxmod.has_failure_signal(excerpt)
            # Webhook tasks carry only a one-line summary now (and older tasks
            # may carry metadata-only "logs"). Fetch the real failing-log tail
            # worker-side so the agent starts with the exact error instead of
            # re-reading the workflow to guess. Deterministic, bounded.
            if not has_signal:
                tail = _enrich_ci_logs(
                    repository=task.repository,
                    installation_id=installation_id,
                    run_id=task.ci_run_id,
                    # Only the exact job identity (when known); an empty value
                    # makes the client select the actual failed job instead of
                    # guessing the workflow name (which matched no job).
                    job_name=task.ci_job or "",
                    check_url=task.ci_url,
                )
                if tail:
                    excerpt = f"{excerpt}\n{tail}" if excerpt else tail.lstrip("\n")
                    task.ci_excerpt = excerpt
                    has_signal = _ctxmod.has_failure_signal(excerpt)
                    db.commit()
            # Deferred webhook enrichment: workflow content / steps / PR files
            # are fetched HERE (worker), never in the webhook request path.
            try:
                from app.github.webhook import build_ci_context_from_task as _ctx_from_task

                enriched = _ctx_from_task(task)
                _event(db, task.id, "CI_CONTEXT_ENRICHED", enriched, limit=20000)
                db.commit()
            except Exception:
                db.rollback()
            ci_info = (
                f"workflow={task.ci_workflow} job={task.ci_job} sha={task.ci_sha} "
                f"run={task.ci_url}\nexcerpt:\n{_ctxmod.clip_logs(excerpt, 6000)}"
            )
            _event(
                db,
                task.id,
                "CI_EVIDENCE",
                {
                    "has_logs": has_signal,
                    "has_workflow": bool(task.ci_workflow),
                    "signal": _ctxmod.failure_signal(excerpt)[:300],
                },
            )
            db.commit()

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
                # FILE_CHANGED means the workspace actually changed. A rejected
                # or gated edit did not write anything, so it must not claim it
                # did (the production trace emitted FILE_CHANGED for a failed
                # `oldString not found` edit).
                if _is_file_change_event(ev):
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
        # Phase 4.5: one cumulative agent budget for the run
        # (per-invocation LLM_MAX_RUNTIME_S still applies).
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
                    on_tool=_live,
                    is_cancelled=_cancel_flag(task.id),
                    ctx=ctx,
                    deadline_mono=agent_deadline,
                    lease_guard=lease_guard,
                )
                if getattr(result, "lease_lost", False):
                    return _fail_lease_lost(db, task, at="during agent run")
                if getattr(result, "usage_limited", False):
                    preserved = _preserve_partial_work(
                        db, task, path, "usage_limit"
                    )
                    if preserved is not None:
                        return preserved
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
            preserved = _preserve_partial_work(db, task, path, "agent_crash")
            if preserved is not None:
                return preserved
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
            # A stall-stop can follow a correct edit (the model kept looping
            # after the fix landed). Preserve real work for review instead of
            # wiping the workspace along with it.
            preserved = _preserve_partial_work(db, task, path, "agent_stopped")
            if preserved is not None:
                return preserved
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

        # Production incident fix (#70/#71): after sync, verify every agent-
        # declared edit is actually visible in the host git state. A missing
        # declared edit blocks publish (NEEDS_REVIEW, workspace preserved).
        try:
            verified = _verify_declared_edits(db, task, path, result.events)
        except _DeclaredEditMissing as exc:
            preserved = _workspace_sync_incomplete(db, task, exc.missing)
            preserved["summary"] = result.summary
            return preserved
        except Exception as exc:
            # Checker failure must never open the publish gate unverified.
            preserved = _workspace_sync_incomplete(
                db, task, [f"<verification-error: {str(exc)[:120]}>"]
            )
            preserved["summary"] = result.summary
            return preserved

        # --- git (FixHub owns it) ---
        try:
            if not _pub.has_meaningful_changes(path):
                _event(db, task.id, "AGENT_FINISHED", {"result": "no changes produced"})
                db.commit()
                out = _no_changes_outcome(db, task)
                out.setdefault("summary", result.summary)
                return out
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
            db.commit()
            _cleanup(task.id)
            return {
                "status": "COMPLETED",
                "superseded_by": green,
                "summary": result.summary,
            }

        # --- review gate: stop here for IDE approval (no commit, keep workspace) ---
        if not _resolve_auto_publish(auto_publish):
            _set(db, task, status="NEEDS_REVIEW", branch=branch, error="")
            _event(
                db, task.id, "NEEDS_REVIEW", {"branch": branch, "files": changed[:20]}
            )
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
