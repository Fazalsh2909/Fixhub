"""Manifest-based guest<->host reconciliation primitives (pure, host-side).

Stage 2 of the workspace sync contract rewrite (production incident fix for
tasks #70/#71): the previous best-effort `sync_guest_to_host()` copied the
whole guest tree and silently skipped every read error, guest error response,
and host write refusal (`firecracker.py:1361-1372`), so agent edits could
vanish from the trusted host workspace while the run still reported success
(`VM_SYNCED files=N` for a tree whose edited `.github/workflows/ci.yml` never
reached the host diff).

This module holds the PURE parts of the replacement contract so they are
deterministically testable without a guest VM:

- `ManifestEntry` / `parse_manifest()`: validate the guest agent's `manifest`
  op response (regular files only: rel path + size + sha256).
- `sha256_bytes()`: the digest every transferred file is verified against —
  write-side verification, not "count of bytes moved".
- `sync_state_path()` / `load_sync_state()` / `save_sync_state()`: a
  JSON snapshot of the last fully-verified sync, stored as a SIBLING of the
  workspace (never inside the task dir, so git status stays clean). Deletion
  reconciliation is only ever computed against this snapshot.
- `compute_deletions()`: which previously-synced files no longer exist in the
  guest manifest. Callers MUST gate deletions on an UNTRUNCATED manifest and
  must re-validate every candidate path (traversal/symlink/sensitive) before
  deleting — this module enforces the exclusion side (`is_sensitive`), the
  caller enforces the filesystem side.

Security invariants (never relax):
- Sensitive paths (env/keys/secrets/credentials), `.git` internals, and
  `__pycache__` are excluded from manifests, transfers, and deletions; the
  guest agent excludes them too (defense in depth).
- Truncation is NEVER silently upgraded to success; a truncated manifest can
  reconciled additions/changes but MUST NOT drive deletions.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Iterable

from app.agent.paths import is_sensitive as _is_sensitive

SYNC_STATE_DIR = ".fixhub-sync"

# Guest manifest entry schema: {"path": str, "size": int, "sha256": <64 hex>}.
_HEX = set("0123456789abcdef")


class ManifestInvalid(ValueError):
    """Guest manifest payload failed schema validation. Fail closed."""


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str


def sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _validate_rel_path(path: object) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ManifestInvalid("manifest entry path missing")
    rel = path.replace("\\", "/").strip()
    if rel.startswith("/") or rel.startswith("~"):
        raise ManifestInvalid(f"absolute manifest path: {rel[:200]}")
    if "\x00" in rel:
        raise ManifestInvalid("manifest path contains NUL byte")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ManifestInvalid(f"manifest path escapes workspace: {rel[:200]}")
    return "/".join(parts)


def _validate_entry(raw: object) -> ManifestEntry:
    if not isinstance(raw, dict):
        raise ManifestInvalid("manifest entry must be an object")
    rel = _validate_rel_path(raw.get("path"))
    if _is_sensitive(rel) or _is_sensitive(rel.split(":")[0]):
        # The guest agent already excludes these; a guest that still sent one
        # is either tampered or misbehaving. Never transfer, never delete.
        raise ManifestInvalid(f"manifest includes sensitive path: {rel[:200]}")
    size = raw.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ManifestInvalid(f"invalid size for manifest entry: {rel[:200]}")
    digest = raw.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in _HEX for ch in digest)
    ):
        raise ManifestInvalid(f"invalid digest for manifest entry: {rel[:200]}")
    return ManifestEntry(path=rel, size=int(size), sha256=digest)


def parse_manifest(payload: dict) -> tuple[list[ManifestEntry], bool]:
    """Validate a guest `manifest` op response. Returns (entries, truncated).

    Raises ManifestInvalid with the offending detail; callers must fail the
    whole sync (never clamp a mismatching guest response into a partial one).
    """
    if not isinstance(payload, dict):
        raise ManifestInvalid("manifest response must be an object")
    if payload.get("error"):
        raise ManifestInvalid(f"manifest op failed: {str(payload['error'])[:300]}")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ManifestInvalid("manifest response has no entries list")
    entries = [_validate_entry(raw) for raw in raw_entries]
    truncated = bool(payload.get("truncated", False))
    return entries, truncated


def sync_state_path(workspace: str, task_id: int | str) -> str:
    """Snapshot JSON path for a task: a sibling of the workspace dir.

    `<WORKSPACE_ROOT>/.fixhub-sync/task-<id>.json` — outside every task git
    tree, so it can never appear in `git status`, and survives workspace
    re-creation across repair rounds.
    """
    root = os.path.dirname(os.path.abspath(workspace))
    task_name = os.path.basename(os.path.abspath(workspace)) or f"task-{int(task_id)}"
    safe = task_name.replace("\\", "_").replace("/", "_")
    return os.path.join(root, SYNC_STATE_DIR, f"{safe}.json")


def load_sync_state(path: str) -> dict[str, ManifestEntry]:
    """Load the last fully-verified sync snapshot. Missing/corrupt -> empty."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return {}
    try:
        return {e.path: e for e in (_validate_entry(r) for r in entries)}
    except ManifestInvalid:
        return {}  # corrupt prior state: deletions become "unknown", i.e. none


def save_sync_state(path: str, entries: Iterable[ManifestEntry]) -> None:
    """Persist the verified manifest snapshot atomically (same-dir rename)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "entries": [
            {"path": e.path, "size": e.size, "sha256": e.sha256}
            for e in sorted(entries, key=lambda x: x.path)
        ]
    }
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def compute_deletions(
    manifest: Iterable[ManifestEntry], state: dict[str, ManifestEntry]
) -> list[str]:
    """Files present in the last verified sync but absent from the manifest now.

    Only ever called with an UNTRUNCATED manifest (caller's duty): a truncated
    listing cannot distinguish "guest deleted it" from "cap cut the walk".
    The result carries no filesystem authority — caller re-validates each
    path before touching anything.
    """
    now = {e.path for e in manifest}
    return sorted(p for p in state if p not in now)
