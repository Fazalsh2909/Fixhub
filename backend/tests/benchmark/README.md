# FixHub Agent Benchmark

Deterministic measurement of whether the FixHub coding agent solves real
repository tasks reliably. No network, no GitHub, no paid APIs: fixtures live
in throwaway tmp dirs and the LLM is a deterministic script. The real
`TaskContext`, agent loop, tools, sandbox, workspace, validation gates,
memory, and task lifecycle execute throughout — nothing is bypassed.

## Run

From `backend/`:

```powershell
python tests/benchmark/runner.py                  # full benchmark
python tests/benchmark/runner.py --scenario 01-off-by-one
python tests/benchmark/runner.py --write-baseline # refresh committed baseline
python -m pytest tests/benchmark -q               # no-op guard (scenarios run via runner)
```

## Layout

- `scenarios.py` — scenario definitions: fixture files, scripted LLM tool
  sequences (realistic observe→inspect→hypothesis→edit→test→validate),
  triggers, and objective success contracts. No chain-of-thought anywhere.
- `runner.py` — harness: isolated tmp DB + workspaces per run, scripted LLM
  injection, GitHub API mocks only where the token path demands it (J-repair),
  metrics, failure classification, human + JSON reports.
- `fixtures/` — reserved for shared fixture data (scenarios are inline).
- `results/` — `latest.json`, `run-<ts>.json`, committed `baseline.json` /
  `baseline.md`. `tmp/` and `run-*` are gitignored.

## Scenario kinds

- `lifecycle` (01–10, S2, J-repair): full `run_task_inline`; success needs
  the real fix, passing validation on a fresh clone of the pushed branch,
  in-scope diff, normal termination, no unsafe behavior. PR creation alone
  never counts.
- `loop` (S1, S3, R/F/K probes): `run_agent` directly for timeout, security,
  path discipline, duplicate protection, and cancellation contracts.

## Reliability probes (A–O)

Loop-level scenarios assert the post-trace fixes hold: relative paths,
absolute/traversal rejection, fixed cwd, structured exit codes, duplicate
handling, strategy change, CI-first context, workflow-aware validation,
bounded repair, cancellation, isolated workspaces/branches, one PR per task.

## Anti-gaming rules

Scenarios never name expected file contents beyond what the scripted agent
does; the harness never edits repos outside the agent loop; success requires
verified behavior on a fresh clone, not events alone. New product code must
pass `test_agent_reliability.py`-style unit tests first; the benchmark then
confirms end-to-end behavior.
