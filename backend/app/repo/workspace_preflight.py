"""Staging preflight for the publish path (fail closed, point at the cause).

Tasks #70/#71 reached `git add` in the publisher and failed with
``open("CONTRIBUTING.md"): Permission denied`` — the ONLY signal was git's
stderr. Root cause: workspace files/dirs had been created with mode
0600/0700 by the sync's temp-file writer, so the publisher process identity
could not read the worktree at all.

This module re-checks, in-process and BEFORE any git call, that this
process identity can actually:

- search every ancestor directory of each file to stage (traversal bit);
- open each target file for reading (1 byte — content is never kept);
- write the git bookkeeping (``.git/index`` + loose objects) it needs.

Diagnostics name the path, the observed permission bits, and the owner
(uid/gid). NEVER file content. `safe.directory` is deliberately NOT the
fix for this class of failure — these workspaces are created by this
service identity stack (explicit WORKSPACE_DIR_MODE / WORKSPACE_FILE_MODE
and the atomic writer's mode discipline); a missing permission bit is a
real misconfiguration and must surface as one actionable sentence, not a
cryptic git subprocess error half-way through staging.

`preflight_staging()` returns the list of blocking failures (empty list =
safe to stage); `PreflightFailure` wraps the same list when callers prefer
to raise.
"""

from __future__ import annotations

import os
import stat as _stat

__all__ = ["PreflightFailure", "preflight_staging", "preflight_workspace_root"]


class PreflightFailure(RuntimeError):
    """Preflight refused staging; `.failures` names each cause (no content)."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = list(failures)
        super().__init__("; ".join(self.failures[:8]))


def _fmt_mode(mode: int) -> str:
    """`stat.filemode`-style rwx triplets (owner/group/other) for logs."""
    bits = _stat.S_IMODE(mode)
    out = ""
    for shift, letters in ((6, "rwx"), (3, "rwx"), (0, "rwx")):
        seg = (bits >> shift) & 0o7
        for i, ch in enumerate(letters):
            out += ch if seg & (1 << i) else "-"
    return out


def _describe(st: os.stat_result) -> str:
    text = f"uid={st.st_uid} gid={st.st_gid} mode={_fmt_mode(st.st_mode)}"
    try:
        import pwd  # POSIX only; never allowed to fail the check

        text += f" user={pwd.getpwuid(st.st_uid).pw_name}"
    except Exception:
        pass
    return text


def _traversable(path: str) -> bool:
    """Can THIS process search inside `path`? (git add reads the worktree;
    it must not be asked to probe-create files in it.)"""
    return os.access(path, os.R_OK | os.X_OK)


def preflight_staging(
    path: str,
    rels: "list[str] | tuple[str, ...]" = (),
    *,
    raise_on_failure: bool = False,
) -> list[str]:
    """Validate that this process can stage `rels` inside workspace `path`.

    Checks, each failing with path + stage + mode/owner (never content):
    1. workspace dir exists and is searchable;
    2. `.git/HEAD` readable, `.git/index` openable for writing;
    3. `.git/objects` writable (proved by a one-file probe — git add writes
       loose object files there);
    4. every ancestor directory (workspace root down to the file's parent)
       is searchable by this process;
    5. every EXISTING target file opens read-only. Missing files are NOT
       failures here: the pipeline's declared-edit verification owns the
       "agent edit never reached the host" case; this gate exists so that
       files which DO exist cannot fail `git add` with only git's stderr
       as the explanation.
    """
    failures: list[str] = []
    abspath = os.path.abspath(path)

    def _add(message: str) -> None:
        if message not in failures:
            failures.append(message)

    # 1. Workspace itself.
    try:
        ws_st = os.stat(abspath)
    except OSError as exc:
        failures.append(f"workspace: stat failed ({exc})")
        if raise_on_failure:
            raise PreflightFailure(failures)
        return failures
    if not _stat.S_ISDIR(ws_st.st_mode):
        failures.append(f"workspace: not a directory: {abspath}")
        if raise_on_failure:
            raise PreflightFailure(failures)
        return failures
    if not _traversable(abspath):
        _add(f"workspace: not searchable by this process: {abspath} [{_describe(ws_st)}]")

    # 2/3. Git bookkeeping. A workspace WITHOUT a real `.git` on disk is
    # not one produced by this stack (tests monkeypatch `_git` on empty
    # trees; the real pipeline always clones first) — skip the git-side
    # checks and defer to git itself; the workspace + file-readability
    # checks below stay live on real files.
    gitdir = os.path.join(abspath, ".git")
    if not os.path.isdir(gitdir):
        pass  # no real .git on disk: defer git-side checks to git itself
    else:
        head = os.path.join(gitdir, "HEAD")
        try:
            head_st = os.stat(head)
        except OSError as exc:
            _add(f".git/HEAD: missing ({exc}) [{abspath}]")
        else:
            try:
                with open(head, "rb") as fh:
                    fh.read(1)
            except OSError as exc:
                _add(f".git/HEAD: not openable by this process ({exc}) [{_describe(head_st)}]")
        index = os.path.join(gitdir, "index")
        try:
            with open(index, "r+b"):
                pass
        except FileNotFoundError:
            pass  # pre-first-add workspace: `git add` creates the index
        except OSError as exc:
            try:
                idx_st = os.stat(index)
                detail = f"[{_describe(idx_st)}]"
            except OSError:
                detail = ""
            _add(f".git/index: not writable by this process ({exc}) {detail}")
        objects = os.path.join(gitdir, "objects")
        try:
            obj_st = os.stat(objects)
        except OSError as exc:
            _add(f".git/objects: missing ({exc}) [{abspath}]")
            obj_st = None
        if obj_st is not None and not _stat.S_ISDIR(obj_st.st_mode):
            _add(f".git/objects: not a directory [{_describe(obj_st)}]")
        else:
            probe = os.path.join(objects, ".fixhub-preflight-probe")
            try:
                with open(probe, "wb") as fh:
                    fh.write(b"probe")
            except OSError as exc:
                _add(f".git/objects: probe write failed ({exc}) [{abspath}]")
            finally:
                try:
                    os.unlink(probe)
                except OSError:
                    pass
            if obj_st is not None and not _traversable(objects):
                _add(f".git/objects: not searchable by this process [{_describe(obj_st)}]")

    # 4/5. Target ancestry + file readability.
    for rel in rels or ():
        rel_norm = str(rel).replace("\\", "/").strip("/")
        parts = [p for p in rel_norm.split("/") if p and p != "."]
        if not parts:
            continue
        if any(p == ".." for p in parts):
            _add(f"{rel_norm}: refuses traversal-out path")
            continue
        node = os.path.join(abspath, *parts)
        try:
            st_file = os.stat(node)
        except FileNotFoundError:
            continue  # declared-edit verifier owns the missing-file case
        except OSError as exc:
            _add(f"{rel_norm}: stat failed ({exc})")
            continue
        # Ancestors, walking from the file's parent UP TO the workspace
        # (never above it: dirs above are the operator's environment).
        walker = os.path.join(abspath, *parts[:-1]) if len(parts) > 1 else abspath
        while True:
            try:
                st_dir = os.stat(walker)
            except OSError as exc:
                _add(f"{rel_norm}: ancestor stat failed ({exc}) [{walker}]")
                break
            if not _traversable(walker):
                _add(
                    f"{rel_norm}: cannot traverse ancestor dir [{_describe(st_dir)}]"
                    " — publisher needs r+x (search) permission on it"
                )
            try:
                if st_dir.st_dev == ws_st.st_dev and st_dir.st_ino == ws_st.st_ino:
                    break  # reached the workspace root: stop above it
            except AttributeError:  # Windows st_ino quirk fallback
                if os.path.abspath(walker) == abspath:
                    break
            walker = os.path.dirname(walker)
        if _stat.S_ISDIR(st_file.st_mode):
            continue  # directory targets need no file-open check
        try:
            with open(node, "rb") as fh:
                fh.read(1)
        except OSError as exc:
            _add(f"{rel_norm}: not openable by this process ({exc}) [{_describe(st_file)}]")

    if raise_on_failure and failures:
        raise PreflightFailure(failures)
    return failures


def preflight_workspace_root(
    root: str = "", *, raise_on_failure: bool = False
) -> list[str]:
    """Non-destructive identity checks on the workspace ROOT itself.

    Unlike `preflight_staging()`, a MISSING root is fine (bootstrap creates
    it with `WORKSPACE_DIR_MODE`); only an EXISTING root with unusable
    permissions fails. Checked at process boot (prod/staging refuse to
    start) and before each task's clone: the root must be searchable AND
    writable by this process identity, because task dirs — and therefore
    the repo clones the publisher later `git add`s — are created inside.
    This is where the tasks #70/#71 class of failure gets its ONE pointed
    sentence instead of surfacing as publish-time Permission denied.
    """
    from app.config import settings as _settings

    root = root or str(getattr(_settings, "WORKSPACE_ROOT", "./workspaces"))
    abspath = os.path.abspath(root)
    failures: list[str] = []
    try:
        st = os.stat(abspath)
    except FileNotFoundError:
        return failures  # first boot: mkdir creates it explicitly
    except OSError as exc:
        failures.append(f"workspace root: stat failed ({exc}) [{abspath}]")
        if raise_on_failure:
            raise PreflightFailure(failures)
        return failures
    if not _stat.S_ISDIR(st.st_mode):
        failures.append(f"workspace root: not a directory [{abspath}] {_describe(st)}")
    else:
        if not os.access(abspath, os.R_OK | os.X_OK):
            failures.append(
                f"workspace root: not searchable by this process [{abspath}] "
                f"{_describe(st)}"
            )
        probe = os.path.join(abspath, ".fixhub-preflight-probe")
        try:
            with open(probe, "wb") as fh:
                fh.write(b"probe")
        except OSError as exc:
            failures.append(
                f"workspace root: not writable by this process ({exc}) "
                f"{_describe(st)}"
            )
        finally:
            try:
                os.unlink(probe)
            except OSError:
                pass
    failures = list(dict.fromkeys(failures))
    if raise_on_failure and failures:
        raise PreflightFailure(failures)
    return failures
