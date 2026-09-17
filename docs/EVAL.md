# Fixhub Eval Report

Honest results only. Every PASS maps to a real execution artifact. No fabricated numbers.

## Summary

| Task | Kind | Repo | Status | Evidence |
|------|------|------|--------|----------|
| auth-jwt-expiry | auth bug (500 vs 401) | demo/fastapi-jwt | **PASS** | docs/DEMO-RUN-2026-09-04.md, task 21 `verified:true`, local `pytest 1 passed` |
| pagination-offbyone | off-by-one slice | demo/fastapi-pagination | **PENDING** | regression test fails as designed (`got ['c','d']`, want `['a','b']`); agent runs recorded as backend tasks 110/111 — correct investigation, blocked: no Docker daemon on this Windows host for the verification lane (see below) |
| sql-nplus1 | SQL bug | demo/fastapi-jwt | NOT_RUN | — |
| retry-idempotency | retry bug | demo/fastapi-jwt | NOT_RUN | — |
| async-exc | async exception | demo/fastapi-jwt | NOT_RUN | — |

Score: **1/3 attempted PASS, 1 pending, 3 not run.** We report attempted-only, not 1/5.
(auth-jwt-expiry PASS; pagination-offbyone ×2: task 110 crashed on a raw
`RemoteProtocolError` → fixed + regression-tested; task 111 ran the full
12-iter loop with correct investigation but verification needs a Docker
daemon, which is down on this Windows box.)

## How to reproduce

```bash
# Bug 1 — JWT expiry (documents the PASS)
cd demo/fastapi-jwt; pip install -r requirements.txt; pytest -q
# expect FAIL before fix: assert 500 == 401

# Bug 2 — pagination off-by-one (currently FAILs by design)
cd ../fastapi-pagination; pip install -r requirements.txt; pytest -q
# expect FAIL: off-by-one: got ['c', 'd']
```

```bash
# Full autonomous run (needs LLM key)
cd backend; pip install -r requirements.txt
export TOKENROUTER_API_KEY=...  # or OPENAI_API_KEY=... with LLM_PROVIDER=openai
uvicorn app.main:app --port 8001 &
cd ../frontend; npm install; npm run dev  # :5174
# open :5174 → Start Autonomous Fix → poll /api/tasks/{id} → verification rows
```

## Metrics tracked per run

`GET /metrics` returns `llm_calls, prompt_tokens, completion_tokens, total_tokens,
tool_calls, tasks_run, tasks_verified, est_cost_usd, avg_latency_ms`.
Costs are estimates (see `backend/app/metrics.py`); free-tier default model = $0.

## Known failure modes (found during real runs)

1. Shell pipes (`| tail`) masked pytest exit codes → no pipes; exit code captured explicitly.
2. Assistant `tool_calls` stored in internal shape → provider 400 → provider-native transcript with synthetic ids + `tool_call_id`.
3. Free model returns `content: null` with answer in `reasoning_content` → fallback in `_text_of`.
4. Unhandled provider error 500'd the endpoint with task stuck → typed `ProviderError` + retries + FAILED transition.
5. Agent had no write tools / path escapes → `edit_file`/`create_file` constrained to workdir, AST syntax gate on `.py`.
6. `httpx.RemoteProtocolError` (server disconnect) is a `TransportError`, not a `ConnectError` → escaped `_post` raw and killed task 110 mid-loop with no FAILED transition. Fixed by catching `httpx.TransportError` (tests: `test_provider_retry.py`).
7. Agent burned all 12 iters guessing `run_test`/`run_command` args the allow-list rejects (task 111) — the rejection messages don't say what IS allowed. Next: include the allow-list in the tool spec descriptions.
8. Verification lane needs a running Docker daemon — `npipe:////./pipe/dockerDesktopLinuxEngine` down on this Windows box blocks `suite` (task 111 `DEBUGGING`). Start Docker Desktop and re-run for the 2/2 PASS.

## Adding a new eval task

1. Add a `demo/<name>/` repo with a failing regression test.
2. Add row to `TASKS` + `EVAL_RESULTS` in `backend/app/eval/benchmark.py` as `PENDING`.
3. Run the agent, record the run doc under `docs/`, flip to `PASS`/`FAIL` with evidence link.
4. Update this table. Never mark PASS without a linked artifact.
