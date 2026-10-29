"""FixHub global config. Secrets come ONLY from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App
    APP_NAME: str = "FixHub"
    ENV: str = "dev"  # dev | prod
    LOG_LEVEL: str = "info"

    # Database: SQLite dev, Postgres prod (e.g. postgresql+psycopg://user:pass@host/db)
    DATABASE_URL: str = "sqlite:///./fixhub.db"
    # Phase 4: production PostgreSQL pool (total conns = (size+overflow) x instances+workers).
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800
    # Phase 4: concurrency caps (0 = unlimited). Enforced at enqueue + claim.
    MAX_RUNNING_TASKS_GLOBAL: int = 100
    MAX_RUNNING_PER_USER: int = 100
    MAX_RUNNING_PER_REPO: int = 100
    # Phase 4: claim lease seconds (must exceed JOB_TIMEOUT_S so slow workers
    # are never reaped mid-run; recovery reaps only expired leases).
    TASK_LEASE_S: int = 10800

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
    # Optional stronger model for CI repair tasks (empty = inherit LLM_MODEL /
    # the task owner's configured model). CI is a short, high-value run where
    # the free tier's repeated reads are the main failure mode.
    LLM_CI_MODEL: str = ""
    LLM_TIMEOUT_S: int = 120
    # Transient upstream blips (429/5xx, 200-with-error-body) are retried with
    # backoff instead of killing the run: attempts × (20s, 40s, 80s, …).
    LLM_RETRY_ATTEMPTS: int = 4
    LLM_RETRY_BASE_S: float = 20.0
    LLM_MAX_ITERATIONS: int = 100
    # Conversation compaction. Provider input usage is cumulative across calls,
    # so large tool outputs and write payloads must not be replayed indefinitely.
    LLM_HISTORY_GROUPS: int = 4
    LLM_CONTEXT_MAX_CHARS: int = 32768
    LLM_TOOL_RESULT_MAX_CHARS: int = 3000
    # Larger cap for one targeted read (read_file/git_diff). The generic
    # 3000-char clip cut off the exact lines named by a failure signal, so
    # the model edited blind from stale context (production CI run, Oct 2026).
    LLM_READ_RESULT_MAX_CHARS: int = 10000
    # Completion cap per chat call. Was hard-coded 2048, which truncated long
    # edit_file payloads (old/new fields over ~1.8k chars arrived as cut-off
    # JSON and parsed into a "_raw" passthrough instead of a real edit).
    LLM_MAX_TOKENS: int = 4096
    # CI fixes should converge quickly; this is the iteration backstop.
    LLM_CI_MAX_ITERATIONS: int = 100
    # Task-wide request cap for one CI task. 30 (was 1000): a stateless
    # provider re-charges the whole context on every call, and the old
    # 16-request default was exhausted by investigation alone.
    LLM_CI_MAX_REQUESTS_PER_TASK: int = 30
    # Convergence pacing for CI tasks: after this many consecutive LLM
    # requests without a successful workspace mutation, inject one user note
    # (AFTER = act now, FINAL = last push before the useful budget is spent).
    # At most two notes per run; a successful edit resets the counter.
    LLM_CI_DIRECTIVE_AFTER: int = 6
    LLM_CI_DIRECTIVE_FINAL: int = 12
    LLM_MAX_RUNTIME_S: int = 3600
    LLM_MAX_OUTPUT_BYTES: int = 200_000

    # Agent loop governor (general-purpose safeguards, not a phase script).
    # Identical tool calls beyond these caps are answered from cache/suppressed
    # instead of re-executed; the model keeps full freedom over WHAT to do.
    LLM_REPEAT_SUPPRESS_READS: int = 3
    LLM_REPEAT_SUPPRESS_COMMANDS: int = 3
    LLM_REPEAT_SUPPRESS_FAILED_EDITS: int = 5
    # Consecutive steps without new evidence before the loop nudges once, then
    # stops with a "stalled" outcome (never a false COMPLETED).
    LLM_STALL_WINDOW: int = 8
    # Distinct reads of one path (read_file/list_directory, any offset/limit)
    # before further reads of that path are blocked with an act-on-what-you-
    # have error. Bounds chunked-paging loops (production task #68).
    LLM_MAX_READS_PER_PATH: int = 4
    # Retain processed webhook deliveries this long (audit + replay window).
    WEBHOOK_DELIVERY_RETENTION_HOURS: int = 168
    # A delivery stuck in "received" longer than this is re-processed.
    WEBHOOK_DELIVERY_STALE_S: int = 300

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
    # Permission discipline for the workspace lifecycle (decimal ints —
    # never octal literals here, they die silently under env overrides):
    # every workspace dir/file must be readable+traversable by the publisher
    # service identity; worker and publisher share one identity/group
    # (tasks #70/#71: modes after a `git add` Permission denied).
    # WORKSPACE_DIR_MODE=0o770, WORKSPACE_FILE_MODE=0o644.
    WORKSPACE_DIR_MODE: int = 0o770
    WORKSPACE_FILE_MODE: int = 0o644
    # Manifest workspace reconciliation caps (guest -> host sync). Sync
    # FAILS CLOSED when any cap is hit — it never silently truncates.
    FC_SYNC_MANIFEST_MAX_FILES: int = 8000
    FC_SYNC_MAX_FILES: int = 2000
    FC_SYNC_MAX_BYTES: int = 10485760
    FC_SYNC_FILE_MAX_BYTES: int = 2000000
    # Phase 5: execution substrate. "host" = legacy subprocess (dev/test only).
    # "firecracker" = real microVMs via jailer (required in prod). Unknown
    # values fail closed (never silently use host).
    SANDBOX_BACKEND: str = "host"
    # Phase 5 Firecracker/jailer settings (host paths; see docs/FIRECRACKER_HOST.md).
    FC_BINARY: str = "/usr/local/bin/firecracker"
    JAILER_BINARY: str = "/usr/local/bin/jailer"
    FC_KERNEL_IMAGE: str = "/srv/firecracker/kernel/vmlinux"
    FC_ROOTFS_IMAGE: str = "/srv/firecracker/rootfs/base.ext4"
    FC_CHROOT_BASE: str = "/srv/firecracker/jails"
    FC_GUEST_VCPU: int = 2
    FC_GUEST_MEM_MIB: int = 1024
    # Writable-disk cap (MiB): the immutable base rootfs must fit inside it
    # (fail-closed otherwise); the per-task overlay is an exact copy, so the
    # VM's disk can never exceed this cap.
    FC_OVERLAY_MB: int = 5120
    # Guest process cap (cgroup pids.max, enforced by the jailer).
    FC_PIDS_MAX: int = 256
    # Worker-side runtime cap per microVM (seconds, enforced by the orphan/
    # overstay reaper). Must stay below JOB_TIMEOUT_S so the RQ kill (not a
    # leaked VM) is what bounds runaway work.
    FC_VM_MAX_RUNTIME_S: int = 7500
    # Owner heartbeat age (seconds) past which a dead owner is considered
    # stale. A stale heartbeat alone never reaps a live-leased task.
    FC_OWNER_STALE_S: int = 900
    FC_BOOT_TIMEOUT_S: int = 30
    FC_VSOCK_TIMEOUT_S: int = 10
    # Phase 5 privilege delegation: the production API runs as User=fixhub
    # with NoNewPrivileges=true and cannot touch netns/nft/jailer directly.
    # FC_NETD_SOCKET set (in the API/worker environment) routes
    # network + jail ops through the root-owned fixhub-netd daemon over this
    # Unix socket (typed {op, task_id} requests only, never shell/argv).
    # Empty = direct execution (laptop dev, pytest, manual root runs).
    FC_NETD_SOCKET: str = "/run/fixhub-netd.sock"
    # Host egress services (root-ns veth address; see net.py/egress.py).
    FC_EGRESS_PROXY_ADDR: str = "10.200.0.1"
    FC_EGRESS_PROXY_PORT: int = 8443
    FC_DNS_STUB_ADDR: str = "10.200.0.1"
    # Phase 5 egress allowlist (comma-separated host suffixes,
    # enforced on the HOST via SNI proxy + nft — never in the guest).
    # github.com is required for git ls-remote/clone; api/codeload/objects
    # cover API + archive + LFS traffic.
    FC_EGRESS_ALLOWLIST: str = (
        "github.com,api.github.com,codeload.github.com,"
        "objects.githubusercontent.com,"
        "pypi.org,files.pythonhosted.org,registry.npmjs.org"
    )
    # Phase 5 real-VM test gate: 1 = sandbox security tests REQUIRE real KVM
    # microVMs and FAIL when prerequisites are missing (Linux CI). 0 = those
    # tests skip cleanly without KVM (Windows laptop dev default).
    FC_REQUIRE_KVM: int = 0

    # GitHub App
    GITHUB_APP_ID: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""  # PEM contents (prefer Secrets Manager in prod)
    GITHUB_APP_PRIVATE_KEY_PATH: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""
    GITHUB_API_URL: str = "https://api.github.com"

    # Queue (RQ/Redis). Webhooks enqueue; `worker` service runs jobs.
    REDIS_URL: str = "redis://localhost:6379/0"
    QUEUE_NAME: str = "fixhub"
    # Phase 2 auth: opaque DB-backed sessions (raw token only in HttpOnly cookie).
    # Secrets come ONLY from environment variables.
    AUTH_COOKIE_NAME: str = "fixhub_session"
    AUTH_SESSION_DAYS: int = 7
    AUTH_COOKIE_SECURE: int = 0  # 1 in production (HTTPS)
    AUTH_COOKIE_SAMESITE: str = "lax"
    AUTH_PASSWORD_MIN_LEN: int = 10
    # Phase 4.5 auth hardening: bounded login protection (DB buckets).
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_WINDOW_S: int = 900
    LOGIN_LOCKOUT_S: int = 900
    # Phase 4.5 admin bootstrap: comma-separated emails promoted idempotently.
    ADMIN_EMAILS: str = ""
    # Phase 4.5 execution coherence ladder (seconds):
    #   per-run LLM_MAX_RUNTIME_S <= cumulative AGENT_BUDGET_S
    #     < RQ JOB_TIMEOUT_S < TASK_LEASE_S (recovery threshold).
    # Gate-fix rounds share the budget via deadline_mono; the tighter of the
    # per-run cap and the remaining budget always wins. Local .env overrides
    # for slow free-tier models must preserve the ordering (see
    # DEV_SLOW_MODEL + validate_timeout_ladder below; docs/TIMEOUTS.md).
    LEASE_RENEW_EVERY_S: int = 120
    AGENT_BUDGET_S: int = 7200
    # Final hardening: tool/process cleanup margin. A worker that finishes a
    # long tool call gets this much grace to renew/revalidate the lease before
    # continuing. Must be << LEASE_RENEW_EVERY_S and << TASK_LEASE_S.
    TOOL_CLEANUP_GRACE_S: int = 30
    # Final hardening: explicit dev-only relaxation for slow free-tier models.
    # 0 = strict coherent ladder everywhere. 1 = non-prod may scale timeouts
    # coherently (never ad-hoc 1500/1800). Production ignores this flag.
    DEV_SLOW_MODEL: int = 0
    # Final hardening: per-user LLM usage limits (config defaults, env-overridable).
    # 0/None semantics: 0 concurrent/daily/request = unlimited is FORBIDDEN in
    # prod (fail-closed); token caps 0/None = off (providers may omit usage).
    MAX_LLM_REQUESTS_PER_TASK: int = 1000
    # Self-hosted development default; production deployments can lower this.
    MAX_TASKS_PER_USER_PER_DAY: int = 10000
    MAX_INPUT_TOKENS_PER_TASK: int = 0
    # CI override (0 = inherit MAX_INPUT_TOKENS_PER_TASK). A CI repair needs
    # room for ~30 stateless requests; the old 100k global cap fired before a
    # single edit in production.
    MAX_INPUT_TOKENS_PER_CI_TASK: int = 300_000
    MAX_OUTPUT_TOKENS_PER_TASK: int = 0
    # Phase 4.5 PG statement guard (0 = off). Applied to app connections only.
    POSTGRES_STATEMENT_TIMEOUT_MS: int = 30000
    # Phase 3 BYOK: server-side master key for credential encryption at rest.
    # Fernet key (generate: python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"). Never commit, never expose.
    FIXHUB_CREDENTIAL_ENCRYPTION_KEY: str = ""
    # Phase 4.5 rotation: previous master key, accepted for decryption only
    # while stored credentials migrate. Never used for new encryptions.
    FIXHUB_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS: str = ""
    # Deprecated: kept only so existing .env files keep loading. Ignored at
    # runtime — every created task is enqueued immediately (`_start_task`).
    AUTO_RUN_ON_WEBHOOK: int = 1
    # ---- GitHub trigger policy -------------------------------------------
    # Which webhook events may create a CI task. `workflow_run` fires once per
    # completed workflow; `check_run` fires once per JOB (duplicates
    # workflow_run), so it is opt-in.
    CI_TRIGGER_EVENTS: str = "workflow_run"
    # With check_run enabled, accept only GitHub Actions checks so third-party
    # checks (Vercel, CodeQL, Dependabot, ...) never start agent runs.
    CI_ACTIONS_CHECKS_ONLY: int = 1
    # Optional comma-separated workflow names or paths (empty = all).
    CI_WORKFLOW_ALLOWLIST: str = ""
    # all | default_only | allowlist (CI_BRANCH_ALLOWLIST, comma-separated).
    CI_BRANCH_POLICY: str = "all"
    CI_BRANCH_ALLOWLIST: str = ""
    # Branch prefixes that never create tasks. FixHub's own repair branches are
    # handled by the CI watcher on the existing task, never as new work.
    CI_IGNORE_BRANCH_PREFIXES: str = "fixhub-fixes/"
    # Conclusions that count as a broken CI/CD pipeline.
    CI_FAILURE_CONCLUSIONS: str = "failure,timed_out,startup_failure"
    # An AWAITING_CI task whose check conclusion stays "unknown"/absent for
    # this many watcher ticks becomes FAILED(ci_unobservable) instead of
    # waiting forever. The watcher runs every 60s, so 30 ≈ 30 minutes.
    CI_UNOBSERVABLE_MAX_TICKS: int = 30
    # Pre-publish guard against "fixes" that disable required CI: off | warn |
    # block. Inspects the workspace diff only (runs no repo commands).
    CI_WEAKENING_GATE: str = "block"
    # 1 = agent run commits+pushes+PRs immediately (legacy behaviour).
    # 0 = agent run stops at NEEDS_REVIEW; publish via POST /api/tasks/{id}/approve.
    AUTO_PUBLISH: int = 1
    # Phase 4.5: must exceed AGENT_BUDGET_S + clone/publish slack so the
    # cumulative budget (not the RQ kill) is what normally bounds agent work.
    JOB_TIMEOUT_S: int = 9000


settings = Settings()


def validate_timeout_ladder(s=None) -> None:
    """Final hardening: fail fast on impossible/unsafe timeout/lease combos.

    Coherent production ladder (seconds):
      LLM_TIMEOUT_S < COMMAND_TIMEOUT_S <= LLM_MAX_RUNTIME_S <= AGENT_BUDGET_S
        < JOB_TIMEOUT_S < TASK_LEASE_S
      LEASE_RENEW_EVERY_S << TASK_LEASE_S (heartbeat much smaller than lease)
      TOOL_CLEANUP_GRACE_S << LEASE_RENEW_EVERY_S (grace fits inside a beat)
      TASK_LEASE_S > LLM_TIMEOUT_S + COMMAND_TIMEOUT_S
        + LEASE_RENEW_EVERY_S + TOOL_CLEANUP_GRACE_S (lease survives one
        slow provider call + one slow tool + a heartbeat + cleanup)
      TASK_LEASE_S <= 2 * JOB_TIMEOUT_S + 3600 (not arbitrarily huge)

    Raises ValueError naming the offending pair. Never returns silently on
    invalid input. Callers: main._enforce_production_guards (prod, fatal) and
    startup validation in dev (fatal unless DEV_SLOW_MODEL=1 in non-prod).
    """
    cfg = s if s is not None else settings
    problems: list[str] = []
    llm_to = int(getattr(cfg, "LLM_TIMEOUT_S", 0))
    cmd_to = int(getattr(cfg, "COMMAND_TIMEOUT_S", 0))
    per_run = int(getattr(cfg, "LLM_MAX_RUNTIME_S", 0))
    budget = int(getattr(cfg, "AGENT_BUDGET_S", 0))
    job = int(getattr(cfg, "JOB_TIMEOUT_S", 0))
    lease = int(getattr(cfg, "TASK_LEASE_S", 0))
    renew = int(getattr(cfg, "LEASE_RENEW_EVERY_S", 0))
    grace = int(getattr(cfg, "TOOL_CLEANUP_GRACE_S", 0))

    def _bad(cond: bool, msg: str) -> None:
        if cond:
            problems.append(msg)

    _bad(not (llm_to > 0), f"LLM_TIMEOUT_S must be > 0 (got {llm_to})")
    _bad(not (cmd_to > 0), f"COMMAND_TIMEOUT_S must be > 0 (got {cmd_to})")
    _bad(
        not (per_run > 0 and budget > 0 and job > 0 and lease > 0 and renew > 0),
        "LLM_MAX_RUNTIME_S/AGENT_BUDGET_S/JOB_TIMEOUT_S/TASK_LEASE_S/"
        f"LEASE_RENEW_EVERY_S must all be > 0 (got {per_run}/{budget}/"
        f"{job}/{lease}/{renew})",
    )
    _bad(
        not (llm_to < cmd_to),
        f"LLM_TIMEOUT_S ({llm_to}) must be < COMMAND_TIMEOUT_S ({cmd_to})",
    )
    _bad(
        not (cmd_to <= per_run),
        f"COMMAND_TIMEOUT_S ({cmd_to}) must be <= LLM_MAX_RUNTIME_S ({per_run})",
    )
    _bad(
        not (per_run <= budget),
        f"LLM_MAX_RUNTIME_S ({per_run}) must be <= AGENT_BUDGET_S ({budget})",
    )
    _bad(
        not (budget < job),
        f"AGENT_BUDGET_S ({budget}) must be < JOB_TIMEOUT_S ({job})",
    )
    _bad(
        not (job < lease),
        f"JOB_TIMEOUT_S ({job}) must be < TASK_LEASE_S ({lease})",
    )
    _bad(
        not (renew < lease // 4 or (lease > 0 and renew < lease)),
        f"LEASE_RENEW_EVERY_S ({renew}) must be << TASK_LEASE_S ({lease})",
    )
    _bad(
        not (grace >= 0 and grace < renew),
        f"TOOL_CLEANUP_GRACE_S ({grace}) must satisfy 0 <= grace < "
        f"LEASE_RENEW_EVERY_S ({renew})",
    )
    _bad(
        not (lease > llm_to + cmd_to + renew + grace),
        f"TASK_LEASE_S ({lease}) must exceed LLM_TIMEOUT_S + COMMAND_TIMEOUT_S "
        f"+ LEASE_RENEW_EVERY_S + TOOL_CLEANUP_GRACE_S "
        f"({llm_to}+{cmd_to}+{renew}+{grace}={llm_to + cmd_to + renew + grace})",
    )
    _bad(
        not (lease <= 2 * job + 3600),
        f"TASK_LEASE_S ({lease}) is arbitrarily huge vs JOB_TIMEOUT_S ({job}); "
        "keep lease <= 2*job + 3600",
    )
    if problems:
        raise ValueError("unsafe timeout/lease configuration: " + "; ".join(problems))


def validate_usage_limits(s=None) -> None:
    """Final hardening: per-user limit config must be bounded and sane."""
    cfg = s if s is not None else settings
    is_prod = str(getattr(cfg, "ENV", "dev")).strip().lower() == "prod"
    req = int(getattr(cfg, "MAX_LLM_REQUESTS_PER_TASK", 0))
    daily = int(getattr(cfg, "MAX_TASKS_PER_USER_PER_DAY", 0))
    per_user = int(getattr(cfg, "MAX_RUNNING_PER_USER", 0))
    glob = int(getattr(cfg, "MAX_RUNNING_TASKS_GLOBAL", 0))
    tok_in = int(getattr(cfg, "MAX_INPUT_TOKENS_PER_TASK", 0) or 0)
    tok_in_ci = int(getattr(cfg, "MAX_INPUT_TOKENS_PER_CI_TASK", 0) or 0)
    tok_out = int(getattr(cfg, "MAX_OUTPUT_TOKENS_PER_TASK", 0) or 0)
    ci_req = int(getattr(cfg, "LLM_CI_MAX_REQUESTS_PER_TASK", 0) or 0)
    problems: list[str] = []
    iters = int(getattr(cfg, "LLM_MAX_ITERATIONS", 0))
    if req <= 0:
        problems.append(f"MAX_LLM_REQUESTS_PER_TASK must be > 0 (got {req})")
    elif iters > 0 and req < iters:
        problems.append(
            f"MAX_LLM_REQUESTS_PER_TASK ({req}) must be >= LLM_MAX_ITERATIONS "
            f"({iters}); the request cap would otherwise fire before the "
            "iteration cap on every long run"
        )
    if daily <= 0:
        problems.append(f"MAX_TASKS_PER_USER_PER_DAY must be > 0 (got {daily})")
    if tok_in < 0:
        problems.append(f"MAX_INPUT_TOKENS_PER_TASK must be >= 0 (got {tok_in})")
    if tok_in_ci < 0:
        problems.append(
            f"MAX_INPUT_TOKENS_PER_CI_TASK must be >= 0 (got {tok_in_ci})"
        )
    if ci_req < 0:
        problems.append(
            f"LLM_CI_MAX_REQUESTS_PER_TASK must be >= 0 (got {ci_req})"
        )
    if tok_out < 0:
        problems.append(f"MAX_OUTPUT_TOKENS_PER_TASK must be >= 0 (got {tok_out})")
    if is_prod and (per_user <= 0 or glob <= 0):
        problems.append(
            "production forbids unbounded worker/task configuration "
            f"(MAX_RUNNING_PER_USER={per_user}, MAX_RUNNING_TASKS_GLOBAL={glob})"
        )
    if problems:
        raise ValueError("unsafe usage-limit configuration: " + "; ".join(problems))


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
