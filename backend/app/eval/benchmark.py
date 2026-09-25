"""Eval benchmarks: realistic tasks, real runs, honest metrics. Never invent results."""

from __future__ import annotations

# Demo fixtures (demo/fastapi-*) were deleted per user request — eval tasks
# now resolve against real cloned repos. Empty until re-registered.
TASKS: list[dict] = []

METRICS = [
    "task_success",
    "regression_rate",
    "test_pass_rate",
    "iterations",
    "tool_failures",
    "duration_s",
    "verification_failures",
    "tokens_used",
    "files_changed",
    "failure_reason",
]

# Honest, hand-verified results. Demo fixtures deleted — no recorded runs.
EVAL_RESULTS: list[dict] = []


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

    task = next((t for t in TASKS if t["id"] == task_id), None)
    if task is None:
        return {
            "task": {"id": task_id},
            "metrics": {m: None for m in METRICS},
            "recorded": None,
            "note": "eval task not registered (demo fixtures deleted)",
            "live": {
                "mode": "error",
                "verified": False,
                "results": [],
                "error": "unknown eval task",
            },
        }
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
            _attr = out.get("attribution", {}) or {}
            live = {
                "mode": "agent",
                "verified": bool(out.get("verified")),
                "overall": out.get("overall"),
                "results": out.get("results", []),
                "attribution": {
                    "verdict": _attr.get("verdict"),
                    "new": list(_attr.get("new", [])),
                    "pre_existing": int(_attr.get("pre_existing", 0)),
                    "resolved": list(_attr.get("resolved", [])),
                },
            }
        else:
            from ..verify.pipeline import overall_status, run_verification

            results = run_verification(db, task_row, workdir)
            live = {
                "mode": "verification-only",
                "verified": overall_status(results) == "VERIFIED",
                "overall": overall_status(results),
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

    files_changed: list[str] = []
    failure_reason: str | None = live.get("error")
    if task_row is not None:
        try:
            from ..repo.workspaces import git_diff_all as _diff_all
            from pathlib import Path as _P

            ws = _P(task_row.workspace_path) if task_row.workspace_path else None
            if ws is not None and ws.is_dir():
                _d = _diff_all(ws)
                files_changed = sorted(
                    {
                        ln[6:]
                        for ln in _d.splitlines()
                        if ln.startswith(("+++ b/", "--- a/"))
                    }
                )[:50]
        except Exception:
            pass
        if not live.get("verified") and not failure_reason:
            try:
                failing = [
                    getattr(r, "check", "?")
                    for r in live.get("results", [])
                    if getattr(r, "status", None) not in ("PASS", "SKIPPED")
                    and getattr(r, "required", True)
                ]
                failure_reason = (
                    f"required gates failing: {', '.join(failing)}" if failing else None
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
        "verification_failures": sum(
            1
            for r in live.get("results", [])
            if getattr(r, "status", None) != "PASS" and getattr(r, "required", True)
        ),
        "tokens_used": after["total_tokens"] - before["total_tokens"],
        "llm_calls": after["llm_calls"] - before["llm_calls"],
        "est_cost_usd": round(after["est_cost_usd"] - before["est_cost_usd"], 6),
        "files_changed": files_changed,
        "failure_reason": failure_reason,
        "verdict": live.get("overall"),
        "new_failures": (live.get("attribution", {}) or {}).get("new", []),
        "pre_existing_failures": (live.get("attribution", {}) or {}).get(
            "pre_existing", 0
        ),
    }
    return {**base, "live": live, "metrics": metrics}


def _pass_rate(results: list) -> float | None:
    if not results:
        return None
    statuses = [getattr(r, "status", None) for r in results]
    if any(s is None for s in statuses):
        # Legacy tuple shape (check, ok) — kept for recorded fixtures.
        return sum(1 for _, ok in results if ok) / len(results)
    return sum(1 for s in statuses if s == "PASS") / len(results)


def summarize_eval_table(results: list[dict]) -> str:
    """Markdown latency/token table for docs/EVAL.md. Input: list of run_benchmark() outputs."""
    lines = [
        "| task | verified | duration_s | tokens | cost_usd | tool_failures |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        m = r.get("metrics", {}) or {}
        task_id = (r.get("task", {}) or {}).get("id", "?")
        verified = (r.get("live", {}) or {}).get(
            "verified", (r.get("recorded", {}) or {}).get("verified", False)
        )
        lines.append(
            f"| {task_id} | {verified} | {m.get('duration_s')} | {m.get('tokens_used')} "
            f"| {m.get('est_cost_usd')} | {m.get('tool_failures')} |"
        )
    return "\n".join(lines)
