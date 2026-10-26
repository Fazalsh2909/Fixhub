"""Minimal GitHub API client (httpx). Used for PR creation + Actions log excerpts."""
from __future__ import annotations

import httpx

from app.config import settings


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def create_pull_request(
    *,
    token: str,
    full_name: str,
    head: str,
    base: str,
    title: str,
    body: str,
) -> dict:
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/pulls"
    resp = httpx.post(
        url, headers=_headers(token), json={"head": head, "base": base, "title": title, "body": body}, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    return {"number": data["number"], "url": data["html_url"]}


def failing_logs_excerpt(*, token: str, full_name: str, run_id: str, max_bytes: int = 8000) -> str:
    """Failure-signal excerpt for a failed run (real log, not step metadata).

    Delegates to failed_log_tail() so callers get the exact error text
    (pytest FAILURES, ruff, npm) instead of "JOB x conclusion=failure".
    Raises on API failure; callers treat this as best-effort.
    """
    return failed_log_tail(
        token=token,
        full_name=full_name,
        run_id=run_id,
        max_bytes=min(int(max_bytes or 6000), 6000),
    )


def get_workflow_run(*, token: str, full_name: str, run_id: str) -> dict:
    """Workflow-run metadata behind a check_run (name/path/sha/branch).

    A check_run payload only carries the job; the workflow file and run-level
    branch/sha live on the parent run. Raises on API failure.
    """
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/actions/runs/{run_id}"
    resp = httpx.get(url, headers=_headers(token), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {
        "id": str(data.get("id", run_id)),
        "name": data.get("name", "") or "",
        "path": data.get("path", "") or "",
        "head_sha": data.get("head_sha", "") or "",
        "head_branch": data.get("head_branch", "") or "",
        "html_url": data.get("html_url", "") or "",
        "conclusion": data.get("conclusion", "") or "",
        "pull_requests": data.get("pull_requests") or [],
    }


def _signal_line(line: str) -> bool:
    """True when a log line looks like failure evidence (not progress noise)."""
    s = line.strip()
    if not s:
        return False
    if len(s) > 500:
        s = s[:500]
    probes = ("FAILED", "failed", "Error", "ERROR", "Traceback",
              "assert", "raise ", "E   ", "F   ")
    return any(p in line for p in probes)


def extract_failure_signal(lines: list[str], *, max_bytes: int = 6000) -> str:
    """Select failure-relevant sections from CI job log lines.

    The full log is already downloaded by the caller; a plain tail keeps the
    summary but drops the failure bodies that precede it (e.g. pytest's
    FAILURES section). This keeps, in original order:
    - pytest-style failure/summary sections (FAILURES, short test summary,
      ERRORS) and any line matching failure probes;
    - the trailing lines (result/summary line always survives);
    and drops the pytest warnings-summary section (usually the bulkiest,
    lowest-signal part). Non-pytest logs without any signal fall back to a
    plain tail. Bounded with a middle cut that preserves both ends.
    """
    import re as _re

    header_re = _re.compile(r"^=+ (.+) =+$")
    keep_ranges: list[tuple[int, int]] = []
    drop_ranges: list[tuple[int, int]] = []
    current: tuple[int, str] | None = None  # (start, kind)
    for i, ln in enumerate(lines):
        m = header_re.match(ln.strip())
        if not m:
            continue
        if current is not None:
            start, kind = current
            (keep_ranges if kind == "keep" else drop_ranges).append((start, i))
            current = None
        title = m.group(1).strip().lower()
        if "warnings summary" in title:
            current = (i, "drop")
        elif ("failure" in title or "short test summary" in title
                or title.startswith("errors")):
            current = (i, "keep")
    if current is not None:
        start, kind = current
        (keep_ranges if kind == "keep" else drop_ranges).append((start, len(lines)))

    dropped = set()
    for start, end in drop_ranges:
        dropped.update(range(start, end))

    tail_start = max(0, len(lines) - 60)
    signal_hit = bool(keep_ranges) or any(
        i < tail_start and i not in dropped and _signal_line(ln)
        for i, ln in enumerate(lines)
    )
    if not signal_hit:
        # No recognizable signal (ruff/mypy/npm style logs): plain tail.
        return "\n".join(lines[-120:])

    keep: set[int] = set()
    for start, end in keep_ranges:
        keep.update(i for i in range(start, end) if i not in dropped)
    for i, ln in enumerate(lines):
        if i not in dropped and _signal_line(ln):
            keep.add(i)
    # Trailing lines always survive (result/summary line).
    keep.update(range(tail_start, len(lines)))
    keep = {i for i in keep if i not in dropped}

    selected = "\n".join(lines[i] for i in sorted(keep))
    if len(selected) <= max_bytes:
        return selected
    marker = f"\n...[{len(selected) - max_bytes} characters omitted]...\n"
    budget = max(0, max_bytes - len(marker))
    head = int(budget * 0.6)
    tail = budget - head
    return selected[:head] + marker + (selected[-tail:] if tail else "")


def failed_log_tail(
    *,
    token: str,
    full_name: str,
    run_id: str,
    job_name: str = "",
    max_lines: int = 120,
    max_bytes: int = 6000,
) -> str:
    """Download the log of the failed job and return its failure signal.

    Gives the agent the exact error text (e.g. pytest's FAILURES section,
    ruff's "Would reformat: ...") instead of just a step name. The full log
    is downloaded; failure sections are extracted and noise (warnings
    summaries, timestamps) is dropped. Bounded; raises on API failure so
    callers can fall back to failing_logs_excerpt().
    """
    jobs_url = f"{settings.GITHUB_API_URL}/repos/{full_name}/actions/runs/{run_id}/jobs"
    resp = httpx.get(url=jobs_url, headers=_headers(token), timeout=30)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    failed = [j for j in jobs if j.get("conclusion") == "failure"] or jobs[:1]
    if job_name:
        named = [j for j in failed if j.get("name") == job_name]
        if named:
            failed = named
    job = failed[0]
    log_url = (
        f"{settings.GITHUB_API_URL}/repos/{full_name}/actions/jobs/{job.get('id')}/logs"
    )
    log = httpx.get(url=log_url, headers=_headers(token), timeout=60, follow_redirects=True)
    log.raise_for_status()
    lines = log.text.splitlines()
    # Strip runner timestamps (2026-09-28T09:25:51.9614987Z ...) for density.
    import re as _re

    clean = [_re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s?", "", ln) for ln in lines]
    signal = extract_failure_signal(clean, max_bytes=max_bytes)
    if signal == "\n".join(clean[-max_lines:]):
        header = f"JOB {job.get('name')} log tail (last {max_lines} lines):\n"
    else:
        header = f"JOB {job.get('name')} failure signal (extracted sections + tail):\n"
    out = header + signal
    return out[:max_bytes] if len(out) > max_bytes else out


def list_open_pulls(*, token: str, full_name: str, limit: int = 30) -> list[dict]:
    """Open PRs: number, head branch/sha, url, body head. Raises on API failure."""
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/pulls"
    resp = httpx.get(url, headers=_headers(token),
                     params={"state": "open", "per_page": min(max(limit, 1), 100)}, timeout=30)
    resp.raise_for_status()
    return [
        {"number": pr.get("number"), "url": pr.get("html_url", ""),
         "head_branch": (pr.get("head") or {}).get("ref", ""),
         "head_sha": (pr.get("head") or {}).get("sha", ""),
         "body": (pr.get("body") or "")[:2000]}
        for pr in resp.json()
    ][:limit]


def pull_files(*, token: str, full_name: str, number: int, limit: int = 100) -> list[str]:
    """Filenames touched by a PR. Raises on API failure."""
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/pulls/{number}/files"
    resp = httpx.get(url, headers=_headers(token),
                     params={"per_page": min(max(limit, 1), 100)}, timeout=30)
    resp.raise_for_status()
    return [f.get("filename", "") for f in resp.json() if f.get("filename")][:limit]


def sha_check_conclusion(*, token: str, full_name: str, sha: str) -> str:
    """Aggregate check conclusion for a commit: success|failure|pending|unknown.

    success = every reported check completed with success/skipped/neutral and
    at least one check exists. Anything failing -> failure. Else pending/unknown.
    Raises on API failure.
    """
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/commits/{sha}/check-runs"
    resp = httpx.get(url, headers=_headers(token), timeout=30)
    resp.raise_for_status()
    runs = resp.json().get("check_runs", [])
    if not runs:
        return "unknown"
    if any(r.get("conclusion") == "failure" for r in runs):
        return "failure"
    if all((r.get("status") == "completed"
            and r.get("conclusion") in ("success", "skipped", "neutral")) for r in runs):
        return "success"
    return "pending"


def close_pull(*, token: str, full_name: str, number: int, comment: str = "") -> dict:
    """Close a PR (and optionally comment why). Returns {closed: True}."""
    if comment:
        httpx.post(f"{settings.GITHUB_API_URL}/repos/{full_name}/issues/{number}/comments",
                   headers=_headers(token), json={"body": comment[:4000]}, timeout=30).raise_for_status()
    resp = httpx.patch(f"{settings.GITHUB_API_URL}/repos/{full_name}/pulls/{number}",
                       headers=_headers(token), json={"state": "closed"}, timeout=30)
    resp.raise_for_status()
    return {"closed": True}


def update_pull(*, token: str, full_name: str, number: int, title: str, body: str) -> dict:
    """Refresh title/body of an existing PR (reused standing-branch PR)."""
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/pulls/{number}"
    resp = httpx.patch(url, headers=_headers(token),
                       json={"title": title, "body": body}, timeout=30)
    resp.raise_for_status()
    return {"number": number}


def list_installation_repos(*, token: str, limit: int = 100) -> list[dict]:
    """Repos visible to an installation token. Raises on API failure."""
    url = f"{settings.GITHUB_API_URL}/installation/repositories"
    resp = httpx.get(url, headers=_headers(token),
                     params={"per_page": min(max(limit, 1), 100)}, timeout=30)
    resp.raise_for_status()
    return [
        {"full_name": r.get("full_name", ""),
         "private": bool(r.get("private", False)),
         "default_branch": r.get("default_branch") or "main"}
        for r in resp.json().get("repositories", [])
    ][:limit]


def failing_steps(*, token: str, full_name: str, run_id: str) -> list[dict]:
    """Failed steps across failed jobs: [{job, step, conclusion}]. Raises on API failure."""
    jobs_url = f"{settings.GITHUB_API_URL}/repos/{full_name}/actions/runs/{run_id}/jobs"
    resp = httpx.get(url=jobs_url, headers=_headers(token), timeout=30)
    resp.raise_for_status()
    out = []
    for job in resp.json().get("jobs", []):
        if job.get("conclusion") != "failure":
            continue
        for step in job.get("steps") or []:
            if step.get("conclusion") == "failure":
                out.append({"job": job.get("name", ""), "job_id": job.get("id"),
                            "step": step.get("name", ""), "conclusion": "failure"})
    return out[:10]


def check_annotations(*, token: str, full_name: str, check_run_id: int | str, limit: int = 10) -> list[dict]:
    """Check-run annotations (often carry the exact error + file/line). Raises on API failure."""
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/check-runs/{check_run_id}/annotations"
    resp = httpx.get(url, headers=_headers(token), timeout=30)
    resp.raise_for_status()
    out = []
    for a in resp.json()[:limit]:
        out.append({"path": a.get("path", ""), "line": a.get("start_line"),
                    "level": a.get("annotation_level", ""),
                    "message": str(a.get("message", ""))[:500]})
    return out


def get_workflow_content(*, token: str, full_name: str, path: str, ref: str = "",
                         max_bytes: int = 8000) -> str:
    """Fetch a repo file (workflow yaml) via contents API, base64-decoded, capped."""
    import base64

    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/contents/{path.lstrip('/')}"
    params = {"ref": ref} if ref else {}
    resp = httpx.get(url, headers=_headers(token), params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("content", "") or ""
    try:
        text = base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:
        return ""
    return text[:max_bytes]


def repo_dir_contents(*, token: str, full_name: str, path: str = ".", ref: str = "",
                     limit: int = 100) -> list[dict]:
    """List one repo directory via the contents API: [{name, type, size, sha}].

    `ref` is a branch/sha (default branch when empty). Raises ValueError when
    the path is a file, httpx errors on API failure.
    """
    p = (path or ".").strip().strip("/")
    suffix = "" if p in ("", ".") else f"/{p}"
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/contents{suffix}"
    params = {"ref": ref} if ref else {}
    resp = httpx.get(url, headers=_headers(token), params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"{path} is a file, not a directory")
    out = []
    for it in data:
        if it.get("type") not in ("file", "dir"):
            continue
        out.append({"name": it.get("name", ""), "type": it.get("type", ""),
                    "size": it.get("size", 0), "sha": it.get("sha", "")})
    return out[:limit]


def repo_file_content(*, token: str, full_name: str, path: str, ref: str = "",
                      max_bytes: int = 200000) -> dict:
    """Fetch one repo file via the contents API: {content, truncated, binary, size}.

    `ref` is a branch/sha (default branch when empty). Raises ValueError when
    the path is a directory, httpx errors on API failure.
    """
    import base64

    p = (path or "").strip().strip("/")
    if not p or p == ".":
        raise ValueError("path is required")
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/contents/{p}"
    params = {"ref": ref} if ref else {}
    resp = httpx.get(url, headers=_headers(token), params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        raise ValueError(f"{path} is a directory, not a file")
    raw = base64.b64decode(data.get("content") or "")
    size = len(raw)
    truncated = size > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    binary = b"\x00" in raw
    return {"content": "" if binary else raw.decode("utf-8", errors="replace"),
            "truncated": truncated, "binary": binary, "size": size}


def pull_diff(*, token: str, full_name: str, number: int,
              max_bytes: int = 200000) -> dict:
    """Unified diff of a PR via the raw-diff media type: {diff, truncated}.

    Raises on API failure.
    """
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/pulls/{number}"
    headers = {**_headers(token), "Accept": "application/vnd.github.diff"}
    resp = httpx.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    text = resp.text or ""
    truncated = len(text) > max_bytes
    return {"diff": text[:max_bytes], "truncated": truncated}


def list_issues(*, token: str, full_name: str, state: str = "open", limit: int = 50) -> list[dict]:
    """List issues (excluding PRs) for a repo using an installation token."""
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/issues"
    resp = httpx.get(
        url,
        headers=_headers(token),
        params={"state": state, "per_page": min(max(limit, 1), 100)},
        timeout=30,
    )
    resp.raise_for_status()
    items = resp.json()
    out = []
    for it in items:
        if "pull_request" in it:
            continue
        out.append(
            {
                "number": it.get("number"),
                "title": it.get("title", ""),
                "body": (it.get("body") or "")[:8000],
                "url": it.get("html_url", ""),
                "labels": [l.get("name", "") for l in (it.get("labels") or [])],
                "comments": it.get("comments", 0),
            }
        )
    return out[:limit]


def get_issue(*, token: str, full_name: str, number: int) -> dict:
    """Fetch one issue (excluding PRs) using an installation token."""
    url = f"{settings.GITHUB_API_URL}/repos/{full_name}/issues/{number}"
    resp = httpx.get(url, headers=_headers(token), timeout=30)
    resp.raise_for_status()
    it = resp.json()
    if "pull_request" in it:
        raise ValueError(f"#{number} is a pull request, not an issue")
    return {
        "number": it.get("number", number),
        "title": it.get("title", ""),
        "body": (it.get("body") or "")[:8000],
        "url": it.get("html_url", ""),
        "labels": [l.get("name", "") for l in (it.get("labels") or [])],
    }


def list_installations(*, app_jwt: str) -> list[dict]:
    """List App installations (id + account) using the App JWT."""
    url = f"{settings.GITHUB_API_URL}/app/installations"
    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    resp.raise_for_status()
    out = []
    for inst in resp.json():
        acct = inst.get("account") or {}
        out.append(
            {"id": str(inst.get("id", "")), "account": acct.get("login", ""), "type": acct.get("type", "")}
        )
    return out
