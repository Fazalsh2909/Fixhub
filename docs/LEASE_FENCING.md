# Lease / Fencing Hardening

Heartbeat + fencing is the base (Phase 4.5). This pass narrows the
slow-not-dead double-execution window. It does NOT eliminate all
distributed-systems double-spend — see residual risks.

## Fence points (all fail-closed: `FAILED` + `LEASE_LOST`, no side effects)

1. **Before every LLM request** — `agent/loop.py` verifies `lease_guard`
   at the top of each iteration and after every tool execution. Loss →
   `LoopResult(lease_lost=True)` → service fails the task. No further
   provider calls are made after loss.
2. **After any long-running tool/command** — `tasks/service.py:_live`
   forces `renew + revalidate` when `run_command` exceeds
   `TOOL_CLEANUP_GRACE_S` (or times out), emitting `LEASE_RENEWED/LEASE_LOST`.
   The loop's post-tool check aborts the batch on loss.
3. **Before every GitHub external side effect** — three fences in the publish
   path: entry (`_publish_task`), before push (`publisher.publish`), and
   between push and PR creation (`publisher.publish`). Loss raises
   `PublishError("lease lost ...")` BEFORE the API call. The local
   (no-token) push path is re-fenced too.
4. **Heartbeat** — every tool call renews when `LEASE_RENEW_EVERY_S` elapsed
   (`_renew_lease` is an atomic `UPDATE ... WHERE status=RUNNING AND
   lease_expires_at >= now`; a reaped/completed task renews 0 rows → False).

Lease duration is NOT arbitrarily huge: `TASK_LEASE_S=3600` with validator
upper bound `lease <= 2*job + 3600` (see `docs/TIMEOUTS.md`).

## Lease parameters

`TASK_LEASE_S=3600` safely exceeds one provider request (`LLM_TIMEOUT_S=120`)
+ one tool (`COMMAND_TIMEOUT_S=180`) + heartbeat (`120`) + grace (`30`).

## Remaining theoretical residual risk (accepted)

- **One duplicated LLM request** during a pathological worker/network
  partition (lease expires mid-request, recovery re-runs): bounded to wasted
  tokens, never a duplicated side effect — push/PR fences stop the loser.
- **Push-then-crash before DB commit**: reconciles via branch/PR reuse
  (`ensure_branch` checkout + `_find_open_pr` + 422-relist + `PullRequest`
  upsert). No duplicate PR by construction (one branch per task).
- **Clock skew**: comparisons use DB `utcnow()` on both sides; skew between
  API/worker hosts is bounded by NTP in practice but not proven here.
- **Slow-not-dead repeat spend**: a worker that is alive but slower than the
  lease still gets reaped and its replacement repeats LLM spend. Narrowed by
  heartbeat + post-tool renewal, not eliminated.
