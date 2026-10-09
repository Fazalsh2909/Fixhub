# Timeout / Lease Ladder

Single coherent model. Validated at startup by `app.config.validate_timeout_ladder`
(prod: fatal; dev: fatal unless `DEV_SLOW_MODEL=1`).

## Production defaults (seconds)

| Knob | Default | Meaning |
| --- | --- | --- |
| `LLM_TIMEOUT_S` | 120 | max one provider request |
| `COMMAND_TIMEOUT_S` | 180 | max one tool/process call |
| `LLM_MAX_RUNTIME_S` | 900 | max one agent-loop invocation |
| `AGENT_BUDGET_S` | 2100 | cumulative budget: initial run + gate-fix rounds |
| `JOB_TIMEOUT_S` | 2400 | RQ kill: budget + clone/gates/publish slack |
| `TASK_LEASE_S` | 3600 | claim lease: outlives the job; recovery threshold |
| `LEASE_RENEW_EVERY_S` | 120 | heartbeat: renew while actively executing |
| `TOOL_CLEANUP_GRACE_S` | 30 | grace to renew/revalidate after a long tool |
| `FC_VM_MAX_RUNTIME_S` | 1500 | microVM reaper cap: overstayed VMs are destroyed (< `JOB_TIMEOUT_S`) |

## Ordering rules (all enforced, fail-fast `ValueError`)

1. `LLM_TIMEOUT_S < COMMAND_TIMEOUT_S <= LLM_MAX_RUNTIME_S <= AGENT_BUDGET_S < JOB_TIMEOUT_S < TASK_LEASE_S`
2. `LEASE_RENEW_EVERY_S << TASK_LEASE_S`
3. `0 <= TOOL_CLEANUP_GRACE_S < LEASE_RENEW_EVERY_S`
4. `TASK_LEASE_S > LLM_TIMEOUT + COMMAND_TIMEOUT + LEASE_RENEW + GRACE`
   (lease survives one slow provider call + one slow tool + a beat + cleanup)
5. `TASK_LEASE_S <= 2 * JOB_TIMEOUT_S + 3600` (not arbitrarily huge)

## Development

Legacy ad-hoc `1500/1800` values are removed. Slow free-tier dev sets
`DEV_SLOW_MODEL=1` (non-prod only; production ignores it) instead of
hand-editing numbers. The ladder relationship is still validated; the flag
only documents the relaxation as intentional.
