# ADR-001: GitHub App (not PAT) from day one
Date: 2026-09-04. Status: accepted.
Context: need least-privilege, installation tokens, webhook secret, marketplace path.
Decision: GitHub App with perms issues:read, contents:read, PRs:write (publisher only), events issues/issue_comment/check_run.
Consequences: local dev needs tunnel (ngrok/smee); slower setup but correct job story.

# ADR-002: FastAPI + Docker per-task sandbox
Date: 2026-09-04. Status: accepted.
Context: spec mandates real sandbox; have Docker 29 + Python 3.11.
Decision: FastAPI backend; each task gets throwaway container, no host socket, scrubbed env, timeouts, cleanup.
Consequences: Windows Docker tuning needed; not microVM — document limits.

# ADR-003: LLM via tokenrouter (OpenRouter-compatible), model z-ai/glm-5.3-free
Date: 2026-09-04. Status: accepted.
Context: user key is tokenrouter; wants swappable models.
Decision: OpenRouterProvider with configurable base_url + model; OpenAI-compatible tools/JSON mode.
Consequences: free-tier rate limits → budgets/retries/backoff; never depend on one vendor string.

# ADR-004: SQLite dev → Postgres prod, one SQLAlchemy model set
Date: 2026-09-04. Status: accepted.
Context: keep dev cost zero, prod AWS-ready.
Decision: same models both DBs; SQLite for demo/tests, Postgres in compose/prod.

# ADR-005: Attribution-based verification (baseline comparison, not exit codes)
Date: 2026-09-23. Status: accepted.
Context: every required-gate FAIL (even byte-identical to the pre-patch baseline, e.g. MCP stdio ExceptionGroup, asyncpg stub errors) sent the agent back to DEBUGGING; 2/8-gate runs burned full loops with zero progress.
Decision: snapshot a BASELINE verification phase pre-patch; normalize gate outputs to failure signatures (pytest node + error type, mypy file + code); attribute each AFTER failure as TASK/BASELINE/ENV/DEP/INFRA/TIMEOUT/CONFIG/UNRELATED/UNKNOWN deterministically (no LLM verdicts); route to DEBUGGING only on new task-attributed failures; publish accepts VERIFIED and VERIFIED_WITH_LIMITATIONS with documented limitations.
Consequences: verification costs ~2x sandbox time per attempt (bounded by fail-fast single attempt); VerificationRun gains phase/signature/attribution/duration_ms via additive migration 0004; legacy rows without baseline keep exact old semantics.
