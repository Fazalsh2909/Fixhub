"""PR Publisher — the ONLY GitHub write path. Tightly scoped by design.

SIMPLE PATH (active): publish_issue_fix() takes the agent's workspace diff
and publishes it: branch → commit → push → PR. Success = finished agent +
meaningful diff. No verification gates, no proof gating.

LEGACY PATH (bypassed, kept for history): VerifiedArtifact +
build_verified_artifact() + PRPublisher.publish() gated on VERIFIED.
Left untouched; the simple worker no longer calls it.

Both paths refuse default-branch targets (using the repo's ACTUAL
default_branch). The agent has no other write tool: agent git verbs are
limited to read-only inspection (see tools/command_policy.py).
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

API = "https://api.github.com"
_GIT_TIMEOUT = 120


@dataclass
class VerifiedArtifact:
    repo_full_name: str
    base_branch: str
    new_branch: str
    patch_diff: str  # unified diff, already verified
    title: str
    body: str  # includes Proof of Fix
    proof_passed: bool
    # Phase 8: enough information to reproduce exactly what will be published.
    base_sha: str = ""
    verification_status: str = ""  # VERIFIED expected
    approval_id: int = 0
    changed_files: list[str] = field(default_factory=list)
    verification_run_ids: list[int] = field(default_factory=list)


class PolicyDeniedError(PermissionError):
    pass


class PublishError(RuntimeError):
    """A publish step failed AFTER some durable step succeeded. Carries the
    last good state so the task lands on an honest state, never a fake one."""


def redact(text: str, secrets: list[str]) -> str:
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


def _run_git(args: list[str], cwd: Path, secrets: list[str] | None = None) -> str:
    """Run one git plumbing command (argv, no shell). Raises RuntimeError
    with secrets scrubbed."""
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git {' '.join(args[:3])} timed out") from e
    if p.returncode != 0:
        raise RuntimeError(
            redact(
                f"git {' '.join(args[:4])} failed: {(p.stderr or p.stdout)[-500:]}",
                secrets or [],
            )
        )
    return p.stdout.strip()


def diff_fingerprint(diff: str) -> str:
    """Stable hash of a normalized diff (trailing whitespace ignored)."""
    normalized = "\n".join(line.rstrip() for line in diff.strip().splitlines())
    return hashlib.sha256(normalized.encode()).hexdigest()


def build_verified_artifact(db: Session, task, repo) -> VerifiedArtifact:
    """Assemble the exact artifact that publishing will reproduce.

    Raises PolicyDeniedError when anything is missing or unverified:
    no real diff, verification verdict not publishable (VERIFIED or
    VERIFIED_WITH_LIMITATIONS — the latter carries its documented
    limitations into the PR body), or the target branch is the
    repository's default branch.
    """
    from ..automation import task_branch
    from ..models import Approval, Patch
    from ..verify.pipeline import verdict_for_task

    from ..models import Task as TaskRow

    assert isinstance(task, TaskRow)
    patch = db.query(Patch).filter_by(task_id=task.id).order_by(Patch.id.desc()).first()
    if (
        patch is None
        or not patch.diff.strip()
        or patch.diff.strip() == "(no files changed)"
    ):
        raise PolicyDeniedError("no verified diff to commit yet")
    verdict, run_ids, gate_lines = verdict_for_task(db, task.id)
    if verdict not in ("VERIFIED", "VERIFIED_WITH_LIMITATIONS"):
        raise PolicyDeniedError(
            f"verification is {verdict} — only VERIFIED or "
            "VERIFIED_WITH_LIMITATIONS may publish"
        )
    branch = task_branch(task, patch)
    default_branch = (repo.default_branch if repo is not None else "") or "main"
    if branch == default_branch or branch in ("main", "master"):
        raise PolicyDeniedError(f"must not push directly to default branch ({branch})")
    approval = (
        db.query(Approval)
        .filter_by(task_id=task.id)
        .order_by(Approval.id.desc())
        .first()
    )
    changed = sorted(
        {
            line[6:]
            for line in patch.diff.splitlines()
            if line.startswith(("+++ b/", "--- a/"))
        }
    )
    title = (
        f"Fix #{task.issue_number}: {task.title}" if task.issue_number else task.title
    )
    body_lines = [
        f"Proof of Fix for #{task.issue_number}"
        if task.issue_number
        else "Proof of Fix",
        "",
        f"Verification: {verdict}",
        *gate_lines,
        "",
        "Files changed:",
        *[f"- {c}" for c in changed[:20]],
    ]
    return VerifiedArtifact(
        repo_full_name=repo.full_name if repo is not None else "",
        base_branch=default_branch,
        new_branch=branch,
        patch_diff=patch.diff,
        title=title,
        body="\n".join(body_lines),
        proof_passed=True,
        base_sha=task.base_sha or "",
        verification_status=verdict,
        approval_id=approval.id if approval else 0,
        changed_files=changed,
        verification_run_ids=run_ids,
    )


def verify_workspace_matches(task, artifact: VerifiedArtifact) -> tuple[bool, str]:
    """Phase 8 integrity: the workspace diff must still equal the verified
    patch, and the base commit must not have moved. Returns (ok, reason).

    Legacy rows without a recorded workspace return (True, 'legacy…') — every
    new run records a workspace, so this only covers pre-P0-1 rows.
    """
    if not task.workspace_path:
        return (
            True,
            "legacy task without isolated workspace — publishing recorded diff as-is",
        )
    workdir = Path(task.workspace_path)
    if not workdir.is_dir():
        return False, "task workspace is gone — re-run to re-verify before publishing"
    from ..repo.workspaces import base_head_sha, git_diff_all, is_git_repo

    current_diff = git_diff_all(workdir)
    if diff_fingerprint(current_diff) != diff_fingerprint(artifact.patch_diff):
        return False, "workspace changed after verification — re-run to re-verify"
    if task.base_sha and is_git_repo(workdir):
        head = base_head_sha(workdir)
        # A worktree HEAD moves only via explicit checkout; any move voids
        # the verified baseline.
        if head and head != task.base_sha:
            # Commits created BY the publisher itself live on the new branch;
            # a bare HEAD move without diff change is still a baseline change.
            return (
                False,
                f"workspace base moved ({task.base_sha[:8]} → {head[:8]}) — re-verify",
            )
    return True, "workspace matches verified artifact"


def create_branch(workdir: Path, branch: str) -> None:
    """Create and check out the task branch in the isolated workspace.
    The branch must be new — reusing one could mix unverified history."""
    existing = _run_git(["branch", "--list", branch], cwd=workdir)
    if existing.strip():
        raise PublishError(f"branch {branch} already exists — delete it or re-run")
    _run_git(["checkout", "-b", branch], cwd=workdir)


def commit_worktree(workdir: Path, message: str) -> str:
    """Commit the verified working tree on the current branch. Returns SHA."""
    _run_git(["add", "-A"], cwd=workdir)
    status = _run_git(["status", "--porcelain"], cwd=workdir)
    if not status.strip():
        raise PublishError("nothing to commit — workspace is clean")
    _run_git(["commit", "-m", message[:500]], cwd=workdir)
    return _run_git(["rev-parse", "HEAD"], cwd=workdir)


def commit_in_workspace(workdir: Path, branch: str, message: str) -> str:
    """Create the task branch in the isolated workspace and commit the
    verified working tree. Returns the new commit SHA."""
    create_branch(workdir, branch)
    return commit_worktree(workdir, message)


def push_branch(workdir: Path, remote_url: str, branch: str, token: str = "") -> None:
    """Push the task branch to the remote. Token never appears in errors."""
    url = remote_url
    if (
        token
        and "github.com" in url
        and "@" not in url.split("//", 1)[-1].split("/", 1)[0]
    ):
        url = url.replace(
            "https://github.com/", f"https://x-access-token:{token}@github.com/"
        )
    try:
        p = subprocess.run(
            ["git", "push", url, f"{branch}:{branch}"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise PublishError(f"git push of {branch} timed out") from e
    if p.returncode != 0:
        raise PublishError(
            redact(
                f"git push of {branch} failed: {(p.stderr or p.stdout)[-500:]}", [token]
            )
        )


def verify_remote_sha(
    remote_url: str, branch: str, expected_sha: str, token: str = ""
) -> str:
    """Confirm the remote branch points at exactly the pushed commit."""
    url = remote_url
    if (
        token
        and "github.com" in url
        and "@" not in url.split("//", 1)[-1].split("/", 1)[0]
    ):
        url = url.replace(
            "https://github.com/", f"https://x-access-token:{token}@github.com/"
        )
    try:
        p = subprocess.run(
            ["git", "ls-remote", url, f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as e:
        raise PublishError(f"remote SHA check for {branch} timed out") from e
    if p.returncode != 0:
        raise PublishError(
            redact(f"remote SHA check failed: {(p.stderr or p.stdout)[-300:]}", [token])
        )
    remote_sha = p.stdout.strip().split()[0] if p.stdout.strip() else ""
    if not remote_sha:
        raise PublishError(f"remote branch {branch} not found after push")
    if remote_sha != expected_sha:
        raise PublishError(
            f"remote SHA mismatch on {branch}: expected {expected_sha[:8]}, got {remote_sha[:8]}"
        )
    return remote_sha


class PRPublisher:
    def __init__(self, installation_token: str, default_branch: str = "main") -> None:
        self._token = installation_token
        self._default = default_branch

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }

    def publish(self, artifact: VerifiedArtifact) -> dict:
        if not artifact.proof_passed:
            raise PolicyDeniedError("proof did not pass — refusing to publish")
        if artifact.verification_status and artifact.verification_status not in (
            "VERIFIED",
            "VERIFIED_WITH_LIMITATIONS",
        ):
            raise PolicyDeniedError(
                f"verification is {artifact.verification_status} — only VERIFIED or "
                "VERIFIED_WITH_LIMITATIONS may publish"
            )
        default = artifact.base_branch or self._default
        if artifact.new_branch == default or artifact.new_branch in ("main", "master"):
            raise PolicyDeniedError("must not push directly to default branch")
        # Real implementation: create branch ref, push via git-over-https in worker, open PR.
        # Thin scaffold performs the PR creation call; git push happens in sandbox worker
        # with the same branch guard. Kept explicit so tests can assert the guard.
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{API}/repos/{artifact.repo_full_name}/pulls",
                headers=self._headers(),
                json={
                    "title": artifact.title,
                    "head": artifact.new_branch,
                    "base": artifact.base_branch,
                    "body": artifact.body,
                },
            )
            r.raise_for_status()
            return r.json()


ASK_BACK_MARKER = "<!-- fixhub-clarify -->"


def build_ask_back_comment(
    *,
    issue_number: int,
    issue_title: str,
    steps_taken: int,
    tests_note: str = "",
) -> str:
    """Short clarifying question for the issue author. No reasoning leaks."""
    lines = [
        f"I tried to fix #{issue_number} ({(issue_title or '').strip()[:150]}) "
        f"but couldn't identify the change needed ({steps_taken} steps explored, "
        "no code changes made).",
    ]
    note = (tests_note or "").strip()
    if note and note != "Not reported by agent.":
        lines.append(f"What I observed: {note[:300]}")
    lines += [
        "",
        "To help me fix this, please reply with any of:",
        "1. What you expected vs what actually happens (error text helps).",
        "2. Steps to reproduce the problem.",
        "3. The area, file, or endpoint involved.",
        "",
        "I'll pick this back up automatically when you reply.",
        "",
        ASK_BACK_MARKER,
    ]
    return "\n".join(lines)


def post_issue_comment(token: str, repo_full_name: str, number: int, body: str) -> dict:
    """Post one comment on a GitHub issue. Minimal write path for ask-backs."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{API}/repos/{repo_full_name}/issues/{number}/comments",
            headers=headers,
            json={"body": body[:4000]},
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {}


def build_simple_pr_body(
    *,
    issue_number: int,
    issue_title: str,
    changed_files: list[str],
    tests_note: str = "",
) -> tuple[str, str]:
    """Simple PR title/body from real information. Never fabricates results."""
    title = f"Fix #{issue_number}: {issue_title}" if issue_number else issue_title
    lines = [
        "## Fix",
        "",
        f"Closes #{issue_number}" if issue_number else "",
        (issue_title or "").strip(),
        "",
        "## Changes",
        "",
        *([f"- {c}" for c in changed_files[:20]] or ["(see diff)"]),
        "",
        "## Tests",
        "",
        (tests_note or "").strip() or "Not reported by agent.",
    ]
    return title[:200], "\n".join(line for line in lines if line is not None).strip()


def publish_issue_fix(db, task, repo, workdir: Path, tests_note: str = "") -> dict:
    """Simple publish: workspace diff → branch → commit → push → PR.

    Success = agent finished + meaningful diff. No verification gates.
    Raises PolicyDeniedError on empty diff or default-branch target,
    PublishError when a git/GitHub step fails (caller records FAILED).
    """
    from ..models import PullRequest

    default_branch = (getattr(repo, "default_branch", "") or "main").strip() or "main"
    try:
        from ..repo.workspaces import git_diff_all

        diff = git_diff_all(workdir) if workdir.is_dir() else ""
    except Exception:
        diff = ""
    if not (diff or "").strip() or (diff or "").strip() == "(no files changed)":
        raise PolicyDeniedError("No code changes produced — refusing empty PR.")
    changed = sorted(
        {
            line[6:].strip()
            for line in diff.splitlines()
            if line.startswith(("+++ b/", "--- a/"))
            and line[6:].strip() not in ("dev/null", "/dev/null")
        }
    )
    base_branch_name = (
        f"fix/issue-{task.issue_number}" if task.issue_number else f"fix/task-{task.id}"
    )
    branch = base_branch_name
    try:
        existing = _run_git(["branch", "--list", branch], cwd=workdir)
        suffix = 2
        while existing.strip():
            branch = f"{base_branch_name}-{suffix}"
            existing = _run_git(["branch", "--list", branch], cwd=workdir)
            suffix += 1
            if suffix > 50:
                raise PublishError(f"branch {base_branch_name} already exists")
    except PublishError:
        raise
    except Exception as e:
        raise PublishError(f"branch check failed: {e}")
    if branch == default_branch or branch in ("main", "master"):
        raise PolicyDeniedError(f"must not push directly to default branch ({branch})")

    title, body = build_simple_pr_body(
        issue_number=task.issue_number or 0,
        issue_title=task.title or "",
        changed_files=changed,
        tests_note=tests_note,
    )
    sha = commit_in_workspace(workdir, branch, f"{title}\n\n{body[:1500]}")

    installation_id = (getattr(repo, "installation_id", "") or "").strip()
    remote_url = (getattr(repo, "clone_url", "") or "").strip() or (
        f"https://github.com/{repo.full_name}.git"
        if getattr(repo, "full_name", "")
        else ""
    )
    if not installation_id:
        # Local-only record (demo / no GitHub App): honest COMMITTED-equivalent.
        db.add(
            PullRequest(task_id=task.id, url="", number=0, commit_sha=sha),
        )
        try:
            from ..models import Patch

            db.add(Patch(task_id=task.id, diff=diff[-20000:], branch=branch))
        except Exception:
            pass
        db.commit()
        return {
            "branch": branch,
            "commit_sha": sha,
            "pr_url": "",
            "changed_files": changed,
        }

    from .app_auth import get_installation_token

    token = get_installation_token(installation_id)
    try:
        push_branch(workdir, remote_url, branch, token)
        remote_sha = verify_remote_sha(remote_url, branch, sha, token)
    except PublishError:
        raise
    pr = _create_simple_pr(token, repo.full_name, default_branch, branch, title, body)
    url = pr.get("html_url", "") if isinstance(pr, dict) else ""
    number = pr.get("number", 0) if isinstance(pr, dict) else 0
    db.add(PullRequest(task_id=task.id, url=url, number=number, commit_sha=remote_sha))
    try:
        from ..models import Patch

        db.add(Patch(task_id=task.id, diff=diff[-20000:], branch=branch))
    except Exception:
        pass
    db.commit()
    return {
        "branch": branch,
        "commit_sha": remote_sha,
        "pr_url": url,
        "changed_files": changed,
    }


def _create_simple_pr(
    token: str, repo_full_name: str, base: str, head: str, title: str, body: str
) -> dict:
    """Create a GitHub PR without any verification gating."""
    if head == base or head in ("main", "master"):
        raise PolicyDeniedError("must not push directly to default branch")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{API}/repos/{repo_full_name}/pulls",
            headers=headers,
            json={"title": title, "head": head, "base": base, "body": body},
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {}
