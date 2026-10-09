"""FixHub owns ALL git operations. The LLM never branches/commits/pushes/PRs.

ONE TASK = ONE WORKSPACE = ONE BRANCH = ONE PR. The branch embeds the task id
(`fixhub-fixes/issue-123-task-7`), so concurrent tasks for one repository can
never share a branch, and every run of the same task — initial attempt, gate
fix rounds, CI repair rounds, manual approve — resolves the identical branch
and reuses the same PR.
"""

from __future__ import annotations

import subprocess

from app.github import client as _gh

FIX_BRANCH = "fixhub-fixes"


class PublishError(RuntimeError):
    pass


def _git(path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, timeout=60
    )


def _ref_exists(path: str, ref: str) -> bool:
    return _git(path, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def has_meaningful_changes(path: str) -> bool:
    proc = _git(path, "status", "--porcelain=v1", "-uall")
    if proc.returncode != 0:
        raise PublishError(f"git status failed: {proc.stderr[-300:]}")
    lines = [
        ln for ln in proc.stdout.splitlines() if ln.strip() and "__pycache__" not in ln
    ]
    return bool(lines)


def changed_files(path: str) -> list[str]:
    proc = _git(path, "status", "--porcelain=v1", "-uall")
    files = []
    for ln in proc.stdout.splitlines():
        name = ln[3:].strip().strip('"')
        # Same exclusion as has_meaningful_changes: interpreter bytecode from
        # in-sandbox test runs must never leak into diffs, memory, or PRs.
        if name and "__pycache__" not in name:
            files.append(name)
    return files[:200]


def ensure_branch(
    path: str, *, branch: str = FIX_BRANCH, base: str = "main", start: str = ""
) -> dict:
    """Checkout the standing fix branch, creating it on first use.

    - local branch exists -> checkout it, fast-forward to origin when possible
    - only remote branch exists -> track-checkout it
    - neither -> create from `start` (e.g. the failing CI commit) when it
      resolves, else origin/<base> (fallback: local base, then HEAD)
    Returns {"branch": ..., "created": bool}. Raises PublishError.
    """
    _git(path, "fetch", "origin", "--quiet")  # best-effort; push surfaces real problems
    if _ref_exists(path, f"refs/heads/{branch}"):
        proc = _git(path, "checkout", branch)
        if proc.returncode != 0:
            raise PublishError(
                f"git checkout {branch} failed: {(proc.stderr or proc.stdout)[-500:]}"
            )
        # Pick up commits pushed by earlier runs (single worker: normally a no-op).
        _git(path, "pull", "--ff-only", "--quiet", "origin", branch)
        return {"branch": branch, "created": False}
    if _ref_exists(path, f"refs/remotes/origin/{branch}"):
        proc = _git(path, "checkout", "-b", branch, "--track", f"origin/{branch}")
        if proc.returncode != 0:
            raise PublishError(
                f"git track-checkout {branch} failed: {(proc.stderr or proc.stdout)[-500:]}"
            )
        return {"branch": branch, "created": False}
    if start and _ref_exists(path, start):
        start_ref = start
    elif _ref_exists(path, f"refs/remotes/origin/{base}"):
        start_ref = f"origin/{base}"
    elif _ref_exists(path, f"refs/heads/{base}"):
        start_ref = base
    else:
        start_ref = "HEAD"
    proc = _git(path, "checkout", "-b", branch, start_ref)
    if proc.returncode != 0:
        raise PublishError(
            f"git checkout -b {branch} failed: {(proc.stderr or proc.stdout)[-500:]}"
        )
    return {"branch": branch, "created": True}


def publish(
    *,
    path: str,
    full_name: str,
    base: str,
    title: str,
    body: str,
    token: str,
    branch: str = FIX_BRANCH,
    start: str = "",
    lease_check=None,
) -> dict:
    """Commit+push+reuse-or-create-PR on the standing branch.

    `start` (e.g. the failing CI commit) is used when the fix branch is
    created fresh, so the fix builds on the exact failing code.
    `lease_check` (optional callable -> bool, final hardening) is verified
    immediately before the push and again between push and PR creation; on
    loss a PublishError("lease lost ...") is raised BEFORE the external side
    effect so a fenced worker never touches GitHub.
    Returns {branch, commit_sha, pr_number, pr_url} (or {no_changes: True}).
    """

    def _fenced(stage: str) -> None:
        if lease_check is None:
            return
        try:
            ok = bool(lease_check())
        except Exception:
            ok = False
        if not ok:
            raise PublishError(f"lease lost {stage}; refusing GitHub side effect")

    if not has_meaningful_changes(path):
        return {
            "branch": branch,
            "commit_sha": "",
            "pr_number": None,
            "pr_url": "",
            "no_changes": True,
        }

    ensure_branch(path, branch=branch, base=base, start=start)

    def _run(step: str, args: list[str]) -> None:
        proc = _git(path, *args)
        if proc.returncode != 0:
            raise PublishError(
                f"git {step} failed: {(proc.stderr or proc.stdout)[-500:]}"
            )

    _run("add", ["add", "-A"])
    _unstage_bytecode(path)
    # FixHub bot identity: never depend on host gitconfig (fresh workers have none).
    _run(
        "commit",
        [
            "-c",
            "user.name=fixhub",
            "-c",
            "user.email=fixhub@fixhub.local",
            "commit",
            "-m",
            title,
        ],
    )
    # Final hardening: fence BEFORE the first external side effect (push).
    _fenced("before push")
    _run("push", ["push", "-u", "origin", branch])

    head = _git(path, "rev-parse", "HEAD")
    sha = head.stdout.strip()
    # Final hardening: fence BETWEEN push and PR creation. A worker reaped
    # during the push must not mint a PR for a branch it no longer owns.
    _fenced("before PR creation")
    existing = _find_open_pr(token=token, full_name=full_name, branch=branch)
    if existing:
        try:
            _gh.update_pull(
                token=token,
                full_name=full_name,
                number=existing["number"],
                title=title,
                body=body,
            )
        except Exception:
            pass  # title refresh is cosmetic; the push already updated the PR diff
        return {
            "branch": branch,
            "commit_sha": sha,
            "pr_number": existing["number"],
            "pr_url": existing["url"],
        }
    # Phase 4.5: narrow the twin-create race. A concurrent publisher (crash
    # recovery overlapping a slow first attempt) may win between our re-check
    # above and this create: on "already exists", re-list and reuse instead
    # of failing or minting a duplicate PR.
    try:
        pr = _gh.create_pull_request(
            token=token,
            full_name=full_name,
            head=branch,
            base=base,
            title=title,
            body=body,
        )
    except Exception as exc:
        if "exists" in str(exc).lower() or "422" in str(exc):
            retry = _find_open_pr(token=token, full_name=full_name, branch=branch)
            if retry:
                return {
                    "branch": branch,
                    "commit_sha": sha,
                    "pr_number": retry["number"],
                    "pr_url": retry["url"],
                }
        raise
    return {
        "branch": branch,
        "commit_sha": sha,
        "pr_number": pr["number"],
        "pr_url": pr["url"],
    }


def _unstage_bytecode(path: str) -> None:
    """Remove interpreter bytecode from the git index (never commit .pyc).

    `git add -A` stages __pycache__ output left by in-sandbox test runs; those
    files must stay untracked in the workspace, never in a commit or PR.
    Best-effort: never raises.
    """
    try:
        proc = _git(path, "status", "--porcelain=v1", "-uall")
        if proc.returncode != 0:
            return
        staged = []
        for ln in proc.stdout.splitlines():
            if len(ln) < 4 or (ln[0] == " " and ln[1] == " "):
                continue  # unstaged-only entries
            name = ln[3:].strip().strip('"')
            if " -> " in name:  # renames: take the new path
                name = name.split(" -> ", 1)[1].strip().strip('"')
            if "__pycache__" in name:
                staged.append(name)
        if staged:
            _git(path, "reset", "-q", "--", *staged)
    except Exception:
        pass


def _find_open_pr(*, token: str, full_name: str, branch: str) -> dict | None:
    """The open PR from the standing branch, if any. None on API failure."""
    try:
        for pr in _gh.list_open_pulls(token=token, full_name=full_name):
            if pr.get("head_branch") == branch:
                return pr
    except Exception:
        pass
    return None


# ONE TASK = ONE BRANCH. Names embed the task id, so collisions are
# impossible (task ids are autoincrement) and every run of the same task —
# initial attempt, gate fix rounds, CI repair rounds, manual approve — resolves
# the identical branch deterministically.
def branch_for_issue(issue_number: int, task_id: int) -> str:
    return f"fixhub-fixes/issue-{issue_number or 0}-task-{task_id}"


def branch_for_ci(sha: str, task_id: int) -> str:
    short = (sha or "head")[:7]
    return f"fixhub-fixes/ci-{short}-task-{task_id}"
