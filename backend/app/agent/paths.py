"""Single authoritative path resolver for the task workspace.

Both the agent tool layer (`agent/tools.py`) and the IDE API (`api/ide.py`)
resolve through here so the jail cannot drift. The sandbox uses it for
command working directories.
"""
from __future__ import annotations

import os
import re

_SENSITIVE = re.compile(
    r"(^|/)(\.env(\..*)?|\.git/.*|.*\.pem$|.*\.key$|.*secret.*|.*token.*|.*credentials.*)$",
    re.IGNORECASE,
)


def resolve(workspace: str, rel: str) -> str:
    """Resolve a repository-relative path against the workspace root.

    Accepts only relative paths inside the workspace. Rejects empty input,
    NUL bytes, absolute paths, home-directory shorthand, Windows drive specs,
    and any traversal (`..`) escaping the root. Returns the canonical path.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("path is required and must be non-empty")
    if "\x00" in rel:
        raise ValueError("path contains NUL byte")
    if os.path.isabs(rel):
        raise ValueError(
            "absolute paths are not allowed: use the repository-relative path instead "
            "(workspace root is implicit, never prepend it)")
    if rel.startswith("~"):
        raise ValueError("home-directory paths are not allowed: use repository-relative paths")
    if re.match(r"^[A-Za-z]:", rel):
        raise ValueError("drive-letter paths are not allowed: use repository-relative paths")
    norm = os.path.normpath(rel)
    if norm.startswith("..") or norm == ".." or os.path.isabs(norm):
        raise ValueError(f"path escapes workspace: {rel[:200]}")
    full = os.path.normpath(os.path.join(os.path.abspath(workspace), norm))
    if full != os.path.abspath(workspace) and not full.startswith(os.path.abspath(workspace) + os.sep):
        raise ValueError(f"path escapes workspace: {rel[:200]}")
    return full


def is_sensitive(rel: str) -> bool:
    return bool(_SENSITIVE.search((rel or "").replace(os.sep, "/")))
