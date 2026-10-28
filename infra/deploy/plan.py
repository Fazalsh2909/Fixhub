#!/usr/bin/env python3
"""Decide what a FixHub staging deployment must do for a given change set.

Pure decision logic (stdin/argv only, no system access) so backend tests can
cover the full restart-selection matrix without a server. `deploy.sh` consumes
the output; this module never runs commands itself.

Input
    Changed file paths (one per line) on stdin, produced by
    `git diff --name-only <old> <new>` in the application checkout.
    --old is the previously deployed SHA; empty means the first automated
    deploy (or an unknown previous state).

Output
    --format json : {"old":..., "new":..., "flags": {...}, "notes": [...]}
    --format sh   : sourceable KEY=VALUE lines (+ "# note" comments), consumed
                    by deploy.sh via eval. All values are literal 0/1.

Rules (docs/DEPLOYMENT_GITHUB_ACTIONS.md)
- Unknown previous SHA: conservative full plan — restart all four services,
  sync both virtualenvs, reinstall tracked systemd config and the netd
  helper, run migrations. A first deploy must prove the whole stack instead
  of assuming anything about the server's current state.
- backend/** changed               -> restart fixhub-api + fixhub-worker and
                                      run migrations (single explicit step).
                                      backend/tests/** is excluded: test-only
                                      changes never run on the server, so
                                      they restart nothing.
- backend/requirements.txt changed -> sync the app venv. The netd/egress venv
                                      is synced ONLY when their code also
                                      changed: installing packages under a
                                      running root daemon can break it until
                                      it restarts, so never sync without a
                                      scheduled restart.
- backend/app/sandbox/** changed   -> restart fixhub-egress.
- infra/firecracker/fixhub-netd.py -> reinstall /usr/local/sbin/fixhub-netd
                                      (installed copy) and restart
                                      fixhub-netd.
- infra/systemd/** changed         -> reinstall tracked drop-ins and restart
                                      api+worker (their effective unit
                                      configuration changed).
- fixhub-netd restart              -> also restart api+worker: their tracked
                                      drop-ins declare Requires=fixhub-netd,
                                      and systemd deactivates units that
                                      Require= a unit that stops.
- frontend/** changed              -> BUILD_FRONTEND=1; deploy.sh only
                                      publishes when FRONTEND_DIST_DIR is
                                      configured on the server, otherwise it
                                      logs a skip note (staging may be
                                      API-only).
- docs/tests-only changes          -> no restarts; the checkout and live
                                      verification still run.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

FLAGS: tuple[str, ...] = (
    "RESTART_API",
    "RESTART_WORKER",
    "RESTART_NETD",
    "RESTART_EGRESS",
    "SYNC_APP_DEPS",
    "SYNC_NET_DEPS",
    "INSTALL_SYSTEMD_CONFIG",
    "REINSTALL_NETD_HELPER",
    "RUN_MIGRATIONS",
    "BUILD_FRONTEND",
)


def _any_prefix(changed: list[str], prefix: str) -> bool:
    return any(c == prefix.rstrip("/") or c.startswith(prefix) for c in changed)


def plan(changed: list[str], old_sha: str) -> tuple[dict[str, int], list[str]]:
    """Return (flags, notes) for the changed-path set and previous SHA."""
    flags = {flag: 0 for flag in FLAGS}
    notes: list[str] = []

    if not old_sha:
        # First automated deploy / unknown previous state: prove everything.
        for flag in FLAGS:
            flags[flag] = 1
        notes.append(
            "no previous deploy state: conservative full plan "
            "(all services restarted, both venvs synced, systemd config "
            "reinstalled, migrations run)"
        )
        return flags, notes

    if not changed:
        notes.append(
            "no deployable changes (docs/tests only): checkout + live "
            "verification only, no restarts"
        )
        return flags, notes

    backend_changed = any(
        c.startswith("backend/") and not c.startswith("backend/tests/")
        for c in changed
    )
    req_changed = "backend/requirements.txt" in changed
    sandbox_changed = _any_prefix(changed, "backend/app/sandbox/")
    netd_src_changed = "infra/firecracker/fixhub-netd.py" in changed
    systemd_changed = _any_prefix(changed, "infra/systemd/")
    frontend_changed = _any_prefix(changed, "frontend/")

    if backend_changed:
        flags["RESTART_API"] = 1
        flags["RESTART_WORKER"] = 1
        flags["RUN_MIGRATIONS"] = 1
    if req_changed:
        flags["SYNC_APP_DEPS"] = 1
    if sandbox_changed:
        flags["RESTART_EGRESS"] = 1
    if netd_src_changed:
        flags["REINSTALL_NETD_HELPER"] = 1
        flags["RESTART_NETD"] = 1
    if (sandbox_changed or netd_src_changed) and req_changed:
        # Only sync the root daemons' venv when they are being restarted too.
        flags["SYNC_NET_DEPS"] = 1
    if systemd_changed:
        flags["INSTALL_SYSTEMD_CONFIG"] = 1
        flags["RESTART_API"] = 1
        flags["RESTART_WORKER"] = 1
    if flags["RESTART_NETD"]:
        # fixhub-api/fixhub-worker drop-ins declare Requires=fixhub-netd;
        # systemd deactivates them when netd stops, so restart them after.
        flags["RESTART_API"] = 1
        flags["RESTART_WORKER"] = 1
    if frontend_changed:
        flags["BUILD_FRONTEND"] = 1

    if not any(flags.values()):
        notes.append(
            "no deployable changes detected: checkout + live verification "
            "only, no restarts"
        )

    return flags, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--old",
        default="",
        help="previously deployed SHA (empty = first automated deploy)",
    )
    parser.add_argument("--new", default="", help="SHA being deployed")
    parser.add_argument("--format", choices=("json", "sh"), default="json")
    args = parser.parse_args(argv)

    for label, sha in (("old", args.old), ("new", args.new)):
        if sha and not SHA_RE.match(sha):
            print(
                f"error: --{label} '{sha}' is not a 40-hex commit SHA",
                file=sys.stderr,
            )
            return 2

    changed = [
        line.strip()
        for line in sys.stdin.read().splitlines()
        if line.strip()
    ]
    flags, notes = plan(changed, args.old)

    if args.format == "json":
        print(
            json.dumps(
                {"old": args.old, "new": args.new, "flags": flags, "notes": notes}
            )
        )
    else:
        for flag in FLAGS:
            print(f"{flag}={flags[flag]}")
        for note in notes:
            print(f"# {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
