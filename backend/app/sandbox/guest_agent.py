"""Phase 5 M3: host-side guest-agent protocol + tokenless repo transfer.

Wire format (vsock, length-prefixed JSON, v1):
  request  {op, cwd, argv|command, stdin, timeout_s, cap_bytes, req_id}
  response {req_id, exit_code, stdout, stderr, truncated, duration_ms, timed_out}

Ops: exec (argv array, no shell), exec_shell (explicit shell string for the
agent's run_command parity), read, write, list, git_status, git_diff.
Paths are repo-relative; the guest re-validates the jail AND the host
re-validates returned paths (defense in depth).

Security rules (enforced here, not in prompts):
- No request field may carry secrets: the host NEVER sends tokens/keys/URLs
  containing credentials. Repo transfer strips credentials BEFORE streaming.
- `strip_token_from_git_config()` removes `x-access-token:` / embedded
  userinfo from `.git/config` remotes so a pushed-capable URL can never enter
  the guest. Push-capable remotes are reconstructed ONLY on the host.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import struct
import tarfile

_OPS = {"exec", "exec_shell", "read", "write", "list", "git_status", "git_diff"}

_TOKEN_URL_RE = re.compile(r"(https?://)([^/@\s]+@)([^\s]+)")
_X_TOKEN_RE = re.compile(r"x-access-token:[^@\s]+@")


def build_request(
    *,
    op: str,
    cwd: str = ".",
    argv: list[str] | None = None,
    command: str = "",
    stdin: str = "",
    timeout_s: int = 180,
    cap_bytes: int = 20_000,
    req_id: str = "",
) -> dict:
    if op not in _OPS:
        raise ValueError(f"unknown guest op: {op}")
    return {
        "v": 1,
        "op": op,
        "cwd": cwd or ".",
        "argv": list(argv or []),
        "command": command or "",
        "stdin": stdin or "",
        "timeout_s": int(timeout_s or 180),
        "cap_bytes": int(cap_bytes or 20_000),
        "req_id": req_id or "r1",
    }


def encode_frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def decode_frame(buf: bytes) -> tuple[dict, bytes]:
    if len(buf) < 4:
        raise ValueError("short frame")
    (n,) = struct.unpack(">I", buf[:4])
    if len(buf) < 4 + n:
        raise ValueError("incomplete frame")
    return json.loads(buf[4 : 4 + n].decode("utf-8")), buf[4 + n :]


def strip_token_from_git_config(config_text: str) -> str:
    """Remove embedded credentials from a .git/config remote URL.

    `https://x-access-token:TOKEN@github.com/o/r.git` ->
    `https://github.com/o/r.git`. Also strips generic `user@` userinfo.
    Idempotent. Never raises.
    """
    try:
        out = _X_TOKEN_RE.sub("", config_text or "")
        # Catch any residual userinfo in https remotes (conservative).
        def _strip(m: re.Match) -> str:
            return f"{m.group(1)}{m.group(3)}"

        return _TOKEN_URL_RE.sub(_strip, out)
    except Exception:
        return config_text


def make_tokenless_tree(src_dir: str, dest_tar_bytes: io.BytesIO | None = None) -> bytes:
    """Export src_dir as a tar stream with .git/config token-stripped.

    The host clones WITH the installation token, then calls this to produce
    the bytes transferred to the guest. The host workspace keeps the token;
    the guest copy never contains it. Excludes no source files (parity with
    host behavior) except credential material in .git/config.
    """
    buf = dest_tar_bytes or io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for root, dirs, files in os.walk(src_dir):
            # Skip the host's pycache noise like publisher does (parity).
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, src_dir)
                if "__pycache__" in rel:
                    continue
                try:
                    if rel.replace(os.sep, "/") == ".git/config":
                        with open(full, "r", encoding="utf-8", errors="replace") as fh:
                            cleaned = strip_token_from_git_config(fh.read())
                        data = cleaned.encode("utf-8")
                        info = tarfile.TarInfo(name=rel)
                        info.size = len(data)
                        info.mtime = int(os.path.getmtime(full))
                        tar.addfile(info, io.BytesIO(data))
                    else:
                        tar.add(full, arcname=rel, recursive=False)
                except OSError:
                    continue
    buf.seek(0)
    return buf.getvalue()


class VsockClient:
    """Length-prefixed JSON over AF_VSOCK (CID, port). One instance per VM.

    Timeouts are enforced at the socket level AND by the guest agent; the
    host treats any transport error as a failed exec (never host fallback).
    """

    def __init__(self, *, cid: int, port: int, connect_timeout_s: int = 10):
        self.cid = int(cid)
        self.port = int(port)
        self.connect_timeout_s = int(connect_timeout_s or 10)

    def call(self, request: dict, timeout_s: int = 180) -> dict:
        if not hasattr(socket, "AF_VSOCK"):
            raise RuntimeError("AF_VSOCK unavailable on this host (need Linux)")
        raw = encode_frame(request)
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.connect_timeout_s)
            sock.connect((self.cid, self.port))
            sock.settimeout(max(1, int(timeout_s or 180)) + 5)
            sock.sendall(raw)
            # Read length prefix then exact body.
            hdr = b""
            while len(hdr) < 4:
                chunk = sock.recv(4 - len(hdr))
                if not chunk:
                    raise RuntimeError("vsock closed before response header")
                hdr += chunk
            (n,) = struct.unpack(">I", hdr)
            if n <= 0 or n > 4 * 1024 * 1024:
                raise RuntimeError(f"vsock response too large: {n}")
            body = b""
            while len(body) < n:
                chunk = sock.recv(min(65536, n - len(body)))
                if not chunk:
                    raise RuntimeError("vsock closed mid-response")
                body += chunk
            resp, _ = decode_frame(hdr + body)
            return resp
        finally:
            try:
                sock.close()
            except Exception:
                pass
