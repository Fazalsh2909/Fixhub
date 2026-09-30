# FixHub setup (current implementation)

No deployed demo exists. Everything below runs locally.

## Option A — Docker (supported install)

Prerequisites: Docker + Docker Compose.

```powershell
copy backend\.env.example backend\.env
# edit backend\.env: set LLM key + GitHub App values (see Configuration)
# place the GitHub App .pem next to backend\.env as ./fixhub-app.pem
docker compose up --build
```

What you get (host ports):

- Frontend IDE: `http://localhost:8080`
- Backend: `http://localhost:8001/health` (host `8001` → container `8000`; see port note)
- Tasks: `http://localhost:8001/api/tasks`
- Queue: `http://localhost:8001/api/queue/health`

Containers: `backend` (FastAPI on container port 8000), `worker` (`python -m app.tasks.worker`), `redis:7-alpine`, `frontend` (nginx serving `dist/`). Shared volumes: `workspaces` (`/tmp/fixhub-workspaces`), `redisdata`, `dbdata` (`/srv/backend/data/fixhub.db`). The App private key is mounted read-only and excluded from the image by `backend/.dockerignore`.

Scale workers (untested under load):

```powershell
docker compose up --scale worker=3
```

Reset local state (removes workspaces, Redis data, and the SQLite file):

```powershell
docker compose down -v
```

### Port note (read this before reporting a bug)

`docker-compose.yml` maps host `8001` to container `8000` because port 8000 was already taken on the author's machine (`infra-api`). Inside Docker, nginx proxies to `http://backend:8000`. In local dev, the backend listens on `http://localhost:8000` and the Vite dev server (`frontend/vite.config.ts`) proxies `/api`, `/webhooks`, and `/health` there. If `http://localhost:8001/health` fails in Docker, check `docker compose ps` and container logs; if `:8000` fails locally, confirm `uvicorn` is running on port 8000.

## Option B — Local dev (no Docker)

Backend (Python 3.12, see `backend/Dockerfile` and `.github/workflows/ci.yml`):

```powershell
cd backend
python -m venv .venv; .\.venv\Scripts\Activate
pip install -r requirements.txt
copy .env.example .env
# set WORKSPACE_ROOT=./workspaces for local runs
python -m pytest tests/ -q
python -m uvicorn app.main:app --port 8000
# worker in a second shell (needs Redis): python -m app.tasks.worker
```

Frontend (Node 20, see `frontend/Dockerfile`):

```powershell
cd frontend
npm install
npm run dev   # http://localhost:5173, proxies /api to localhost:8000
npm run build # tsc --noEmit + vite build -> dist/
```

Without Redis, use inline runs: `POST /api/tasks/{id}/run?sync=true`.

## GitHub App (required for live issue/CI runs)

1. Create a GitHub App in your account/org settings.
2. Permissions (from `README.md` + webhook/client usage): Contents read/write, Issues read, Pull requests read/write, Actions read, Checks read.
3. Webhook URL: `https://<your-public-host>/webhooks/github` (use a tunnel such as ngrok for local testing). Generate a webhook secret.
4. Install the app on your test repo (`owner/repo`). Note the installation ID (numeric, visible in the install URL or via `GET /api/github/installations`).
5. Configure `backend/.env` (see below) and restart the stack.
6. Connect the repo so FixHub stores the installation mapping (required — without it runs stop at `BLOCKED: no clone source`):
   - IDE Repositories panel → Connect button, or
   - `POST /api/repositories/connect` with `{github_full_name: "owner/repo", installation_id: "…"}`, or
   - SQL: `UPDATE repositories SET installation_id='<install-id>' WHERE github_full_name='owner/repo';`
7. Verify: `GET /api/github/repos` lists the installation, `GET /api/github/issues?repo=owner/repo` lists open issues, `GET /api/queue/health` shows `{ok: True}`.

For forks of OSS repos outside your installations, the IDE manual owner/repo + installation-ID form remains available.

## Configuration (`backend/.env.example` + `backend/app/config.py`)

| Variable | Default / example | Meaning |
| --- | --- | --- |
| `ENV` | `dev` | `dev` or `prod`. Returned by `/health`. |
| `DATABASE_URL` | `sqlite:///./fixhub.db` | Docker overrides to `sqlite:////srv/backend/data/fixhub.db`. Postgres prod string supported. |
| `LLM_PROVIDER` | `bynara` | `bynara` or `xkiro`. Both are OpenAI-compatible and must support `tools` + `tool_choice`. |
| `BYNARA_BASE_URL` / `BYNARA_API_KEY` / `BYNARA_MODEL` | `https://router.bynara.id/v1` / empty / `nemotron-3.5-lightning-free` | Active when `LLM_PROVIDER=bynara`. |
| `XKIRO_BASE_URL` / `XKIRO_API_KEY` / `XKIRO_MODEL` | `https://api.xkiro.com/v1` / empty / `qwen/qwen3-coder-plus:free` | Active when `LLM_PROVIDER=xkiro`. |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | bynara defaults / empty | Legacy trio: when `LLM_API_KEY` is set it wins over `LLM_PROVIDER`. |
| `LLM_TIMEOUT_S` | `120` | Per chat-completion HTTP timeout. |
| `LLM_RETRY_ATTEMPTS` / `LLM_RETRY_BASE_S` | `4` / `20.0` | Backoff for transient 429/5xx/transport blips and 200-with-error envelopes. |
| `LLM_MAX_ITERATIONS` | `40` | Agent loop cap. |
| `LLM_MAX_RUNTIME_S` | `900` | Agent loop wall-clock cap. |
| `JOB_TIMEOUT_S` | `1200` | RQ job timeout. |
| `COMMAND_TIMEOUT_S` | `180` | Default per-command sandbox timeout. |
| `LLM_MAX_REWRITES_PER_PATH` | `8` | Rewrite convergence cap per file. |
| `LLM_HISTORY_GROUPS` | `12` | Tool-exchange blocks resent per LLM call. |
| `TOOL_OUTPUT_MAX_BYTES` | `20000` | Tool/sandbox output cap. |
| `LLM_MAX_OUTPUT_BYTES` | `200000` | Agent summary cap. |
| `WORKSPACE_ROOT` | `./workspaces` locally, `/tmp/fixhub-workspaces` in Docker | Per-task clone root (`task-{id}`). |
| `WORKSPACE_KEEP` | `0` | `1` keeps workspaces after terminal states for debugging. |
| `GITHUB_APP_ID` | empty | GitHub App ID (not installation ID). |
| `GITHUB_APP_PRIVATE_KEY` / `GITHUB_APP_PRIVATE_KEY_PATH` | empty / `./fixhub-app.pem` | Prefer the mounted file; never paste multiline PEM into issues/logs. |
| `GITHUB_WEBHOOK_SECRET` | empty | Must match the GitHub App webhook secret. |
| `GITHUB_API_URL` | `https://api.github.com` | Override for GHES. |
| `REDIS_URL` | `redis://localhost:6379/0` locally, `redis://redis:6379/0` in Docker | Queue connection. |
| `QUEUE_NAME` | `fixhub` | RQ queue name. |
| `AUTO_RUN_ON_WEBHOOK` | `1` | `0` creates tasks without auto-enqueue. |
| `AUTO_PUBLISH` | `1` | `0` stops runs at `NEEDS_REVIEW` for IDE approval. |

Never commit `backend/.env`, `*.pem`, `*.key`, or `*.db`. All are gitignored and dockerignored.
