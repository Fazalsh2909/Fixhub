# Phase 5 — Firecracker microVM sandbox

Replaces the subprocess sandbox with real microVM isolation for untrusted repo
code. Docker is **not** the sandbox.

## Execution flow

GitHub event → API (HMAC + dedupe, owner-scoped QUEUED) → Postgres leases →
RQ worker `claim_task()` → `provision(task_id, workspace)` (jailer) → tokenless
repo sync host→guest (vsock writes, `.git/config` stripped) → agent tools via
vsock exec (`exec`/`exec_shell`/`read`/`write`/`list`) → bounded results →
`sync_guest_to_host()` → lease/fencing checks on host → `_publish_task()` on
host (installation token never enters guest) → `destroy(task_id)` (halt + kill
+ rm jail/overlay/netns).

LLM calls stay on the host (BYOK thread-local). GitHub push/PR stays on the
host. Guest holds a tokenless checkout only.

## Security boundary (guest never receives)

FixHub session secrets, database credentials, LLM API keys, GitHub App private
keys, other users' credentials, host filesystem access, Docker sockets (none
exist), the Firecracker host API socket, or host-controlling credentials.
Enforced by: no-secret vsock API (type-level — guest calls take no token
param), tokenless transfer (`strip_token_from_git_config`), per-VM jail dir +
0700 API socket + per-VM uid/gid, netns/nft default-deny, cgroup + rate limits,
immutable base + disposable overlay.

## Configuration (`SANDBOX_BACKEND=host|firecracker`)

- `host` (default): legacy subprocess — dev/test only. Production refuses it.
- `firecracker`: real microVMs. Any missing prerequisite (KVM, binaries,
  images, net isolation) raises `SandboxBlockedError` → task BLOCKED.
  Unknown backend names fail closed (never default to host).

## Jail layout (official)

`<FC_CHROOT_BASE>/firecracker/task-<id>/root/` holds `firecracker` (jailer
copy), `firecracker.pid`, `api.socket` (explicit `--api-sock /api.socket`),
`vmlinux`, and `overlay.ext4`. The host dials `<root>/api.socket`; drive and
kernel API paths are jail-relative (`./…`). There is no host fallback.

## Files

- `backend/app/sandbox/backend.py` — ABC + dispatch (the cut point).
- `backend/app/sandbox/sandbox.py` — host backend + shared redaction/policy.
- `backend/app/sandbox/firecracker.py` — jailer lifecycle, vsock exec, repo
  sync both directions, destroy/orphan reaper. Guest CID = `_cid_for(task)`
  in BOTH the `/vsock` config and every host `VsockClient` dial.
- `backend/app/sandbox/guest_agent.py` — host-side protocol + token stripping.
- `backend/app/sandbox/guest/agent.py` — guest-side exec server (baked into rootfs).
- `backend/app/sandbox/net.py` — netns/TAP/veth/nft default-deny + forced
  TCP-443 redirect into the host proxy. No default route in task netns.
- `backend/app/sandbox/egress.py` — host SNI proxy + DNS stub (the
  allowlist enforcement point; `FC_EGRESS_ALLOWLIST` suffixes, including
  `github.com` for git ls-remote/clone).
- `backend/app/sandbox/images.py` — artifact verification.
- Tests: `test_sandbox_backend.py` (no KVM) + `test_sandbox_firecracker.py`
  (15 real-VM probes, `FIXHUB_FIRECRACKER_TEST=1`).
