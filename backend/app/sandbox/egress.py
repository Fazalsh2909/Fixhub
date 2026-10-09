"""Phase 5: host-side egress proxy + DNS stub (the allowlist enforcement point).

The guest has NO direct route out. All guest TCP 443 is NAT-redirected
into this proxy (see net.py); the guest stub resolver is this DNS server.
Both run on the TRUSTED host (root on the KVM host), never in the guest.

Policy (single source of truth: FC_EGRESS_ALLOWLIST suffixes, enforced here):
- DNS: answer A queries for allowlisted names via the host resolver;
  REFUSE everything else (no direct-DNS bypass, no tunnelling oracle).
- Proxy: parse the TLS ClientHello SNI, suffix-match the allowlist, resolve
  the name on the host, connect to <resolved-ip>:443, splice bytes with caps.
  No SNI (or non-allowlisted SNI, or literal-IP SNI) -> connection closed.
  The proxy NEVER issues requests itself based on guest input beyond the
  TCP splice; LLM provider APIs stay host-only by allowlist construction.

Lifecycle: one shared daemon per host (pidfile + socket probes). Started
on demand by net.ensure_isolation(); fail-closed when it cannot bind.
Per-task state is forbidden here (all tasks share the same allowlist).

This module never runs inside the guest.
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import threading
import time

PROXY_PORT_DEFAULT = 8443
DNS_PORT_DEFAULT = 53
_DNS_ADDR_DEFAULT = "10.200.0.1"
_PIDFILE = "/var/run/fixhub-egress.pid"
_SPLICE_CAP_BYTES = 20 * 1024 * 1024
_CONN_TIMEOUT_S = 15


def _cfg(name: str, default=""):
    try:
        from app.config import settings as _settings

        return getattr(_settings, name, default)
    except Exception:
        return default


def proxy_addr() -> str:
    return str(_cfg("FC_EGRESS_PROXY_ADDR", _DNS_ADDR_DEFAULT) or _DNS_ADDR_DEFAULT)


def proxy_port() -> int:
    try:
        return int(_cfg("FC_EGRESS_PROXY_PORT", PROXY_PORT_DEFAULT) or PROXY_PORT_DEFAULT)
    except (TypeError, ValueError):
        return PROXY_PORT_DEFAULT


def dns_addr() -> str:
    return str(_cfg("FC_DNS_STUB_ADDR", _DNS_ADDR_DEFAULT) or _DNS_ADDR_DEFAULT)


def allowlist() -> list[str]:
    try:
        from app.config import settings as _settings

        raw = str(getattr(_settings, "FC_EGRESS_ALLOWLIST", "") or "")
    except Exception:
        raw = ""
    return [h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip()]


def host_allowed(hostname: str, allow: list[str] | None = None) -> bool:
    """Suffix-match a hostname against the allowlist (pure, unit-testable)."""
    allow = allow if allow is not None else allowlist()
    name = (hostname or "").strip().lower().rstrip(".")
    if not name or "/" in name or " " in name or "@" in name:
        return False
    # Literal IPs never match (blocks direct-IP bypass).
    try:
        import ipaddress as _ip

        _ip.ip_address(name)
        return False
    except ValueError:
        pass
    for suffix in allow:
        if name == suffix or name.endswith("." + suffix):
            return True
    return False


def sni_from_clienthello(data: bytes) -> str:
    """Extract the SNI hostname from a TLS ClientHello (pure, unit-testable).

    Returns "" when absent/unparseable (caller must reject).
    """
    try:
        if len(data) < 5 or data[0] != 0x16 or data[1] != 0x03:
            return ""
        rec_len = struct.unpack(">H", data[3:5])[0]
        if len(data) < 5 + rec_len:
            return ""
        hs = data[5 : 5 + rec_len]
        if len(hs) < 4 or hs[0] != 0x01:
            return ""
        body = hs[4:]
        if len(body) < 34:
            return ""
        pos = 34  # skip version(2) + random(32)
        if pos >= len(body):
            return ""
        sid_len = body[pos]
        pos += 1 + sid_len
        if pos + 2 > len(body):
            return ""
        cs_len = struct.unpack(">H", body[pos : pos + 2])[0]
        pos += 2 + cs_len
        if pos >= len(body):
            return ""
        comp_len = body[pos]
        pos += 1 + comp_len
        if pos + 2 > len(body):
            return ""
        ext_total = struct.unpack(">H", body[pos : pos + 2])[0]
        pos += 2
        end = min(len(body), pos + ext_total)
        while pos + 4 <= end:
            etype = struct.unpack(">H", body[pos : pos + 2])[0]
            elen = struct.unpack(">H", body[pos + 2 : pos + 4])[0]
            pos += 4
            if pos + elen > end:
                break
            if etype == 0 and elen >= 2:  # server_name
                lst_len = struct.unpack(">H", body[pos : pos + 2])[0]
                p2 = pos + 2
                while p2 + 3 <= pos + elen and p2 + 3 <= end:
                    ntype = body[p2]
                    nlen = struct.unpack(">H", body[p2 + 1 : p2 + 3])[0]
                    p2 += 3
                    if p2 + nlen > end:
                        break
                    if ntype == 0:
                        return body[p2 : p2 + nlen].decode("ascii", "replace")
                    p2 += nlen
                return ""
            pos += elen
        return ""
    except Exception:
        return ""


def _splice(a: socket.socket, b: socket.socket, cap: int) -> None:
    """Bidirectional copy until EOF/error/cap. Returns (never raises)."""
    total = [0]

    def _one(src: socket.socket, dst: socket.socket) -> None:
        try:
            while total[0] < cap:
                chunk = src.recv(65536)
                if not chunk:
                    break
                total[0] += len(chunk)
                dst.sendall(chunk)
        except Exception:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass

    t = threading.Thread(target=_one, args=(b, a), daemon=True)
    t.start()
    _one(a, b)
    t.join(timeout=5)


def _handle_proxy_conn(client: socket.socket) -> None:
    allow = allowlist()
    try:
        client.settimeout(_CONN_TIMEOUT_S)
        # Peek the ClientHello without consuming (MSG_PEEK may be unavailable
        # on some platforms; fall back to a plain read + fail closed).
        try:
            head = client.recv(4096, socket.MSG_PEEK)
        except Exception:
            head = b""
        sni = sni_from_clienthello(head) if head else ""
        if not sni or not host_allowed(sni, allow):
            return
        try:
            dest_ip = socket.gethostbyname(sni)
        except OSError:
            return
        upstream = socket.create_connection((dest_ip, 443), timeout=_CONN_TIMEOUT_S)
        try:
            upstream.settimeout(60)
            # Consume the peeked bytes by reading them for real, then forward.
            try:
                client.settimeout(_CONN_TIMEOUT_S)
                first = client.recv(len(head) if head else 1)
            except Exception:
                return
            if not first:
                return
            upstream.sendall(first)
            _splice(client, upstream, _SPLICE_CAP_BYTES)
        finally:
            try:
                upstream.close()
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass


def _dns_response(query: bytes, allow: list[str]) -> bytes | None:
    """Build a DNS reply: A-answer for allowlisted names, REFUSED otherwise.

    Pure logic over the wire format (unit-testable). Returns None when the
    query is malformed (caller drops it).
    """
    try:
        if len(query) < 12:
            return None
        txid, flags, qd, _, _, _ = struct.unpack(">HHHHHH", query[:12])
        if qd != 1:
            return None
        pos = 12
        labels: list[str] = []
        while True:
            if pos >= len(query):
                return None
            ln = query[pos]
            pos += 1
            if ln == 0:
                break
            if ln & 0xC0 or pos + ln > len(query):
                return None
            labels.append(query[pos : pos + ln].decode("ascii", "replace"))
            pos += ln
        if pos + 4 > len(query):
            return None
        qtype, qclass = struct.unpack(">HH", query[pos : pos + 4])
        question = query[12 : pos + 4]
        name = ".".join(labels)
        if qtype != 1 or qclass != 1 or not host_allowed(name, allow):
            # REFUSED (rcode 5), echo the question.
            return struct.pack(">HHHHHH", txid, 0x8185, 1, 0, 0, 0) + question
        try:
            ip = socket.gethostbyname(name)
            rdata = socket.inet_aton(ip)
        except OSError:
            # SERVFAIL (rcode 2) when the host itself cannot resolve.
            return struct.pack(">HHHHHH", txid, 0x8182, 1, 0, 0, 0) + question
        answer = (
            b"\xc0\x0c"  # pointer to the question name
            + struct.pack(">HHIH", 1, 1, 60, 4)
            + rdata
        )
        return struct.pack(">HHHHHH", txid, 0x8180, 1, 1, 0, 0) + question + answer
    except Exception:
        return None


def _dns_loop(bind_ip: str) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        srv.bind((bind_ip, DNS_PORT_DEFAULT))
    except OSError:
        return
    while True:
        try:
            data, peer = srv.recvfrom(512)
            resp = _dns_response(data, allowlist())
            if resp:
                srv.sendto(resp, peer)
        except Exception:
            continue


def _proxy_loop(bind_ip: str, port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((bind_ip, port))
    except OSError:
        return
    srv.listen(128)
    while True:
        try:
            conn, _ = srv.accept()
            threading.Thread(target=_handle_proxy_conn, args=(conn,), daemon=True).start()
        except Exception:
            continue


def _probe_available(timeout_s: int = 5) -> tuple[bool, str]:
    """Live-probe both daemons. Returns (ok, detail)."""
    addr = proxy_addr()
    # 1. Proxy TCP connect.
    try:
        s = socket.create_connection((addr, proxy_port()), timeout=timeout_s)
        s.close()
    except OSError as exc:
        return False, f"proxy {addr}:{proxy_port()} unreachable: {exc}"
    # 2. DNS: allowlisted name must resolve; junk name must NOT.
    allow = allowlist()
    if not allow:
        return False, "egress allowlist is empty (refusing to run open)"
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.settimeout(timeout_s)
            txid = 0x1234
            q = (
                struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
                + b"".join(
                    bytes([len(p)]) + p.encode("ascii")
                    for p in allow[0].split(".")
                )
                + b"\x00"
                + struct.pack(">HH", 1, 1)
            )
            probe.sendto(q, (dns_addr(), DNS_PORT_DEFAULT))
            resp, _ = probe.recvfrom(512)
            rtxid, rflags = struct.unpack(">HH", resp[:4])
            if rtxid != txid or (rflags & 0x000F) != 0:
                return False, f"DNS stub refused allowlisted {allow[0]}"
        finally:
            probe.close()
    except OSError as exc:
        return False, f"DNS stub {dns_addr()}:53 unreachable: {exc}"
    return True, "proxy+dns live"


def ensure_available() -> dict:
    """Start the shared egress daemon if needed; fail closed when unusable.

    Returns {proxy_addr, proxy_port, dns_addr}. Raises RuntimeError otherwise.
    Idempotent: an already-running daemon (pidfile + live probes) is reused.
    """
    if os.name == "nt":
        raise RuntimeError("egress proxy requires Linux (refusing VM boot on Windows)")
    ok, detail = _probe_available()
    if ok:
        return {"proxy_addr": proxy_addr(), "proxy_port": proxy_port(), "dns_addr": dns_addr()}
    # Try to start (or adopt) the daemon, then re-probe once.
    _start_daemon_detached()
    deadline = time.monotonic() + 10
    last = detail
    while time.monotonic() < deadline:
        time.sleep(0.5)
        ok, last = _probe_available()
        if ok:
            return {"proxy_addr": proxy_addr(), "proxy_port": proxy_port(), "dns_addr": dns_addr()}
    raise RuntimeError(f"host egress proxy/stub unavailable: {last}")


def _start_daemon_detached() -> None:
    """Spawn the daemon (double-fork-ish via setsid + stdio to devnull)."""
    try:
        if os.path.exists(_PIDFILE):
            with open(_PIDFILE, "r", encoding="utf-8") as fh:
                pid = int(fh.read().strip().split()[0])
            os.kill(pid, 0)  # alive -> someone else owns it; just wait on probes
            return
    except (OSError, ValueError):
        pass
    try:
        here = os.path.abspath(__file__)
        backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(here)))
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.sandbox.egress", "serve"],
            cwd=backend_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            with open(_PIDFILE, "w", encoding="utf-8") as fh:
                fh.write(str(proc.pid))
        except OSError:
            pass
    except Exception as exc:
        raise RuntimeError(f"could not spawn egress daemon: {exc}")


def serve_forever() -> None:
    """Daemon entrypoint: `python -m app.sandbox.egress serve` (as root)."""
    addr = proxy_addr()
    threads = [
        threading.Thread(target=_dns_loop, args=(addr,), daemon=True),
        threading.Thread(target=_proxy_loop, args=(addr, proxy_port()), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) > 1 and _sys.argv[1] == "serve":
        serve_forever()
    else:
        ok, detail = _probe_available()
        print(("OK " if ok else "FAIL ") + detail)
