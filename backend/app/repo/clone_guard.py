"""Clone-any-OSS guards: only public github.com owner/repo, safe clone, index."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

_OWNER_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+?(\.git)?$")


def validate_github_url(url: str) -> tuple[str, str]:
    """Validate a public GitHub repo URL. Returns (owner, repo). Raises ValueError."""
    u = (url or "").strip()
    if not u:
        raise ValueError("empty url")
    p = urlparse(u)
    if p.scheme != "https" or p.hostname != "github.com":
        raise ValueError("only https://github.com/<owner>/<repo> is allowed")
    if p.username or p.password or "@" in (p.netloc or ""):
        raise ValueError("credentials in URL are not allowed")
    parts = [x for x in p.path.strip("/").split("/") if x]
    if len(parts) != 2:
        raise ValueError("URL must be exactly https://github.com/<owner>/<repo>")
    owner, repo = parts
    if not _OWNER_RE.match(owner) or not _REPO_RE.match(repo):
        raise ValueError("invalid owner/repo characters")
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    return owner, repo


def clone_shallow(owner: str, repo: str, dest: Path, timeout: int = 180) -> Path:
    """Shallow-clone into dest/<owner>__<repo>. Raises on git failure/timeout."""
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"{owner}__{repo}"
    if target.exists():
        # Refresh existing clone cheaply; never fail the request on fetch issues.
        try:
            subprocess.run(
                ["git", "-C", str(target), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception:
            pass
        return target
    p = subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            f"https://github.com/{owner}/{repo}.git",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if p.returncode != 0:
        raise RuntimeError(f"clone failed: {(p.stderr or p.stdout)[-500:]}")
    return target
