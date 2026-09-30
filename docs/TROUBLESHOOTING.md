# FixHub troubleshooting (observed behavior)

All entries describe behavior in the current implementation. Follow the check order; each step names the source file or endpoint so you can verify instead of guessing.

## Backend unreachable in Docker

- Symptom: `http://localhost:8001/health` fails.
- Check `docker compose ps` (expect `backend`, `worker`, `redis`, `frontend` up) and `docker compose logs backend`.
- Remember the port mapping: host `8001` → container `8000` (`docker-compose.yml`). The container still listens on `8000`; nginx proxies to `http://backend:8000` (`frontend/nginx.conf`).
- Local dev uses `http://localhost:8000/health` (`uvicorn app.main:app --port 8000`).

## Queue shows offline / runs fall back inline

- Symptom: IDE worker pill says `offline (runs fall back inline)`, or `GET /api/queue/health` returns `{ok: False, error: "redis unavailable"}`.
- Cause: Redis unreachable (`backend/app/tasks/queue.py:_redis_connection` ping failed) or `rq` missing.
- Fix: `docker compose up redis worker`, confirm `REDIS_URL` (`redis://redis:6379/0` in Docker, `redis://localhost:6379/0` locally). For dev without Redis, run explicitly inline: `POST /api/tasks/{id}/run?sync=true`.

## Webhook returns 401 bad signature

- Cause: `GITHUB_WEBHOOK_SECRET` in `backend/.env` does not match the GitHub App webhook secret, or the signature header is missing (`backend/app/github/webhook.py:verify_signature` expects `sha256=` HMAC of the raw body).
- Fix: copy the exact secret from the App settings into `backend/.env`, restart `backend`, redeliver the webhook.

## Task stuck at BLOCKED: no clone source

- Cause: the `repositories` row for `owner/repo` has no `installation_id` (`backend/app/tasks/service.py:run_task_inline` → `BLOCKED`, `no_source`).
- Fix: connect the repo via the IDE Repositories panel, `POST /api/repositories/connect`, or SQL `UPDATE repositories SET installation_id='…' WHERE github_full_name='owner/repo';`. The webhook auto-creates the repo row without the mapping; the mapping enables clone + issues.

## Task BLOCKED: repo inaccessible / installation_token_failed

- Cause: App key misconfigured (`GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY_PATH`), app not installed on the repo, or token request failed (`backend/app/github/app_auth.py`).
- Fix: confirm the `.pem` file exists next to `backend/.env` (mounted read-only in Docker), the App ID is the App ID (not installation ID), and the app is installed on `owner/repo`. Check `GET /api/github/installations`.

## Task BLOCKED: no LLM key / LLM auth rejected

- Cause: no provider key set, or the gateway returned 401/403 (`backend/app/llm/client.py:_active`, `chat_completion`).
- Fix: set `BYNARA_API_KEY` (or `XKIRO_API_KEY`) for the active `LLM_PROVIDER`, or set the legacy `LLM_API_KEY` trio. Restart `backend`/`worker`. Transient 429/5xx/transport blips are retried with backoff; persistent auth failures fail fast to `BLOCKED`, not `FAILED`.

## Task FAILED: agent crashed / publish_failed / git push TLS disconnect

- Observed live: an LLM transport blip recorded `FAILED` with the real error; a TLS disconnect on `git push` recorded `FAILED/publish_failed` with the real error (no fake success). See `docs/FINAL_ENGINEERING_REPORT.md` incidents.
- Fix: inspect `GET /api/tasks/{id}` events (last `TOOL_CALL` / `FAILED` payload holds the real error), fix the underlying cause (network, key, remote), then retry explicitly via `POST /api/tasks/{id}/run`. One attempt per call by design; there are no hidden auto-retries.

## Task at NEEDS_REVIEW instead of a PR

- Expected when: `AUTO_PUBLISH=0` (review gate), local gates stayed red after 2 fix rounds (`gate_failed`), the agent crashed with real uncommitted changes (`agent_crash`, workspace preserved), or `approve` found no meaningful changes path variants.
- Fix: open the task in the IDE (Explorer + Diff/Proof + TraceView + verification view), resolve locally, then ReviewPanel Approve & Commit (`POST /api/tasks/{id}/approve`). Workspaces are kept for `NEEDS_REVIEW`; other terminal states are removed unless `WORKSPACE_KEEP=1`.

## Task COMPLETED with superseded_by (no PR created)

- Expected: a green FixHub PR already touched the same files (pre-agent or pre-publish stale guard, `SUPERSEDED`). This prevents competing rewrites on top of a landed fix.
- Fix: none needed. Follow the linked PR URL from the event payload.

## Task FAILED: branch_collision

- Cause: another active (`RUNNING`/`AWAITING_CI`/`NEEDS_REVIEW`) task already owns the same deterministic branch (`backend/app/tasks/service.py:_publish_task`). Branch names embed the task ID, so this fires only on genuine misuse.
- Fix: let the owning task finish, or cancel it, then re-run.

## CI watcher never completes (stays AWAITING_CI / pending)

- Causes: no installation token or commit SHA (cannot observe), GitHub API error, or checks still non-terminal (`backend/app/tasks/ciwatch.py:_check_one` returns `pending` and retries next 60s tick).
- Fix: confirm the repo is connected and the commit exists; trigger a manual pass via `POST /api/cron/ci-watch`; check backend logs. After 3 failed repair rounds the task goes `FAILED` with the preserved tail.

## Missed webhooks (tunnel/host downtime)

- Deliveries depend on the public webhook URL staying reachable (e.g. ngrok + host uptime). There is no dead-letter replay; a reboot mid-session required explicit redrive during the live E2E (`docs/FINAL_ENGINEERING_REPORT.md`).
- Fix: redeliver from GitHub App settings → Recent Deliveries, or recreate the task via the IDE Fix button / `POST /api/tasks/from-issue`, then run it.

## Workspace expired (410) in the IDE

- Cause: terminal-state cleanup removed `WORKSPACE_ROOT/task-{id}` (`WORKSPACE_KEEP=0`), or `docker compose down -v` wiped the volume.
- Fix: re-run the task for a fresh workspace, or set `WORKSPACE_KEEP=1` before reproducing for debugging and clean up with `POST /api/tasks/{id}/cleanup`.

## Stale RUNNING tasks after a crash/restart

- The backend sweeps tasks stuck `RUNNING` longer than `JOB_TIMEOUT_S + 600s` to `FAILED` on startup (`backend/app/main.py:_sweep_stale_running_tasks`). RQ `on_failure` also marks crashed jobs `FAILED`. Re-run to retry.

## Agent repeats path errors or edits the wrong file

- Check the trace first: `TOOL_CALL` args show the exact repo-relative path attempted. Absolute paths, `/tmp` prefixes, workspace-root prefixes, and `..` escapes are rejected by `backend/app/agent/paths.py` — use the repository-relative path (e.g. `apps/api/app/core/config.py`) and per-call `cwd` (e.g. `apps/api`).
- Sensitive paths (`.env`, `.git/*`, `*.pem`, `*.key`, secrets/tokens/credentials) are blocked for reads/writes/search by design.
- Memory is background only and may describe already-fixed files; the CI failure/issue text is the ground truth. Verify files before trusting memory.

## Frontend dev proxy issues

- `frontend/vite.config.ts` proxies `/api`, `/webhooks`, `/health` to `http://localhost:8000`. If the Vite app (`:5173`) shows backend-down, confirm the backend is on port 8000 (not 8001) in local dev.
