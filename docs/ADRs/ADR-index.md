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
