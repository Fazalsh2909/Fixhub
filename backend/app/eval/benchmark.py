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


def run_benchmark(
    task_id: str,
    workdir=None,
    db=None,
    llm=None,
) -> dict:
    """Harness contract: recorded result + optional LIVE run.

    - No args (back-compat): return recorded result + metric keys, never invent PASS.
    - With workdir+db: run the REAL verification pipeline (or full agent loop if
      llm is provided), measure duration_s, tokens, cost, iterations,
      tool_failures. Returns measured metrics alongside recorded.
    """
    import time
    from pathlib import Path

    task = next(t for t in TASKS if t["id"] == task_id)
    recorded = next((r for r in EVAL_RESULTS if r["task_id"] == task_id), None)
    base = {
        "task": task,
        "metrics": {m: None for m in METRICS},
        "recorded": recorded,
        "note": "run via orchestrator; no fabricated results — see docs/EVAL.md",
    }
    if workdir is None or db is None:
        return base

    from ..metrics import snapshot as metrics_snapshot
    from ..models import Task as TaskRow
    from ..models import TaskEvent

    workdir = Path(workdir)
    start = time.monotonic()
    before = metrics_snapshot()
    live: dict = {"mode": "verification-only", "verified": False, "results": []}
    task_row = None
    try:
        # Use an ephemeral Task row so verification rows are persisted with evidence.
        repo_id = 0
        try:
            from ..models import Repository

            repo = db.query(Repository).filter_by(full_name=task["repo"]).first()
            repo_id = repo.id if repo else 0
        except Exception:
            repo_id = 0
        task_row = TaskRow(
            repo_id=repo_id, issue_number=0, title=f"eval:{task_id}", state="CREATED"
        )
        db.add(task_row)
        db.commit()
        db.refresh(task_row)

        if llm is not None:
            from ..agent.orchestrator import engineer_issue

            out = engineer_issue(db, task_row, workdir, llm)
            live = {
                "mode": "agent",
                "verified": bool(out.get("verified")),
                "results": out.get("results", []),
            }
        else:
            from ..verify.pipeline import run_verification

            results = run_verification(db, task_row, workdir)
            live = {
                "mode": "verification-only",
                "verified": bool(all(ok for _, ok in results)),
                "results": results,
            }
    except Exception as e:
        live = {
            "mode": "error",
            "verified": False,
            "results": [],
            "error": str(e)[:500],
        }
    duration_s = round(time.monotonic() - start, 2)
    after = metrics_snapshot()

    iterations = 0
    tool_failures = 0
    if task_row is not None:
        try:
            iterations = (
                db.query(TaskEvent).filter_by(task_id=task_row.id, stage="TOOL").count()
            )
            tool_failures = (
                db.query(TaskEvent)
                .filter(
                    TaskEvent.task_id == task_row.id,
                    TaskEvent.stage == "TOOL",
                    TaskEvent.message.like("%ok=False%"),
                )
                .count()
            )
        except Exception:
            pass

    metrics = {
        "task_success": 1 if live.get("verified") else 0,
        "regression_rate": None,  # filled by caller comparing before/after repro
        "test_pass_rate": _pass_rate(live.get("results", [])),
        "iterations": iterations,
        "tool_failures": tool_failures,
        "duration_s": duration_s,
        "verification_failures": sum(1 for _, ok in live.get("results", []) if not ok),
        "tokens_used": after["total_tokens"] - before["total_tokens"],
        "llm_calls": after["llm_calls"] - before["llm_calls"],
        "est_cost_usd": round(after["est_cost_usd"] - before["est_cost_usd"], 6),
    }
    return {**base, "live": live, "metrics": metrics}


def _pass_rate(results: list) -> float | None:
    if not results:
        return None
    return sum(1 for _, ok in results if ok) / len(results)


def summarize_eval_table(results: list[dict]) -> str:
    """Markdown latency/token table for docs/EVAL.md. Input: list of run_benchmark() outputs."""
    lines = [
        "| task | verified | duration_s | tokens | cost_usd | tool_failures |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        m = r.get("metrics", {})
        task_id = r.get("task", {}).get("id", "?")
        verified = r.get("live", {}).get(
            "verified", r.get("recorded", {}).get("verified", False)
        )
        lines.append(
            f"| {task_id} | {verified} | {m.get('duration_s')} | {m.get('tokens_used')} "
            f"| {m.get('est_cost_usd')} | {m.get('tool_failures')} |"
        )
    return "\n".join(lines)
