# Contributing to FixHub

## Quickstart (Docker, recommended)

```powershell
copy backend\.env.example backend\.env   # fill LLM + GitHub App values
docker compose up --build
# backend: http://localhost:8001/health  (host 8001 -> container 8000, see docs/SETUP.md)
# frontend: http://localhost:8080
```

Full setup, configuration table, and GitHub App steps: `docs/SETUP.md`. API reference: `docs/API.md`. Architecture: `docs/architecture.md`.

## Local dev (no Docker)

```powershell
cd backend
python -m venv .venv; .\.venv\Scripts\Activate
pip install -r requirements.txt
copy .env.example .env
python -m pytest tests/ -q
python -m uvicorn app.main:app --port 8000
# worker (needs Redis): python -m app.tasks.worker
```

Frontend:

```powershell
cd frontend
npm install
npm run dev   # :5173 proxies /api to :8000
npm run build # tsc + vite build -> dist/ (served by nginx in Docker)
```

## Rules

- One attempt per task, no hidden retries. Retry via `POST /api/tasks/{id}/run`.
- Webhooks enqueue to Redis; the worker executes. `?sync=true` runs inline (tests/dev).
  Never let a webhook handler block on the LLM loop.
- FixHub owns git: the LLM never branches/commits/pushes. See `backend/app/github/publisher.py`.
  With `AUTO_PUBLISH=0`, runs stop at `NEEDS_REVIEW`; publish via `approve_task()` /
  `POST /api/tasks/{id}/approve` only.
- Every task workspace is fresh: `WORKSPACE_ROOT/task-{id}` is wiped on clone
  and removed after terminal states. Never reuse a workspace across tasks.
  `NEEDS_REVIEW` keeps the workspace for IDE review; `approve` cleans up.
- IDE APIs (`backend/app/api/ide.py`) share the agent's path/sensitive-file guards.
  Terminal runs through the same sandbox denylist + env scrub.
- Memory is prior knowledge, not truth: verify files before trusting it.
- Never log secrets, tokens, or clone URLs.
- Keep PRs small and add/extend a test under `backend/tests/` for behavior changes.
- Run before opening a PR: `python -m pytest tests/ -q` from `backend/`, plus `python tests/benchmark/runner.py` when the agent loop, tools, sandbox, gates, or task lifecycle changed. Frontend changes need `npm run build` (`tsc --noEmit` + `vite build`) clean from `frontend/`.
- Do not redesign the agent in a docs/OSS PR. This implementation is frozen; behavior changes need a failing test that proves a concrete regression first.
- Documentation claims must match the implementation: link the file and line that proves each behavior, label localhost screenshots as local, and list limitations instead of removing them.
