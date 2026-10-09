# Final Hardening Before Phase 5 — Blocker Report

Pass complete. Phase 5 (sandbox hardening) NOT started. Evidence below is
from this pass only; prior phases keep their own reports.

## Verification evidence

- Full suite: **267 passed** (`python3 -m pytest tests/ -q`), 0 failures.
  11 errors are environmental only: `test_phase4_pg.py` (10) + redis-outage
  (1) require Docker (`docker info` fails: daemon not running). Same tests
  error identically without these changes.
- Deterministic benchmark: **0 FAIL** lines across all scenarios
  (incl. P1-01..P1-12, K-repair-live, S/R/F/K probes).
- New tests: `test_lease_fencing.py` (6), `test_usage_limits.py` (7),
  `test_hardening_config.py` (16) — all pass.
- Live GitHub recovery test: implemented opt-in, skips cleanly without
  credentials (2 skipped). NOT executed against a real repo (no disposable
  repo configured yet) — see residual R3.
- Ruff: touched files clean; remaining hits pre-exist (`ide.py`,
  `client.py` E741, `webhook.py`, test F401s, `conftest.py` E402).

## What changed

1. **Lease/fencing** — `lease_guard` before every LLM request + after every
   tool (`agent/loop.py` → `LoopResult.lease_lost`); post-long-tool
   renew/revalidate (`TOOL_CLEANUP_GRACE_S=30`); split publish fence: entry +
   before-push + push-vs-PR (`github/publisher.py` `lease_check`);
   `docs/LEASE_FENCING.md`.
2. **Live recovery test** — `tests/test_github_recovery_live.py` (opt-in via
   `FIXHUB_LIVE_TEST=1` + `FIXHUB_TEST_REPO` + `FIXHUB_TEST_TOKEN`;
   ignored by default suite); `docs/LIVE_GITHUB_TEST.md`.
3. **Timeout cleanup** — one ladder + `validate_timeout_ladder()` /
   `validate_usage_limits()` (fail-fast); `DEV_SLOW_MODEL` explicit dev flag;
   legacy `1500/1800` removed; `docs/TIMEOUTS.md`.
4. **Usage limits** — `MAX_LLM_REQUESTS_PER_TASK=40`,
   `MAX_TASKS_PER_USER_PER_DAY=20`, optional token caps (nullable, never
   fabricated); request-guard per LLM call; `USAGE_LIMIT_HIT` events;
   enqueue + pre-run + per-round enforcement, DB-backed (multi-instance
   safe); owner-scoped accounting (no global counter).
5. **Prod fail-closed** — guards now also reject unbounded workers, bad
   ladders; no silent fallbacks (Redis→QUEUED+error, BYOK→BLOCKED,
   owner from session only).

## Remaining limitations

### CRITICAL — none known.

### HIGH

- **H1. Live recovery unproven on real GitHub.** Push-then-crash reconcile
  and 422-relist paths are covered by mocks + an unexecuted opt-in test.
  Mitigation: run `test_github_recovery_live.py` once a disposable repo +
  token exist. Until then, treat first production crash-recovery as
  observe-closely.

### MEDIUM

- **M1. One duplicated LLM request is still theoretically possible** during
  a pathological worker/network partition (lease expires mid-request, the
  replacement repeats the call). Bounded to wasted tokens: push/PR fences
  guarantee no duplicated side effect. This is inherent to at-least-once
  execution and is NOT claimed away.
- **M2. Slow-not-dead repeats LLM spend.** A live-but-slow worker can be
  reaped; its replacement re-spends. Narrowed (heartbeat + post-tool
  renewal), not eliminated.
- **M3. Push-then-crash relies on reuse correctness.** Reconciles via
  branch checkout + `_find_open_pr` + 422-relist + PR-row upsert. Mock-
  proven; live-proven only after H1 closes.
- **M4. PG/Redis integration unexecuted here.** Docker daemon was down, so
  10/20-simultaneous-task, lease-race-on-PG, and redis-outage proof did not
  run in this pass (they passed in Phase 4.5 with Docker up). Re-run with
  Docker before production cutover.

### LOW / ACCEPTABLE RESIDUAL

- **L1. Clock skew** between API/worker hosts is assumed NTP-bounded, not
  proven. Comparisons use a single DB `utcnow()` on both sides.
- **L2. Token caps depend on provider honesty.** Missing usage → non-token
  limits still bind; fabricated counts are never invented.
- **L3. Dev ladder (`DEV_SLOW_MODEL=1`, 80 iterations) is intentionally lax.**
  Prod ignores the flag and enforces the strict ladder. Never copy dev
  values to prod.
- **L4. Legacy `on_event` + per-request-cookie deprecation warnings.**
  Cosmetic (FastAPI/Starlette deprecations); no behavior impact.

## Production cutover checklist

1. Set strict env: `ENV=prod`, Postgres `DATABASE_URL`, `AUTH_COOKIE_SECURE=1`,
   `FIXHUB_CREDENTIAL_ENCRYPTION_KEY`, `ADMIN_EMAILS`; leave usage/timeout
   defaults unless deliberately tuned (startup validates).
2. Start Docker Desktop; re-run `test_phase4_pg.py` + redis-outage + 10/20
   concurrency modules green.
3. Configure disposable repo + token; run live recovery test green.
4. Observe first crash-recovery and first usage-limit hit in logs
   (`LEASE_LOST`, `USAGE_LIMIT_HIT` events) before declaring steady-state.
