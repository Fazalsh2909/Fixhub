"""Structured workspace changes derived from real Git state.

The Explorer's Files/Changes views and the per-file diff endpoint both read
from here, so the UI can never disagree with `git status` or with what the
publisher is about to commit.

Parsing rules:
- `git status --porcelain=v1 -z --untracked-files=all` (NUL separated, so
  spaces and unusual characters in paths are unambiguous);
- porcelain v1 -z emits the rename target first and the source second;
- only four user-facing statuses exist: modified, added, deleted, renamed.
"""
from __future__ import annotations

import os
import re
import subprocess

from app.agent.paths import is_sensitive as _is_sensitive

STATUSES = ("modified", "added", "deleted", "renamed")

# Interpreter bytecode must never appear (same exclusion as the publisher),
# and git internals are never browsable.
_EXCLUDED_PATH = re.compile(r"(^|/)(__pycache__|\.git)(/|$)|\.py[co]$", re.IGNORECASE)

_DIFF_MAX_CHARS = 30_000
_CHANGE_LIMIT = 500

_GITHUB_STATUS = {
    "added": "added",
    "removed": "deleted",
    "deleted": "deleted",
    "renamed": "renamed",
    "modified": "modified",
    "changed": "modified",
    "copied": "added",
    "unchanged": "modified",
}


class GitStateError(RuntimeError):
    """`git status` failed for a workspace that should be a repository."""


def is_excluded(path: str) -> bool:
    """True when a path must never surface in changes or diffs."""
    clean = (path or "").replace("\\", "/").strip()
    if not clean:
        return True
    if _EXCLUDED_PATH.search(clean):
        return True
    return _is_sensitive(clean)


def normalize_github_status(raw: str) -> str:
    return _GITHUB_STATUS.get((raw or "").strip().lower(), "modified")


def counts_of(changes: list[dict]) -> dict:
    counts = {status: 0 for status in STATUSES}
    for entry in changes:
        status = entry.get("status")
        if status in counts:
            counts[status] += 1
    counts["total"] = len(changes)
    return counts


def _classify(code: str) -> str:
    index, worktree = (code + "  ")[:2]
    if index == "?" and worktree == "?":
        return "added"
    if "R" in (index, worktree):
        return "renamed"
    if "D" in (index, worktree):
        return "deleted"
    if "A" in (index, worktree):
        return "added"
    if "C" in (index, worktree):
        return "added"
    return "modified"


def _parse_status(raw: bytes) -> list[dict]:
    tokens = (raw or b"").split(b"\x00")
    changes: list[dict] = []
    i = 0
    while i < len(tokens):
        record = tokens[i]
        i += 1
        if not record:
            continue
        text = record.decode("utf-8", "surrogateescape")
        if len(text) < 4:
            continue
        code, path = text[:2], text[3:]
        previous = None
        if "R" in code or "C" in code:
            if i < len(tokens):
                previous = tokens[i].decode("utf-8", "surrogateescape")
                i += 1
        if not path:
            continue
        changes.append(
            {
                "path": path.replace("\\", "/"),
                "status": _classify(code),
                "previous_path": (
                    previous.replace("\\", "/") if previous and "R" in code else None
                ),
            }
        )
    return changes


def git_changes(workspace: str, limit: int = _CHANGE_LIMIT) -> dict:
    """Current workspace changes: `{changes, counts}` from `git status`."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=workspace,
            capture_output=True,
            timeout=30,
        )
    except Exception as exc:  # pragma: no cover - defensive (missing git binary)
        raise GitStateError(f"git status failed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        raise GitStateError(f"git status failed: {detail}")
    entries = [c for c in _parse_status(proc.stdout) if not is_excluded(c["path"])]
    # A file can appear twice (staged + unstaged records are one record), but a
    # rename target that is also listed separately must win over a stale entry.
    deduped: dict[str, dict] = {}
    for entry in entries:
        existing = deduped.get(entry["path"])
        if existing is None or (existing["status"] != "renamed" and entry["status"] == "renamed"):
            deduped[entry["path"]] = entry
    changes = sorted(deduped.values(), key=lambda c: c["path"].lower())[:limit]
    return {"changes": changes, "counts": counts_of(changes)}


def _run_diff(workspace: str, args: list[str]) -> tuple[str, int]:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "diff", "--no-color", "--no-ext-diff", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise GitStateError(f"git diff failed: {exc}") from exc
    # `--no-index` exits 1 whenever the two inputs differ; that is success here.
    if proc.returncode not in (0, 1):
        detail = ((proc.stderr or "") + (proc.stdout or ""))[-300:]
        raise GitStateError(f"git diff failed: {detail}")
    return proc.stdout or "", proc.returncode


def file_diff(
    workspace: str,
    path: str,
    *,
    status: str | None = None,
    previous_path: str | None = None,
    max_chars: int = _DIFF_MAX_CHARS,
) -> dict:
    """Unified diff for ONE changed path (never the whole task diff).

    Untracked files are invisible to `git diff HEAD`; they are diffed against
    the platform null device so a brand-new file still gets a real patch.
    Deleted files diff HEAD against the missing worktree path.
    """
    normalized = (path or "").replace("\\", "/")
    if status is None:
        match = next(
            (c for c in git_changes(workspace)["changes"] if c["path"] == normalized),
            None,
        )
        if match is None:
            raise KeyError(normalized)
        status = match["status"]
        previous_path = match["previous_path"]

    targets = [normalized]
    if status == "renamed" and previous_path:
        targets.append(previous_path)

    untracked = False
    if status == "added":
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", normalized],
            cwd=workspace,
            capture_output=True,
            timeout=30,
        )
        untracked = tracked.returncode != 0

    if untracked:
        text, _ = _run_diff(workspace, ["--no-index", "--", os.devnull, normalized])
    else:
        text, _ = _run_diff(workspace, ["HEAD", "-M", "--", *targets])

    binary = "Binary files" in text or "GIT binary patch" in text
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]..."
    return {
        "path": normalized,
        "status": status,
        "previous_path": previous_path,
        "diff": text,
        "binary": binary,
        "truncated": truncated,
        "message": (
            "Binary file — no text diff available."
            if binary
            else ("No diff produced for this file." if not text else "")
        ),
    }


def from_github_files(files: list[dict], limit: int = _CHANGE_LIMIT) -> dict:
    """Map GitHub pull-request file entries to the same `{changes, counts}`."""
    changes: list[dict] = []
    for item in (files or [])[:limit]:
        path = str(item.get("path") or "").replace("\\", "/")
        if not path or is_excluded(path):
            continue
        status = normalize_github_status(str(item.get("status") or "modified"))
        previous = item.get("previous_path") or None
        changes.append(
            {
                "path": path,
                "status": status,
                "previous_path": previous if status == "renamed" else None,
            }
        )
    changes.sort(key=lambda c: c["path"].lower())
    return {"changes": changes, "counts": counts_of(changes)}


def patch_text(entry: dict) -> str:
    """Rendered diff text for a GitHub file entry (or '' when unavailable)."""
    patch = entry.get("patch") or ""
    if not str(patch).strip():
        return ""
    header_path = entry.get("path") or ""
    previous = entry.get("previous_path") or header_path
    return f"--- a/{previous}\n+++ b/{header_path}\n{patch}"
