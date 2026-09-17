# Fixhub Architecture (Phase 0)

## Flow
```
GitHub (App: issues read, contents read, PRs write-scoped)
  → Event Gateway (FastAPI /webhooks/github: raw-body HMAC, persist + idempotency, ack fast)
  → Queue (Redis dev / SQS prod) → Orchestrator (state machine, resumable)
  → Agent Worker (LLM tool loop) → Sandbox Worker (Docker per task)
  → Verification (repro, regression FAIL→PASS, suites, lint/type/build/scan, adversarial, review)
  → Proof of Fix → Policy Engine → PR Publisher → GitHub
         ↕                  ↕
  Repo Intel + Engineering Memory → Context Retrieval Engine → agent (selective, token-efficient)
```

## Security model
- Read plane: installation token (least-privilege perms) used ONLY by `github/read_client.py`. Never enters sandbox env.
- Write plane: `github/publisher.py` exposes 3 functions taking `VerifiedArtifact` dataclass — no free-form prompt, no repo-delete, no default-branch push (branch guard + test).
- Sandbox: per-task container (`python:3.11-slim` + repo mount copy), cpu/mem/pids/timeout, no `--privileged`, no host docker socket, egress allowlist (pypi, npm, tokenrouter), env scrubbed, always cleaned up. Limits documented in `backend/app/sandbox/README.md`. NOT a microVM — prod path is microVM/GPU workers.
- Secrets: env → Secrets Manager; Fernet-encrypted `llm_providers` row; redaction filter on logs; LLM output treated as untrusted (validated before shell/git).

## Events (extensible)
`issues.opened|labeled(fixhub-fix)` (MVP) + stubbed: `check_run.failed`, `dependabot`, `security_advisory`, `issue_comment(/fix)`, `pull_request_review`, `issues.reopened|assigned`. New type = new gateway parser + queue job, no orchestrator rewrite.

## Task state machine
`CREATED→ANALYZING→REPRODUCING→ROOT_CAUSE_FOUND→PLANNING→IMPLEMENTING→TESTING→DEBUGGING→VERIFYING→REVIEWING→READY_FOR_APPROVAL→COMMITTED→PUSHED→PR_CREATED` (+`FAILED/CANCELLED`). Persisted per transition + `task_events` append-only. Crash in TESTING resumes from snapshot.

## Memory (7 types, one table + type enum)
`memories(fact, type, repo_id, source_path, commit_sha, confidence, status, last_verified)` + `memory_sources`. Statuses ACTIVE/STALE/INVALIDATED/VERIFIED. Staleness worker diffs `repo_snapshots.prev_sha→cur_sha` changed files → mark overlapping memories STALE. Retrieval: issue text → subsystem classifier (keyword + symbol overlap) → top-k memories + symbols + tests + recent commits. Never inject all.

## Repo intelligence (hybrid, incremental)
`files(path, lang, size)` + `symbols(name, kind, file, line)` via stdlib `ast` (py) + regex (ts/js) — no full dump. Lexical grep + symbol lookup + dep edges (`imports`) + git log/blame. Incremental: diff commits, re-index changed files only.

## LLM abstraction
`LLMProvider.generate/stream/tool_call/structured_output`. `OpenRouterProvider` posts OpenAI-compatible chat completions to `TOKENROUTER_BASE_URL` (default `https://tokenrouter.com`) with `model=z-ai/glm-5.3-free`. Tool loop caps iterations (default 12) + token budget; every model claim must be followed by a tool execution before VERIFIED.

## Observability
`request_id` (or `X-GitHub-Delivery`) on every log; JSON logs `{event, task_id, stage, tool, duration_ms}`; RED metrics stub `/metrics`; never log keys/tokens/CoT. See `backend/app/logging.py`.

## AWS (prod target, dev-cheap)
CloudFront→ALB→FastAPI→SQS→EC2/ECS workers + Docker; RDS Postgres; S3 artifacts; Secrets Manager; CloudWatch. `infra/` has compose now, terraform skeleton later.
