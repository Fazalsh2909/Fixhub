# Fixhub — GitHub-native autonomous AI software engineer

![Python 3.11](https://img.shields.io/badge/python-3.11-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![React 19 + TS](https://img.shields.io/badge/frontend-React19%2BTS-blue)
<!-- CI badge: push to GitHub, then add [![CI](https://github.com/<OWNER>/<REPO>/actions/workflows/ci.yml/badge.svg)](https://github.com/<OWNER>/<REPO>/actions/workflows/ci.yml) -->

Connect GitHub → understand repo → reproduce issue → fix in a Docker sandbox → verify with real tests → open a policy-gated PR with **Proof of Fix**. Demo mode runs with **zero GitHub creds**.

A chatbot stays connected to your GitHub App: pick a repo (or clone any public
`github.com` repo), say `list issues` / `fix #N`, review the diff in-app, then
**Approve & Commit**. Nothing pushes to GitHub before your approval.
Setup: [`docs/GITHUB-CONNECT.md`](docs/GITHUB-CONNECT.md).

> Push to GitHub (`git remote add origin <url>; git push -u origin main`), then add the CI badge above with your `<OWNER>/<REPO>`.

## Demo (2 min)

1. `cd backend; pip install -r requirements.txt; uvicorn app.main:app --port 8001`
2. `cd ../frontend; npm install; npm run dev` → open http://localhost:5174
3. Press **Start Autonomous Fix** → watch Agent Trace → Verification (`suite: PASS`) → Proof of Fix.

No LLM key? You get the honest **verification-only path** (real pytest output, `verified:false`-aware UI). With `TOKENROUTER_API_KEY` (or `OPENAI_API_KEY` + `LLM_PROVIDER=openai`, `BYNARA_API_KEY` + `LLM_PROVIDER=bynara`, `XKIRO_API_KEY` + `LLM_PROVIDER=xkiro`) you get the full agent loop.

Documented live run: [`docs/DEMO-RUN-2026-09-04.md`](docs/DEMO-RUN-2026-09-04.md) — real Docker FAIL → fix → PASS (tasks 17→21), including every bug found in Fixhub itself. Eval table: [`docs/EVAL.md`](docs/EVAL.md) — currently **1/3 attempted PASS** (honest count; pagination runs blocked by no Docker daemon on Windows, documented with task traces).

Proof assets (all real, captured 2026-09-17): [`docs/assets/ui-shell.png`](docs/assets/ui-shell.png) (live UI, Playwright e2e green), [`docs/assets/metrics.json`](docs/assets/metrics.json) (`GET /metrics`), [`docs/assets/architecture.mmd`](docs/assets/architecture.mmd) (render at mermaid.live).

## Architecture

```
GitHub App (issues read, contents read, PRs write-scoped)
  → Event Gateway (FastAPI /webhooks/github: raw-body HMAC, idempotent on X-GitHub-Delivery)
  → Queue (Redis / in-memory fallback) → Orchestrator (resumable state machine)
  → Agent Worker (bounded LLM tool loop, max 12 iters) → Sandbox (Docker per task)
  → Verification (repro → regression FAIL→PASS → suite/lint) → Proof of Fix
  → Policy Engine → PR Publisher (VerifiedArtifact only, never default branch)
         ↕                    ↕
  Repo Intel (ast+regex index) + Engineering Memory (7 types, STALE on change)
```

Details: [`docs/architecture.md`](docs/architecture.md) · Spec: [`docs/SPEC-fixhub.md`](docs/SPEC-fixhub.md)

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2.0, SQLite (dev) → Postgres 16 (prod), Redis queue |
| Frontend | React 19 + TypeScript 5 + Vite 6 + Monaco, live polling, trace/tests/diff/proof tabs |
| LLM | `LLMProvider` ABC → OpenAI-compatible `/chat/completions` (TokenRouter default, OpenAI supported via `LLM_PROVIDER=openai`); retries, `reasoning_content` fallback, usage/cost in `/metrics` |
| Sandbox | Docker per-task (cpu/mem/pids caps, scrubbed env, `--rm`), local-fallback with same interface |
| Tests | pytest (74 backend), vitest (8 frontend), Playwright e2e, `demo/*` regression FAIL-by-design |

## Quickstart

```bash
# backend
cd backend; pip install -r requirements.txt; uvicorn app.main:app --port 8001
pytest -q                                    # 72 passed
ruff check app && ruff format --check app; mypy app

# frontend
cd ../frontend; npm install; npm run dev     # :5174, proxies /api → :8001
npm run test; npx tsc --noEmit; npm run build

# demos (both FAIL by design — that's the point)
cd ../demo/fastapi-jwt; pip install -r requirements.txt; pytest -q          # 500 vs 401
cd ../fastapi-pagination; pip install -r requirements.txt; pytest -q        # off-by-one

# e2e (needs both servers up)
npx playwright test e2e/demo.spec.ts

# prod-ish
docker compose -f infra/docker-compose.yml up --build
```

Env: copy `backend/.env.example` → `backend/.env`, set `TOKENROUTER_API_KEY` or `OPENAI_API_KEY`. Never commit `.env` (gitignored). If you ever exposed a key, rotate it immediately.

## API

| Endpoint | What |
|---|---|
| `GET /health` | status + active provider/model |
| `GET /metrics` | llm_calls, tokens, tool_calls, tasks_run/verified, est_cost_usd, avg_latency_ms |
| `GET /api/provider` | provider, model, has_key (no secrets) |
| `GET /api/tasks`, `GET /api/tasks/{id}` | list + detail (events, verification rows, memory slice, diff, approvals) |
| `POST /api/demo/trigger` | run autonomous fix (rate-limited) |
| `POST /api/tasks/{id}/run` | run agent on any chat/webhook task (resolves clone workspace) |
| `POST /api/tasks/{id}/approve` | Approve & Commit — policy-gated push + PR (or local commit record) |
| `POST /api/tasks/{id}/reject` | request changes → back to DEBUGGING |
| `POST /api/chat` | coding assistant — plain-words work orders auto-run (`add dark mode`, `fix #N`, `run`, `status`) |
| `GET /api/automation` | automation flags + provider/key readiness (no secrets) |
| `GET /api/github/status` | App configured? installations? connected repos? (no secrets) |
| `GET /api/github/repos?installation_id=` | live installation repo list |
| `POST /api/github/connect` | connect a repo to the debugger |
| `GET /api/github/repos/issues` | poll open issues (localhost-safe webhook fallback) |
| `POST /api/github/from-issue` | create fix task from a live issue |
| `POST /api/github/clone` | clone any public github.com repo (guarded) + index |
| `GET /api/eval` | eval tasks + honest recorded results |
| `POST /webhooks/github` | HMAC-verified GitHub gateway (label/`/fix`/auto on connected repos) |

## Stay-connected flow

1. Create + install the GitHub App ([guide](docs/GITHUB-CONNECT.md)), paste the
   installation id into the left panel — header shows `App ✓`.
2. Connect repos (or clone OSS via URL). Just tell the chat the work —
   `add dark mode`, `fix the login redirect`, `fix #N` — the agent starts
   immediately (`AUTO_RUN`, default on) and you watch the Agent Trace.
3. Trace → Verification → **Diff** tab.
4. **Approve & Commit** opens the PR with Proof of Fix; **Request changes** loops back.
   No installation attached (demo/clone)? Approval is recorded locally with the branch.
   Set `AUTO_PR_ON_VERIFIED=true` for hands-free PRs on verified runs
   (still branch-guarded, never the default branch).

## Security model

- Read plane (installation token) never enters sandbox env; write plane is 3 functions on `VerifiedArtifact` only.
- Sandbox: no `--privileged`, no host socket, egress allowlist, always cleaned up. NOT a microVM (prod path documented in `backend/app/sandbox/README.md`).
- Secrets from env → Secrets Manager in prod; Fernet-encrypted `llm_providers` row; redaction filter on logs; LLM output treated as untrusted.
- Policy engine denies default-branch writes (tested); `edit_file` is workdir-jailed + AST-gated.

## Limitations (stated, not hidden)

- Memory retrieval is keyword-overlap, not embeddings — cheap and explainable, weaker on paraphrase.
- Verification scaffold runs suite+lint; type/build/scan/adversarial are recorded lanes to fill per repo.
- Free-tier models rate-limit; budgets/retries/cached intel mitigate, paid models via `LLM_PROVIDER=openai`.

## Project structure

```
backend/app/ → main, config, db, metrics, logging, queue, github/ (webhook, api, app_auth, read_client, publisher), repo/ (workspace, clone_guard), intel/, memory/, tools/, llm/, agent/, sandbox/, verify/, policy/, eval/, chat/, review/
frontend/src/ → App (debugger chatbot + review), lib/api, lib/tasks (+tests)
demo/fastapi-jwt/ → JWT 500-vs-401 bug · demo/fastapi-pagination/ → off-by-one bug
infra/ → docker-compose.yml, sandbox.Dockerfile, main.tf (stub)
e2e/ → Playwright specs · docs/ → SPEC, architecture, DEMO-RUN, EVAL
```
