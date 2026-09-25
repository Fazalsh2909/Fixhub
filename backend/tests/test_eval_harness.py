"""Eval harness measures real runs, never invents PASS."""

from pathlib import Path

from app.db import SessionLocal, init_db
from app.eval.benchmark import TASKS, run_benchmark, summarize_eval_table


def test_recorded_path_unchanged():
    # Demo fixtures deleted: no registered eval tasks — unknown ids error loudly.
    assert TASKS == []
    out = run_benchmark("auth-jwt-expiry")
    assert out["task"]["id"] == "auth-jwt-expiry"
    assert out["live"]["mode"] == "error"
    assert "unknown eval task" in out["live"]["error"]


def test_live_verification_only_collects_metrics(tmp_path: Path):
    init_db()
    db = SessionLocal()
    # minimal python repo: pytest passes, no lint/type config
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n")
    out = run_benchmark("sql-nplus1", workdir=tmp_path, db=db)
    assert "live" in out
    assert out["live"]["mode"] == "error"
    assert "unknown eval task" in out["live"]["error"]
    db.close()


def test_summarize_table():
    out = run_benchmark("auth-jwt-expiry")
    md = summarize_eval_table([out])
    assert "| task | verified |" in md
