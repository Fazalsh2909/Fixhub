"""Eval benchmarks: realistic tasks, real runs, honest metrics. Never invent results."""

from __future__ import annotations

TASKS = [
    {"id": "auth-jwt-expiry", "kind": "authentication bug", "repo": "demo/fastapi-jwt"},
    {
        "id": "pagination-offbyone",
        "kind": "pagination bug",
        "repo": "demo/fastapi-pagination",
    },
    {"id": "sql-nplus1", "kind": "SQL bug", "repo": "demo/fastapi-jwt"},
    {
        "id": "retry-idempotency",
        "kind": "retry/idempotency bug",
        "repo": "demo/fastapi-jwt",
    },
    {"id": "async-exc", "kind": "async exception", "repo": "demo/fastapi-jwt"},
]

METRICS = [
    "task_success",
    "regression_rate",
    "test_pass_rate",
    "iterations",
    "tool_failures",
    "duration_s",
    "verification_failures",
    "tokens_used",
]

# Honest, hand-verified results. Only auth-jwt-expiry has a documented live run
# (docs/DEMO-RUN-2026-09-04.md, tasks 17→21). Others are pending — not fabricated.
EVAL_RESULTS = [
    {
        "task_id": "auth-jwt-expiry",
        "status": "PASS",
        "evidence": "docs/DEMO-RUN-2026-09-04.md (task 21 verified:true, pytest 1 passed)",
        "repro": "FAILED tests/test_expiry.py — assert 500 == 401",
        "fix": "ExpiredSignatureError/InvalidTokenError → 401",
        "verified": True,
    },
    {
        "task_id": "pagination-offbyone",
        "status": "PENDING",
        "evidence": (
            "demo/fastapi-pagination regression test fails as designed "
            "(2 failed); agent runs as backend tasks 110/111: correct "
            "investigation, blocked — no Docker daemon on this Windows host "
            "for the verification lane. Task 110 also exposed a raw "
            "RemoteProtocolError leak, fixed + regression-tested."
        ),
        "repro": "pytest demo/fastapi-pagination (expect FAIL)",
        "fix": "",
        "verified": False,
    },
    {
        "task_id": "sql-nplus1",
        "status": "NOT_RUN",
        "evidence": "",
        "repro": "",
        "fix": "",
        "verified": False,
    },
    {
        "task_id": "retry-idempotency",
        "status": "NOT_RUN",
        "evidence": "",
        "repro": "",
        "fix": "",
        "verified": False,
    },
    {
        "task_id": "async-exc",
        "status": "NOT_RUN",
        "evidence": "",
        "repro": "",
        "fix": "",
        "verified": False,
    },
]


def run_benchmark(task_id: str) -> dict:
    """Harness contract: look up the task, return its recorded result + metric keys.

    Real agent runs update EVAL_RESULTS via docs; this function never invents a PASS.
    """
    task = next(t for t in TASKS if t["id"] == task_id)
    recorded = next((r for r in EVAL_RESULTS if r["task_id"] == task_id), None)
    return {
        "task": task,
        "metrics": {m: None for m in METRICS},
        "recorded": recorded,
        "note": "run via orchestrator; no fabricated results — see docs/EVAL.md",
    }
