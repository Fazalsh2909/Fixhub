# Spec: Fixhub — GitHub-Native Autonomous AI Software Engineer

## Objective
Build Fixhub: connect GitHub App → understand repo → investigate issue → reproduce → fix in Docker sandbox → verify (tests/lint/type/build/scan) → memory update → policy-gated verified PR with Proof of Fix. Demo mode works with zero GitHub creds. Token-efficient via repo index + selective memory retrieval.

Users: (1) repo owner fixing issue #N via manual task or `fixhub-fix` label webhook; (2) hiring reviewer running demo + eval; (3) future automation (CI fail, dependabot, `/fix` comments — stubbed event types).

## Tech Stack
- Backend: Python 3.11, FastAPI 0.115, SQLAlchemy 2.0, SQLite (dev/demo) → Postgres 16 (prod), Redis (queue dev) / SQS (prod stub), Docker 29 per-task sandbox
- Frontend: React 19 + TypeScript 5 + Vite 6 + Monaco Editor, Tailwind
- LLM: `LLMProvider` ABC → `OpenRouterProvider` (OpenAI-compatible `/chat/completions` + tools; default `base_url=https://api.tokenrouter.com/v1`, model `z-ai/glm-5.3-free`; also `openai` / `experiential` / `bynara` / `xkiro` via `LLM_PROVIDER`); keys encrypted, never logged, never in sandbox
- Tests: pytest (backend), vitest (frontend), Playwright (e2e/demo)

## Commands
```bash
# backend
cd backend; pip install -r requirements.txt; uvicorn app.main:app --reload --port 8000
pytest -q                                    # full backend suite
pytest tests/test_policy.py -q               # focused example
ruff check . && ruff format --check .; mypy app
# frontend
cd frontend; npm install; npm run dev        # :5173
npm run build; npm run test; npx tsc --noEmit
# demo
cd demo/fastapi-jwt; pip install -r requirements.txt; pytest -q
# e2e
npx playwright test e2e/demo.spec.ts
# infra
docker compose -f infra/docker-compose.yml up --build
```

## Project Structure
```
backend/app/ → main, config, db, logging, queue, github/, repo/, intel/, memory/, tools/, llm/, agent/, sandbox/, verify/, policy/, eval/
frontend/src/ → IDE layout (activity bar, explorer, Monaco, right AI panel, bottom terminal/tests/problems/git/diff/trace)
demo/fastapi-jwt/ → preloaded JWT bug repo (500 vs 401)
infra/ → docker-compose.yml, terraform stub
e2e/ → Playwright specs
```

## Code Style
```python
# backend: typed, small funcs, structured logs, no secrets in logs
def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
```
- Python: ruff + mypy strict on new code. TS: strict, no `any` without justification. Never expose chain-of-thought; emit engineering events only.

## Testing Strategy
- pytest: unit (policy, memory freshness, webhook verify, publisher guard) + integration (webhook→queue→task row, sandbox run echo) + demo regression (FAIL-before/PASS-after real pytest)
- vitest: component render (explorer, proof panel)
- Playwright: demo autonomous-fix flow, IDE smoke, webhook 401 path. No fabricated results — every PASS maps to real exit code/output artifact.

## Boundaries
- Always: raw-body-then-HMAC-verify webhooks; ack <10s then queue; idempotency on `X-GitHub-Delivery`; run untrusted code only in Docker sandbox; policy gate before any GitHub write; update memory with provenance; redact secrets in logs/telemetry.
- Ask first: DB schema change, new dependency, new webhook event, new tool capability, CORS change.
- Never: give agent a general GitHub-write tool; modify default branch; log keys/tokens/chain-of-thought; commit secrets; fake test/PR/trace results; `npm audit fix --force` blindly.

## Success Criteria
- [ ] Demo: Start Autonomous Fix on JWT issue → reproduce FAIL → regression test → fix → PASS → proof panel + diff, no GitHub creds
- [ ] Webhook: signed `issues.labeled(fixhub-fix)` → task row CREATED → orchestrator resumable through TESTING crash
- [ ] Publisher: only `create_branch/push verified patch/create_pr` from VerifiedArtifact; default-branch write DENY tested
- [ ] Memory: 7 types with provenance + STALE on file change; retrieval injects only relevant slice
- [ ] Playwright e2e green; pytest green; `tsc` + `vite build` green

## Open Questions
- GitHub App id/slug + webhook URL for local tunneling (ngrok/smee) — owner to provide at install time.
- tokenrouter API key storage: env for dev, Secrets Manager for prod — confirmed pattern, key to be supplied via env.
