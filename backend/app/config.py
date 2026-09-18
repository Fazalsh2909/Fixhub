"""Central config. Secrets come from env only; never logged."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "fixhub"
    # development | production — controls fail-fast secret checks + HSTS.
    app_env: str = "development"
    database_url: str = "sqlite:///./fixhub.db"
    redis_url: str = "redis://localhost:6379/0"
    # Bearer token for mutating API routes (approve/reject/run/trigger/connect).
    # Empty = open (dev/test). Set in prod; enforced when non-empty.
    api_token: str = ""
    github_app_id: str = ""
    github_private_key: str = ""
    github_webhook_secret: str = "dev-secret-change-me"
    # GitHub App connection. Private key via env (PEM) or file path; file wins if set.
    github_app_private_key_path: str = ""
    github_app_slug: str = ""
    # When True, any opened/reopened issue on a connected repo creates a task.
    # Default True: connected repos are stay-connected — the agent starts
    # immediately (AUTO_RUN) without label ceremony. Every run costs LLM
    # calls and is capped by AGENT_MAX_COST_USD; set false for manual triage.
    auto_trigger_on_issue: bool = True
    # Automation: run the agent immediately when a task is created (chat fix,
    # chat instruction, Create button, webhook). Runs in a background thread;
    # the frontend polls the Agent Trace. Default True (Claude-code style).
    # LLM spend note: every auto-run costs model calls — set false for manual.
    auto_run: bool = True
    # When True, a VERIFIED run (real tests PASS + non-empty diff + policy
    # ALLOW + attached GitHub App) opens a PR without a manual Approve click.
    # Pre-authorization is recorded as an AUTO_APPROVED approval row.
    # Default-branch pushes stay DENY no matter what. Default False.
    auto_pr_on_verified: bool = False
    # Generic OpenAI-compatible provider. TOKENROUTER_* kept for backwards compat.
    # LLM_PROVIDER: tokenrouter | openai | experiential | bynara | xkiro | custom
    llm_provider: str = "tokenrouter"
    tokenrouter_base_url: str = "https://api.tokenrouter.com/v1"
    tokenrouter_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    # Experiential Labs gateway (OpenAI-compatible, docs: platform.experientiallabs.ai/docs).
    # Model slugs from GET /v1/models, e.g. glm-5.3. Key is xpl_... (settings/api-keys).
    explabs_base_url: str = "https://api.experientiallabs.ai/v1"
    explabs_api_key: str = ""
    # NaraRouter gateway (OpenAI-compatible, docs: router.bynara.id/docs).
    # Model aliases from GET /api/pricing, e.g. glm-5.3-free. Key is sk-nry-...
    bynara_base_url: str = "https://router.bynara.id/v1"
    bynara_api_key: str = ""
    # xKiro gateway (OpenAI-compatible, docs: docs.xkiro.com).
    # Model ids carry vendor prefix, e.g. minimax/minimax-m3. Key is sk-xt-...
    xkiro_base_url: str = "https://api.xkiro.com/v1"
    xkiro_api_key: str = ""
    # Model id (already vendor-prefixed, e.g. minimax/minimax-m3:free).
    # NEXUS_MODEL is accepted as a fallback alias (old .env name) — FIXHUB_MODEL wins.
    # NOTE: restart uvicorn after editing backend/.env; the running server keeps old values.
    fixhub_model: str = ""
    nexus_model: str = ""
    default_branch_guard: str = "main"
    sandbox_image: str = "python:3.11-slim"
    sandbox_timeout_s: int = 300
    # Comma-separated CORS origins. Local dev defaults; set FRONTEND_URL in prod.
    cors_origins: str = "http://localhost:5173,http://localhost:5174"
    # Simple in-memory rate limit for demo trigger (req/min/IP).
    rate_limit_per_min: int = 20
    # ECC/opencode skill injection for the fix agent. Empty = auto:
    # vendored bundle first, then the local user pack, else silently off.
    skills_enabled: bool = True
    skills_dir: str = ""
    skills_top_k: int = 2
    skills_max_chars: int = 6000
    # Agent auto-retry: total attempts = 1 + this value. Retries on both
    # verification FAIL (DEBUGGING) and retryable provider errors (429/5xx).
    # Non-retryable provider errors (400/401/403) fail fast without retry.
    agent_max_retries: int = 2
    agent_retry_backoff_s: float = 5.0
    # Per-task spend guard. 0 = unlimited (dev). Prod: e.g. 0.50.
    # Enforced in orchestrator before each billable LLM call.
    agent_max_cost_usd: float = 0.0
    # Transcript budget per LLM call (chars, ~4 chars/token). Older tool
    # output is truncated, oldest exchanges dropped — DB keeps the full record.
    agent_context_budget_chars: int = 60000
    # Explore-subagent turns. A runaway explorer is worse than a missing answer.
    subagent_max_turns: int = 6
    # Queue backend: redis (default when reachable) | memory (tests/demo).
    # Prod path: set REDIS_URL to ElastiCache; SQS adapter plugs into queue.py
    # via the same enqueue/dequeue/ack interface (see queue.py docstring).
    queue_backend: str = "auto"

    def resolved_model(self) -> str:
        """Model id to send. FIXHUB_MODEL > NEXUS_MODEL alias > built-in default."""
        return (
            self.fixhub_model.strip() or self.nexus_model.strip() or "z-ai/glm-5.3-free"
        )

    def resolved_llm(self) -> tuple[str, str, str]:
        """Return (base_url, api_key, model) for the selected provider."""
        model = self.resolved_model()
        if self.llm_provider in ("experiential", "explabs"):
            return self.explabs_base_url, self.explabs_api_key, model
        if self.llm_provider == "bynara":
            return self.bynara_base_url, self.bynara_api_key, model
        if self.llm_provider == "xkiro":
            return self.xkiro_base_url, self.xkiro_api_key, model
        if self.llm_provider == "openai" or self.openai_api_key:
            if (
                self.openai_api_key
                and self.llm_provider == "tokenrouter"
                and not self.tokenrouter_api_key
            ):
                # Auto-prefer OpenAI when only it is configured.
                return self.openai_base_url, self.openai_api_key, model
            if self.llm_provider == "openai":
                return self.openai_base_url, self.openai_api_key, model
        return self.tokenrouter_base_url, self.tokenrouter_api_key, model

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_prod(self) -> bool:
        return self.app_env.lower() == "production"

    def validate_prod(self) -> None:
        """Fail fast on unsafe prod defaults. Called at app startup."""
        if not self.is_prod:
            return
        problems: list[str] = []
        if not self.github_webhook_secret or self.github_webhook_secret in (
            "dev-secret-change-me",
            "change-me",
        ):
            problems.append(
                "GITHUB_WEBHOOK_SECRET must be set to a random 32+ char value"
            )
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL must be Postgres in production")
        if not self.api_token:
            problems.append("API_TOKEN must be set in production")
        if problems:
            raise RuntimeError("unsafe production config: " + "; ".join(problems))


settings = Settings()
