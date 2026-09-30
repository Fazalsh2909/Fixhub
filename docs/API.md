# FixHub API reference (current implementation)

Base URLs (local only, no deployed demo):

- Docker: backend on host `http://localhost:8001`, frontend on `http://localhost:8080`.
- Local dev: backend `http://localhost:8000` (`uvicorn app.main:app --port 8000`), frontend `http://localhost:5173`.

Sources: `backend/app/main.py` (task/repo routes), `backend/app/api/ide.py` (IDE routes), `backend/app/github/webhook.py` (webhook).

## Health and queue

- `GET /health` → `{status: "ok", env}` (`backend/app/main.py:health`).
- `GET /api/queue/health` → `{ok, queue, count?, redis_url? | error}`. `ok: False` means Redis is unavailable; runs fall back to inline (`backend/app/tasks/queue.py:queue_health`).
- `POST /api/cron/ci-watch` → one CI-watcher pass `{checked, completed, repair_enqueued, failed, pending}` (`backend/app/tasks/ciwatch.py`).

## Webhook

- `POST /webhooks/github` — GitHub App deliveries. Requires `X-Hub-Signature-256: sha256=…` (HMAC of raw body with `GITHUB_WEBHOOK_SECRET`) and `X-GitHub-Delivery` (dedupe) plus `X-GitHub-Event`.
  - `issues` `opened`/`reopened` → Task `RUNNING` + `TASK_CREATED`, auto-enqueue when enabled. Other actions ignored.
  - `workflow_run` `completed`+`failure` → Task `RUNNING` (`trigger_type: ci`) + `TASK_CREATED` + `CI_CONTEXT_LOADED`, auto-enqueue. Other conclusions ignored. Active same repo+sha+job tasks are reused (`duplicate: active_task`).
  - `check_run` `completed`+`failure` → same as above for check runs.
  - Returns `{ok, task_id?, queued?, duplicate?}` or `{ok, ignored}`. Auth failures return 401/400 JSON.

## Repositories and GitHub data

- `GET /api/repositories` → up to 200 repos `{id, github_full_name, default_branch, connected}` (`connected` = installation ID stored).
- `POST /api/repositories/connect` `{github_full_name: "owner/repo", installation_id: "…"} ` → `{id, github_full_name, connected: True}`. 400 when the name or installation ID is missing/invalid.
- `GET /api/github/installations` → App installations `{id, account, type}[]`, or `{error}` with 400 (no App key) / 502 (GitHub API failure).
- `GET /api/github/repos` → installations grouped with repos `{github_full_name, private, default_branch, connected, installation_id}`. Per-installation failures degrade to `[]`, never raise.
- `GET /api/github/issues?repo=owner/repo` → live open issues (PRs excluded) for a connected repo, or 400/502 JSON when unconnected/unreachable.
- `POST /api/tasks/from-issue` `{repository, issue_number}` → `{task_id}`. Creates a `RUNNING` task from the live issue title/body/URL plus `TASK_CREATED`. 400 when the repo is unconnected or the payload is invalid.

## Tasks

- `GET /api/tasks` → up to 200 task summaries `{id, repository, trigger_type, issue_number, issue_title, status, branch, commit_sha, pr_number, pr_url, error, created_at, updated_at}`.
- `GET /api/tasks/{id}` → summary plus `events: [{type, data, at}]` (operational trace, no chain-of-thought) and `memory: [{path, summary}]` for the task repository.
- `POST /api/tasks/{id}/run?sync=false` → default enqueues to Redis (`{task_id, queued: True, job_id}`); `?sync=true` runs inline (`{task_id, result, sync: True}`). When Redis is unavailable, falls back to inline and returns `{queue_fallback}`. 404 when the task is missing.
- `POST /api/tasks/{id}/cleanup` → `{task_id, removed}` (manual workspace wipe).
- `POST /api/tasks/{id}/cancel` → `{task_id, status, cancel_requested? | already_terminal?}`. Sets a flag the agent loop polls every iteration and before every tool call.

## IDE (task-workspace-scoped)

All routes below resolve repository-relative paths through the shared jail and block sensitive files. Expired/missing workspaces return 410; unknown tasks return 404.

- `GET /api/tasks/{id}/files?path=.` → `{path, entries: [{name, is_dir, size}]}` (`.git` hidden, max 1000 entries).
- `GET /api/tasks/{id}/file?path=…&offset=1&limit=500` → bounded file read (limit 1–2000, 200KB cap).
- `PUT /api/tasks/{id}/file` `{path, content}` → save file inside the task workspace.
- `GET /api/tasks/{id}/diff` → git status plus bounded unified diff for review.
- `GET /api/tasks/{id}/events?after=0` → agent trace poll for TraceView.
- `GET /api/tasks/{id}/verification` → gate/verification view data.
- `POST /api/tasks/{id}/terminal` `{command}` → sandboxed command (same denylist, env scrub, path jail, and output caps as agent tools).
- `POST /api/tasks/{id}/chat` `{message}` → append a user note to the task trace.
- `GET /api/tasks/{id}/chat/stream` → SSE stream of new events.
- `POST /api/tasks/{id}/approve` `{title?, body?}` → publish a `NEEDS_REVIEW` (also `FAILED`/`RUNNING` with workspace) task: commit+push+create-or-update-PR on the task's deterministic branch, then cleanup. Returns the publish result or `{status, pr?, branch?}`.

## Status and event vocabulary

- Task `status`: `RUNNING` | `NEEDS_REVIEW` | `AWAITING_CI` | `CANCELLED` | `COMPLETED` | `FAILED` | `BLOCKED`.
- Selected `task_events.type`: `TASK_CREATED`, `CI_CONTEXT_LOADED`, `QUEUED`, `QUEUE_FAILED`, `AGENT_STARTED`, `TOOL_CALL`, `FILE_CHANGED`, `COMMAND_RUN`, `CI_CHECKOUT`, `AGENT_FINISHED`, `VALIDATION_STARTED`, `VALIDATION_FAILED`, `VALIDATION_PASSED`, `COMMIT_CREATED`, `PR_CREATED`, `AWAITING_CI`, `CI_PASSED`, `CI_FAILED`, `CI_REPAIR_STARTED`, `CI_REPAIR_WORKSPACE`, `SUPERSEDED`, `NEEDS_REVIEW`, `CANCELLED`, `CANCEL_REQUESTED`, `FAILED`, `BLOCKED`.
