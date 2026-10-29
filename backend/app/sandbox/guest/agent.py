"""Phase 5 guest agent (runs INSIDE the microVM rootfs — stdlib only).

Baked into the base rootfs image (see infra/firecracker/build-rootfs.sh) and
started by guest init on vsock port 5000. Speaks the length-prefixed JSON
frame protocol defined in app/sandbox/guest_agent.py (host side).

Security properties (guest side, defense in depth — host re-validates too):
- repo-relative paths only (absolute, drive-letter, NUL, `..` escapes rejected);
- sensitive names (.env, .git/config with tokens, *.pem/*.key, *secret*,
  *token*, *credentials*) blocked for read/write/list;
- argv exec (no shell) preferred; exec_shell is a single explicit `sh -c`
  string with wall timeout + output cap + process-group kill;
- NO secrets, NO network policy, NO credentials in this process: the guest
  has no tokens by construction (host strips them before transfer).

This file must NOT import app.* (it ships inside the guest image).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess

PORT = 5000
CAP_DEFAULT = 20_000
# Hard frame-safety bound for `manifest` responses (length-prefixed JSON
# frames must stay far below the 4 MB vsock frame cap: 8000 entries
# ~1.3 MB). The host clamps its configured cap against this.
_MANIFEST_MAX_ENTRIES = 8000
# Raw byte transfer guard: a single file larger than this is REFUSED by the
# `read` op (mode=raw) rather than truncated — the host verifies every file
# by sha256, so partial content is worse than none.
_RAW_READ_MAX = 2_000_000

_SENSITIVE = re.compile(
    r"(^|/)(\.env(\..*)?|\.git/.*|.*\.pem$|.*\.key$|.*secret.*|.*token.*|.*credentials.*)$",
    re.IGNORECASE,
)


def _resolve(root: str, rel: str) -> str:
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("path is required")
    if "\x00" in rel:
        raise ValueError("path contains NUL byte")
    if os.path.isabs(rel):
        raise ValueError("absolute paths are not allowed")
    if rel.startswith("~") or re.match(r"^[A-Za-z]:", rel):
        raise ValueError("home/drive paths are not allowed")
    norm = os.path.normpath(rel)
    if norm.startswith(".."):
        raise ValueError(f"path escapes workspace: {rel[:200]}")
    full = os.path.normpath(os.path.join(os.path.abspath(root), norm))
    base = os.path.abspath(root)
    if full != base and not full.startswith(base + os.sep):
        raise ValueError(f"path escapes workspace: {rel[:200]}")
    return full


def _is_sensitive(rel: str) -> bool:
    return bool(_SENSITIVE.search((rel or "").replace(os.sep, "/")))


def _cap(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    return text[:cap] + f"\n...[truncated {len(text) - cap} bytes]...", True


def _handle(req: dict, root: str) -> dict:
    op = req.get("op", "")
    cwd = req.get("cwd", ".") or "."
    timeout = max(1, int(req.get("timeout_s", 30) or 30))
    cap = int(req.get("cap_bytes", CAP_DEFAULT) or CAP_DEFAULT)
    req_id = req.get("req_id", "")
    try:
        run_dir = _resolve(root, cwd)
    except ValueError as exc:
        return {"req_id": req_id, "error": str(exc), "exit_code": 1}
    if op == "exec":
        argv = req.get("argv", [])
        if not argv:
            return {"req_id": req_id, "error": "argv required", "exit_code": 1}
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=run_dir if os.path.isdir(run_dir) else root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                out, err = proc.communicate(timeout=timeout)
                timed_out = False
                code = proc.returncode
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                out, err = proc.communicate()
                timed_out = True
                code = None
        except OSError as exc:
            return {"req_id": req_id, "error": str(exc), "exit_code": 1}
        s, t1 = _cap(out or "", cap // 2)
        e, t2 = _cap(err or "", cap // 2)
        return {"req_id": req_id, "exit_code": code, "stdout": s, "stderr": e,
                "truncated": t1 or t2, "timed_out": timed_out, "cwd": cwd}
    if op == "exec_shell":
        command = req.get("command", "")
        if not command:
            return {"req_id": req_id, "error": "command required", "exit_code": 1}
        try:
            proc = subprocess.Popen(
                command, shell=True,
                cwd=run_dir if os.path.isdir(run_dir) else root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True,
            )
            try:
                out, err = proc.communicate(timeout=timeout)
                timed_out = False
                code = proc.returncode
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                out, err = proc.communicate()
                timed_out = True
                code = None
        except OSError as exc:
            return {"req_id": req_id, "error": str(exc), "exit_code": 1}
        s, t1 = _cap(out or "", cap // 2)
        e, t2 = _cap(err or "", cap // 2)
        return {"req_id": req_id, "exit_code": code, "stdout": s, "stderr": e,
                "truncated": t1 or t2, "timed_out": timed_out, "cwd": cwd}
    def _has_symlink_prefix(path: str) -> bool:
        # Any existing path component (or the target itself) that is a
        # symlink escapes lexical containment: refuse instead of following.
        node = path
        seen: set[str] = set()
        while node and node not in seen:
            seen.add(node)
            try:
                if os.path.islink(node):
                    return True
            except OSError:
                return False
            parent = os.path.dirname(node)
            if parent == node:
                break
            node = parent
        return False

    if op == "read":
        path = req.get("path", "")
        if _is_sensitive(path):
            return {"req_id": req_id, "error": "access to sensitive file is blocked"}
        try:
            full = _resolve(root, path)
        except ValueError as exc:
            return {"req_id": req_id, "error": str(exc)}
        if _has_symlink_prefix(full):
            return {"req_id": req_id, "error": "symlink access is blocked"}
        if req.get("mode") == "raw":
            # Byte-exact transfer mode for the manifest reconciliation: the
            # host verifies every file by sha256, so the text-mode decode
            # below (errors="replace") would silently rewrite bytes and
            # make digest verification meaningless. Response is base64
            # `content_b64` + `size`; files beyond _RAW_READ_MAX are
            # REFUSED (an over-cap file can never be silently truncated).
            try:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(full, flags)
                try:
                    with os.fdopen(fd, "rb") as fh:
                        blob = fh.read()
                except Exception as exc:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    return {"req_id": req_id, "error": str(exc)}
            except OSError as exc:
                return {"req_id": req_id, "error": str(exc)}
            if len(blob) > _RAW_READ_MAX:
                return {
                    "req_id": req_id,
                    "error": (
                        f"file exceeds raw read cap: {len(blob)} > {_RAW_READ_MAX}"
                    ),
                }
            return {
                "req_id": req_id,
                "content_b64": base64.b64encode(blob).decode("ascii"),
                "size": len(blob),
            }
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(full, flags)
            try:
                with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except Exception as exc:
                try:
                    os.close(fd)
                except OSError:
                    pass
                return {"req_id": req_id, "error": str(exc)}
        except OSError as exc:
            return {"req_id": req_id, "error": str(exc)}
        content, _ = _cap(content, cap)
        return {"req_id": req_id, "content": content}
    if op == "write":
        path = req.get("path", "")
        content = req.get("content", "")
        if _is_sensitive(path):
            return {"req_id": req_id, "error": "writing to sensitive file is blocked"}
        try:
            full = _resolve(root, path)
        except ValueError as exc:
            return {"req_id": req_id, "error": str(exc)}
        if _has_symlink_prefix(full):
            return {"req_id": req_id, "error": "symlink access is blocked"}
        try:
            os.makedirs(os.path.dirname(full) or full, exist_ok=True)
            if _has_symlink_prefix(full):
                return {"req_id": req_id, "error": "symlink access is blocked"}
            tmp = full + ".fixhub-tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, full)
        except OSError as exc:
            return {"req_id": req_id, "error": str(exc)}
        return {"req_id": req_id, "ok": True}
    if op == "list":
        path = req.get("path", ".")
        recursive = bool(req.get("recursive", False))
        if _is_sensitive(path):
            return {"req_id": req_id, "error": "access to sensitive path is blocked"}
        try:
            full = _resolve(root, path)
        except ValueError as exc:
            return {"req_id": req_id, "error": str(exc)}
        entries: list[str] = []
        try:
            if recursive:
                for r, dirs, files in os.walk(full, followlinks=False):
                    dirs[:] = [
                        d for d in dirs
                        if d not in ("__pycache__", ".git")
                        and not os.path.islink(os.path.join(r, d))
                    ]
                    for name in dirs + files:
                        fp = os.path.join(r, name)
                        if os.path.islink(fp):
                            continue
                        rel = os.path.relpath(fp, root).replace(os.sep, "/")
                        # NOTE: `.git` internals only — never a raw prefix
                        # match, or `.github/*` (the workflow files) would
                        # be silently dropped from the tree listing. That
                        # prefix mistake is the exact root cause of the
                        # #70/#71 "hidden edits invisible to sync" case.
                        if (
                            "__pycache__" in rel
                            or rel == ".git"
                            or rel.startswith(".git/")
                        ):
                            continue
                        if _is_sensitive(rel):
                            continue
                        entries.append(rel)
                        if len(entries) > 2000:
                            break
            else:
                for name in sorted(os.listdir(full)):
                    if name == ".git":
                        continue
                    entries.append(name)
        except OSError as exc:
            return {"req_id": req_id, "error": str(exc)}
        return {"req_id": req_id, "entries": entries[:2000]}
    if op == "manifest":
        # Host->guest reconciliation response: regular repository files with
        # rel path + size + sha256 so the host can transfer only CHANGED
        # files and verify each write by digest. Hidden dirs are INCLUDED
        # (.github/workflows/ci.yml must reconcile); `.git`, `__pycache__`,
        # symlinks, and sensitive names are excluded (same rules as `list`).
        # Truncation is EXPLICIT: a truncated response can never drive host
        # deletions or be mistaken for the complete guest state.
        path = req.get("path", ".")
        if _is_sensitive(path):
            return {"req_id": req_id, "error": "access to sensitive path is blocked"}
        try:
            full = _resolve(root, path)
        except ValueError as exc:
            return {"req_id": req_id, "error": str(exc)}
        try:
            max_entries = int(req.get("max_entries", _MANIFEST_MAX_ENTRIES))
        except (TypeError, ValueError):
            max_entries = _MANIFEST_MAX_ENTRIES
        max_entries = max(1, min(int(max_entries), _MANIFEST_MAX_ENTRIES))
        out: list[dict] = []
        truncated = False
        try:
            for r, dirs, files in os.walk(full, followlinks=False):
                dirs[:] = [
                    d for d in dirs
                    if d not in ("__pycache__", ".git")
                    and not os.path.islink(os.path.join(r, d))
                ]
                for name in sorted(dirs + files):
                    fp = os.path.join(r, name)
                    rel = os.path.relpath(fp, root).replace(os.sep, "/")
                    # `.git` internals only — same prefix-matching rule as
                    # `list` (a raw `startswith(".git")` also swallowed
                    # `.github/*` and hid workflow edits; see #70/#71).
                    if (
                        "__pycache__" in rel
                        or rel == ".git"
                        or rel.startswith(".git/")
                    ):
                        continue
                    if _is_sensitive(rel):
                        continue
                    try:
                        st = os.lstat(fp)
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue  # dirs/symlinks/sockets never shipped
                    if len(out) >= max_entries:
                        truncated = True
                        break
                    h = hashlib.sha256()
                    size = 0
                    try:
                        with open(fp, "rb") as fh:
                            while True:
                                chunk = fh.read(65536)
                                if not chunk:
                                    break
                                h.update(chunk)
                                size += len(chunk)
                    except OSError:
                        continue  # unreadable mid-walk: leave it out; the
                        # host surfaces the gap through verification failure
                    out.append(
                        {"path": rel, "size": size, "sha256": h.hexdigest()}
                    )
                if truncated:
                    break
        except OSError as exc:
            return {"req_id": req_id, "error": str(exc)}
        return {
            "req_id": req_id,
            "entries": out,
            "truncated": truncated,
        }
    return {"req_id": req_id, "error": f"unknown op: {op}", "exit_code": 1}


def _recvall(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def serve_once(conn: socket.socket, root: str) -> None:
    hdr = _recvall(conn, 4)
    (n,) = struct.unpack(">I", hdr)
    if n <= 0 or n > 4 * 1024 * 1024:
        raise ValueError("bad frame length")
    body = _recvall(conn, n)
    req = json.loads(body.decode("utf-8"))
    resp = _handle(req, root)
    out = json.dumps(resp).encode("utf-8")
    conn.sendall(struct.pack(">I", len(out)) + out)


def main(root: str = "/workspace") -> None:
    os.makedirs(root, exist_ok=True)
    # AF_VSOCK listener on PORT (host connects to guest CID:PORT).
    srv = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    srv.bind((socket.VMADDR_CID_ANY, PORT))
    srv.listen(16)
    while True:
        conn, _ = srv.accept()
        try:
            # One request per connection (simple + auditable).
            serve_once(conn, root)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    import sys

    main(sys.argv[1] if len(sys.argv) > 1 else "/workspace")
