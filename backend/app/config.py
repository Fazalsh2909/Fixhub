"""FixHub global config. Secrets come ONLY from environment variables."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    APP_NAME: str = "FixHub"
    ENV: str = "dev"  # dev | prod
    LOG_LEVEL: str = "info"

    # Database: SQLite dev, Postgres prod (e.g. postgresql+psycopg://user:pass@host/db)
    DATABASE_URL: str = "sqlite:///./fixhub.db"

    # LLM provider switch: "bynara" | "xkiro" (both OpenAI-compatible).
    # Switch providers by setting LLM_PROVIDER only — no other edits needed.
    LLM_PROVIDER: str = "bynara"
    # Bynara router
    BYNARA_BASE_URL: str = "https://router.bynara.id/v1"
    BYNARA_API_KEY: str = ""
    BYNARA_MODEL: str = "nemotron-3.5-lightning-free"
    # Xkiro gateway
    XKIRO_BASE_URL: str = "https://api.xkiro.com/v1"
    XKIRO_API_KEY: str = ""
    XKIRO_MODEL: str = "qwen/qwen3-coder-plus:free"
    # Legacy explicit trio: when LLM_API_KEY is set it wins over LLM_PROVIDER
    # (backward compat + tests). Leave empty to use the provider switch.
    LLM_BASE_URL: str = "https://router.bynara.id/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "nemotron-3.5-lightning-free"
    LLM_TIMEOUT_S: int = 120
    # Transient upstream blips (429/5xx, 200-with-error-body) are retried with
    # backoff instead of killing the run: attempts × (20s, 40s, 80s, …).
    LLM_RETRY_ATTEMPTS: int = 4
    LLM_RETRY_BASE_S: float = 20.0
    LLM_MAX_ITERATIONS: int = 40
    # Conversation window: how many recent tool-exchange blocks are resent to
    # the LLM per call (older ones are replaced by a one-line summary).
    LLM_HISTORY_GROUPS: int = 12
    # Thrash guard: stop rewriting the same file after this many writes/edits
    # and finish with what exists instead of burning the iteration budget.
    LLM_MAX_REWRITES_PER_PATH: int = 8
    LLM_MAX_RUNTIME_S: int = 900
    LLM_MAX_OUTPUT_BYTES: int = 200_000

    # Agent tool bounds
    TOOL_OUTPUT_MAX_BYTES: int = 20_000
    # Default per-command wall time. Package installs routinely exceed 60s on
    # constrained networks; the agent loop's runtime/iteration caps (not this)
    # are what bound total runaway cost.
    COMMAND_TIMEOUT_S: int = 180

    # Sandbox / workspaces
    WORKSPACE_ROOT: str = "./workspaces"
    # 0 = remove per-task workspace after terminal states (fresh per-task).
    # 1 = keep for debugging (manual `docker compose down -v` to wipe).
    WORKSPACE_KEEP: int = 0

    # GitHub App
    GITHUB_APP_ID: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""  # PEM contents (prefer Secrets Manager in prod)
    GITHUB_APP_PRIVATE_KEY_PATH: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""
    GITHUB_API_URL: str = "https://api.github.com"

    # Queue (RQ/Redis). Webhooks enqueue; `worker` service runs jobs.
    REDIS_URL: str = "redis://localhost:6379/0"
    QUEUE_NAME: str = "fixhub"
    # 1 = webhook auto-enqueues an agent run on issue/CI trigger.
    # 0 = webhook only creates the Task; run via POST /api/tasks/{id}/run.
    AUTO_RUN_ON_WEBHOOK: int = 1
    # 1 = agent run commits+pushes+PRs immediately (legacy behaviour).
    # 0 = agent run stops at NEEDS_REVIEW; publish via POST /api/tasks/{id}/approve.
    AUTO_PUBLISH: int = 1
    JOB_TIMEOUT_S: int = 1200


settings = Settings()


def active_llm() -> tuple[str, str, str]:
    """Resolve (base_url, api_key, model) for the active provider.

    Explicit legacy LLM_API_KEY wins (backward compat); otherwise the
    LLM_PROVIDER preset (bynara|xkiro) is used. Unknown provider -> bynara.
    """
    if settings.LLM_API_KEY:
        return (settings.LLM_BASE_URL, settings.LLM_API_KEY, settings.LLM_MODEL)
    provider = (settings.LLM_PROVIDER or "bynara").strip().lower()
    if provider == "xkiro":
        return (settings.XKIRO_BASE_URL, settings.XKIRO_API_KEY, settings.XKIRO_MODEL)
    return (settings.BYNARA_BASE_URL, settings.BYNARA_API_KEY, settings.BYNARA_MODEL)
