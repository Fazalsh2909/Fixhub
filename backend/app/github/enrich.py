"""Issue context enrichment: turn bare references into real content.

The product promise is "issue filed → agent fixes it, no instructions
needed". That is impossible when the agent only receives a URL or
"fix #N" — so FixHub resolves GitHub references server-side and injects
the fetched title/body/comments into the prompt (context, not control).

Resolves, in title+body text:
- full issue URLs: https://github.com/{owner}/{repo}/issues/{n}
- cross-repo refs: {owner}/{repo}#{n}
- same-repo refs: #{n} (needs default_repo)

Everything is fail-open: any fetch error skips that source, never the run.
Total output is capped so the prompt stays compact.
"""

from __future__ import annotations

import re

_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)"
)
_CROSS_RE = re.compile(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)")
_SAME_RE = re.compile(r"(?<![A-Za-z0-9_./-])#(\d+)")

MAX_REFS = 3
MAX_COMMENTS = 5
MAX_TOTAL_CHARS = 2500


def parse_references(text: str, default_repo: str = "") -> list[tuple[str, int]]:
    """Extract [(full_name, number)] refs, deduped, order-preserved."""
    found: list[tuple[str, int]] = []

    def _add(full: str, num: int) -> None:
        key = (full.strip(), int(num))
        if key not in found and len(found) < MAX_REFS:
            found.append(key)

    for m in _URL_RE.finditer(text or ""):
        _add(f"{m.group(1)}/{m.group(2)}", int(m.group(3)))
    for m in _CROSS_RE.finditer(text or ""):
        _add(f"{m.group(1)}/{m.group(2)}", int(m.group(3)))
    if (default_repo or "").strip() and "/" in default_repo:
        for m in _SAME_RE.finditer(text or ""):
            _add(default_repo.strip(), int(m.group(1)))
    return found


def _snip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _format_issue(full_name: str, number: int, issue: dict) -> str:
    title = _snip(str(issue.get("title", "")), 200)
    body = _snip(str(issue.get("body", "")), 900)
    parts = [f"--- Linked issue {full_name}#{number}: {title}"]
    if body:
        parts.append(body)
    comments = issue.get("_comments") or []
    for c in comments[:MAX_COMMENTS]:
        author = str((c or {}).get("author", ""))
        cbody = _snip(str((c or {}).get("body", "")), 300)
        if cbody:
            parts.append(f"Comment by {author}: {cbody}")
    return "\n".join(parts)


def enrich_issue_context(
    *,
    repo_full_name: str = "",
    issue_number: int = 0,
    title: str = "",
    body: str = "",
    installation_id: str = "",
    _client_factory=None,
) -> str:
    """Fetch primary + linked issue content. Returns "" when nothing found.

    _client_factory is a test seam: (installation_id) -> client with
    get_issue(full_name, number) and get_issue_comments(full_name, number).
    """
    text = f"{title or ''}\n{body or ''}"
    refs = parse_references(text, repo_full_name)
    # Always include the primary issue itself when we know its number.
    primary = None
    if issue_number and (repo_full_name or "").strip():
        primary = (repo_full_name.strip(), int(issue_number))
        if primary not in refs:
            refs.insert(0, primary)
    if not refs:
        return ""

    client = None
    if (installation_id or "").strip() and _client_factory is not None:
        try:
            client = _client_factory(installation_id.strip())
        except Exception:
            client = None
    if client is None and _client_factory is None:
        try:
            from .app_auth import get_installation_token
            from .read_client import GitHubReadClient

            if (installation_id or "").strip():
                client = GitHubReadClient(
                    get_installation_token(installation_id.strip())
                )
        except Exception:
            client = None

    chunks: list[str] = []
    for full_name, number in refs[: MAX_REFS + 1]:
        try:
            issue = _fetch_issue(client, full_name, number)
        except Exception:
            continue
        if not issue:
            continue
        chunks.append(_format_issue(full_name, number, issue))
        if sum(len(c) for c in chunks) >= MAX_TOTAL_CHARS:
            break
    out = "\n\n".join(chunks)
    if len(out) > MAX_TOTAL_CHARS:
        out = out[:MAX_TOTAL_CHARS] + "\n…[context truncated]"
    return out


def _fetch_issue(client, full_name: str, number: int) -> dict | None:
    """Fetch one issue + comments via client, else unauthenticated public GET."""
    if client is not None:
        try:
            issue = client.get_issue(full_name, number)
            if not isinstance(issue, dict):
                return None
            try:
                comments = client.get_issue_comments(full_name, number)
            except Exception:
                comments = []
            issue = dict(issue)
            issue["_comments"] = [
                {
                    "author": str((c or {}).get("user", {}).get("login", "")),
                    "body": str((c or {}).get("body", "")),
                }
                for c in (comments or [])
                if isinstance(c, dict)
            ][:MAX_COMMENTS]
            return issue
        except Exception:
            return None
    # No installation: best-effort public fetch (rate-limited, fail-open).
    try:
        import httpx

        with httpx.Client(timeout=15) as c:
            r = c.get(
                f"https://api.github.com/repos/{full_name}/issues/{number}",
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            issue = r.json()
            if not isinstance(issue, dict):
                return None
            try:
                rc = c.get(
                    f"https://api.github.com/repos/{full_name}/issues/{number}/comments",
                    headers={"Accept": "application/vnd.github+json"},
                )
                rc.raise_for_status()
                comments = rc.json()
            except Exception:
                comments = []
            issue = dict(issue)
            issue["_comments"] = [
                {
                    "author": str((c or {}).get("user", {}).get("login", "")),
                    "body": str((c or {}).get("body", "")),
                }
                for c in (comments or [])
                if isinstance(c, dict)
            ][:MAX_COMMENTS]
            return issue
    except Exception:
        return None
