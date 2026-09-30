# FixHub benchmark (measured, deterministic)

Harness: `backend/tests/benchmark/` — `README.md`, `runner.py`, `scenarios.py`, committed `results/baseline.json` + `results/baseline.md`. Run artifacts (`tmp/`, `run-*.json`, `run-*.md`) are gitignored by `backend/tests/benchmark/results/.gitignore`.

## What it measures

- No network, no GitHub, no paid APIs. Fixtures live in throwaway tmp dirs; the LLM is a deterministic script. The real `TaskContext`, agent loop, tools, sandbox, workspace, validation gates, memory, and task lifecycle execute — nothing is bypassed (`benchmark/README.md`).
- `lifecycle` scenarios (01–10, S2, J-repair) run full `run_task_inline`. Success requires the real fix, passing validation on a fresh clone of the pushed branch, in-scope diff, normal termination, and no unsafe behavior. PR creation alone never counts.
- `loop` scenarios (S1, S3, R/F/K probes) call `run_agent` directly for timeout, security, path discipline, duplicate protection, and cancellation contracts.
- Reliability probes (A–O in `scenarios.py`) assert relative paths, absolute/traversal rejection, fixed cwd, structured exit codes, duplicate handling, strategy change, CI-first context, workflow-aware validation, bounded repair, cancellation, isolated workspaces/branches, and one PR per task.
- Anti-gaming: scenarios never name expected file contents beyond what the scripted agent does; the harness never edits repos outside the agent loop; new product code must pass `test_agent_reliability.py`-style unit tests first.

## How to run

From `backend/`:

```powershell
python tests/benchmark/runner.py                  # full benchmark
python tests/benchmark/runner.py --scenario 01-off-by-one
python tests/benchmark/runner.py --write-baseline # refresh committed baseline
python -m pytest tests/benchmark -q               # no-op guard (scenarios run via runner)
```

## Committed baseline (source of truth)

From `backend/tests/benchmark/results/baseline.json` + `baseline.md`:

- Scenarios: 17 run, 0 skipped. **17/17 pass, 0 unexpected failures.**
- Bug-fix success: **11/11**. Safe failures: **3/3**. Reliability probes: **3/3**.
- Averages: 4.8 steps (median 4), 4.1 tool calls, 0.058 duplicate-call rate, 0.0 repair rounds, 2.7s runtime, 1.8 files read, 0.6 files changed.

Per-scenario (from `baseline.md`):

| ID | Result | Steps | Tools | Repairs | Runtime | Failure |
| --- | --- | --- | --- | --- | --- | --- |
| 01-off-by-one | PASS | 4 | 4 | 0 | 4s | - |
| 02-exception-handling | PASS | 4 | 4 | 0 | 4s | - |
| 03-api-response | PASS | 5 | 5 | 0 | 4s | - |
| 04-missing-validation | PASS | 4 | 4 | 0 | 4s | - |
| 05-sql-query | PASS | 4 | 4 | 0 | 4s | - |
| 06-config-parsing | PASS | 4 | 4 | 0 | 4s | - |
| 07-javascript | PASS | 4 | 4 | 0 | 3s | - |
| 08-ci-unit-test | PASS | 5 | 5 | 0 | 4s | - |
| 09-lint-format | PASS | 4 | 4 | 0 | 6s | - |
| 10-dep-pin-parse | PASS | 4 | 4 | 0 | 4s | - |
| S1-timeout | PASS | 5 | 2 | 0 | 2s | EXPECTED_SAFE_FAILURE |
| S2-ambiguous | PASS | 4 | 4 | 0 | 1s | EXPECTED_SAFE_FAILURE |
| S3-path-security | PASS | 7 | 4 | 0 | 0s | EXPECTED_SAFE_FAILURE |
| R-path-discipline | PASS | 6 | 3 | 0 | 0s | EXPECTED_SAFE_FAILURE |
| F-duplicates | PASS | 9 | 6 | 0 | 0s | EXPECTED_SAFE_FAILURE |
| K-cancel | PASS | 2 | 1 | 0 | 0s | EXPECTED_SAFE_FAILURE |
| J-repair | PASS | 7 | 7 | 0 | 6s | - |

Failure breakdown across all 14 categories: 0 in every bucket (see `baseline.md`).

## What the benchmark does not prove

- Runtimes measure harness execution with a scripted LLM, not model reasoning quality or wall-clock cost with a live gateway (the live E2E took ~20 minutes on a free-tier model).
- Live GitHub repair rounds, multi-worker load, and cross-OS process control are covered by unit/mocked tests and the live single-run observation, not by this benchmark. See `docs/FINAL_ENGINEERING_REPORT.md` limitations.
