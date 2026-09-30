# FixHub Final Engineering Report

Date: 2026-09-30. Scope: reliability upgrade + final validation. No agent
frameworks added; no rewrites; single standing branch replaced by per-task
branches per explicit instruction.

## 1. Concurrent Task Isolation

What changed (`backend/app/github/publisher.py`, `backend/app/tasks/service.py`,
`backend/tests/test_concurrency.py`):
- Branch naming is now `fixhub-fixes/issue-<n>-task-<id>` /
  `fixhub-fixes/ci-<shortsha>-task-<id>`, deterministic per task. Initial runs,
  gate fix rounds, CI repair rounds, and manual approve all resolve the
  identical name, so repairs reuse the same branch and PR.
- `ensure_branch` (checkout / fast-forward / track-checkout / create-from-ref)
  is unchanged and already branch-parameterized.
- New collision guard in `_publish_task`: a different active
  (`RUNNING`/`AWAITING_CI`/`NEEDS_REVIEW`) task owning the same branch fails
  the run loudly (`branch_collision`) instead of last-writer-wins.
- Repair clones check out the task's own branch (`task.branch`), never a
  shared one.

Tests proving isolation (`test_concurrency.py`, 4 tests, passing):
- Two same-repo tasks: disjoint workspaces, branches, commits; cross-absence
  verified via `git show branch:path`; workspaces cleaned after terminal states.
- Issue + CI task: distinct deterministic names.
- Repair round: same branch, `create_pull_request` called exactly once
  (second round takes the update path), same PR URL.
- Squatter task owning another task's branch: run fails with
  `branch_collision`, no push occurs.

## 2. Real GitHub E2E

Trigger: controlled breakage on branch `live-e2e-test`
(`live e2e test` verbatim is rejected by GitHub ref rules) of
`Fazalsh2909/nexus-mcp-intelligence`: off-by-one in new
`apps/api/app/core/pagination.py` + probe test `test_fixhub_probe.py`,
opened as PR #10. Real CI failed (`assert 3 == 4`, all else green).

Observed live lifecycle (task #20):
webhook `check_run`/`workflow_run` deliveries → `TASK_CREATED` →
`CI_CONTEXT_LOADED` (branch, workflow content, run id) → queued → agent
started → `CI_CHECKOUT` at failing sha → read workflow/test/source →
one-line fix (ceiling division) → pytest + pinned ruff 0.8.0 + mypy locally
→ `VALIDATION_PASSED` → commit → push
`fixhub-fixes/ci-ce814b9-task-20` → PR #11 (first and only PR for the task)
→ `AWAITING_CI` → watcher observed real green checks
(docker skipped, frontend/backend success) → `CI_PASSED` → `COMPLETED`.

Repair coverage: no repair round was needed (first PR green). The repair
path (same task/branch/PR, max 3, `create_pull_request` counted once) is
covered by unit tests, the mocked J-repair benchmark scenario, and the
concurrency repair test — but a *live* repair round on real GitHub was not
observed in this session. A staged red-on-purpose repair was deliberately
not performed (it would mean sabotaging the agent's own commit).

Incidents during the run (all handled, none masked):
- Task 17 chased a stale memory file; the stale-guard correctly superseded
  it against green PR #7 — no bad PR created.
- Task 18 died on an LLM transport blip; recorded FAILED with the real error.
- Task 19 passed local gates then hit a TLS disconnect on `git push`;
  recorded `FAILED/publish_failed` with the real error (no fake success).
- A host reboot killed Docker mid-session; the stack self-recovered
  (`restart: unless-stopped`); the missed webhook window was re-driven
  explicitly and documented as such.

## 3. Real Agent Trace

Reference: `docs/examples/real-ci-repair-trace.md` — exported from the
FixHub `task_events` table for task #20 (31 events), scrub-gated for
secrets before writing. Operational data only; chain-of-thought is never
stored by FixHub.

## 4. Benchmark

Harness: `backend/tests/benchmark/` (README, runner, 17 scenarios, results).
Scripted deterministic LLM; real TaskContext, loop, tools, sandbox,
workspace, gates, memory, lifecycle. Success requires the real fix, passing
validation on a fresh clone, in-scope diff, normal termination, no unsafe
behavior. PR creation alone never counts.

Baseline (`results/baseline.json`, committed): **17/17, 0 unexpected failures.**
- Bug-fix success: 11/11 (10 bug types + J-repair incl. single-PR assertion)
- Safe failures: 3/3 (timeout bounded + recorded, ambiguous = no diff,
  path/security violations blocked with zero writes)
- Reliability probes: 3/3 (path discipline, duplicate protection, cancel)
- Averages: 4.8 steps (median 4), 4.1 tool calls, 0.058 duplicate rate,
  0.0 repair rounds, 2.7s runtime, 1.8 files read, 0.6 files changed
- Failure breakdown: empty across all 14 categories

Failures found *during* baseline construction (reproduced first, fixed
minimally, re-baselined — not hidden):
1. `__pycache__` bytecode committed into PRs (`git add -A` + scope check
   divergence) → excluded from `changed_files()` and unstaged before commit.
2. Gate shell used `| tail`/`;` → dead on Windows `cmd.exe` → gates split
   into single-purpose commands.
3. Gate used bare `pip`/`ruff` binaries (PATH mismatch after install) →
   `python -m` throughout for interpreter consistency.
4. Windows pipe-inheritance deadlock made timeouts unenforceable (30s sleep
   unkilled) → process-group tree kill; S1 runtime 30s → 2s proves it.

## 5. Regression Tests

- Full backend suite: **117 passed** (includes 4 concurrency, 26 reliability,
  gate suites, script-aware e2e).
- Frontend: `tsc` + `vite build` clean (includes task Cancel button,
  `AWAITING_CI`/`CANCELLED` pills).
- Benchmark: 17/17 above; deterministic (two consecutive full runs
  identical per-scenario).
- `python -m compileall app tests`: clean.
- Live stack rebuilt (`backend`, `worker`, `frontend`); health, queue,
  worker-listening, and UI 200 verified post-build.

## 6. Remaining Limitations (real, reproducible)

1. Live repair-round on real GitHub not yet observed (unit + mocked coverage
   only); the machinery it exercises (watcher transition, same-branch clone,
   single-PR update) is tested, but the full live loop is not.
2. Single RQ worker serializes execution; `--scale worker=N` is untested
   under load (branch isolation makes it safe by construction, not by proof).
3. Local gates green does not imply CI green (environment drift); persistent
   local red goes to `NEEDS_REVIEW`, which needs a human.
4. Wall-clock cost is dominated by free-tier LLM latency (task 20: ~20 min
   for 12 tool calls); benchmark runtimes measure execution, not model
   reasoning quality (scripted LLM by design).
5. `S1-timeout` style enforcement depends on OS process control; verified on
   Windows (this machine) and Linux containers by design, not matrix-tested.
6. Old shared `fixhub-fixes` branches and superseded PRs on live repos were
   left untouched per instruction; the new collision guard only governs new
   per-task branches.
7. ngrok tunnel + host uptime are operational dependencies for webhooks;
   missed delivery windows require explicit redrive (no dead-letter replay).

## 7. Final Recommendation

Demonstrated: per-task workspace/branch/PR isolation with tests; a complete
real trigger-to-green-COMPLETED lifecycle on live GitHub with independently
verified checks; 17/17 deterministic benchmark with zero unexpected
failures; 117-test suite green; safe-stop behavior on timeouts, ambiguity,
path violations, crashes, and collisions — all with real errors recorded,
no masked states found.

Tested: everything in sections 4–5 above, twice for the benchmark.

Not tested live: a full repair round on real GitHub (item 1); multi-worker
concurrency under load (item 2).

Strongest remaining failure mode: free-tier model flakiness (transport
drops, latency) meeting fixed time budgets — mitigated by retries, crash
preservation, and NEEDS_REVIEW, but retries cost wall time, not success.

No further architecture is justified by current evidence. Freeze.
