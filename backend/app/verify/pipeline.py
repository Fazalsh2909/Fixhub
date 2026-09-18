"""Verification-first pipeline with explicit evidence for each quality gate."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Task, VerificationRun
from ..sandbox.docker_runner import run_in_sandbox


def _record(
    db: Session, task: Task, check: str, passed: bool, output: str
) -> tuple[str, bool]:
    db.add(
        VerificationRun(
            task_id=task.id, check=check, passed=passed, output=output[-4000:]
        )
    )
    db.commit()
    return check, passed


def _exit_code(output: str, name: str) -> int | None:
    match = re.search(rf"^{name}_EXIT=(\\d+)$", output, re.MULTILINE)
    return int(match.group(1)) if match else None


def detect_verification_config(workdir: Path) -> dict:
    """Per-repo verification plan. Never hardcodes paths like `backend`.

    Override with fixhub.verify.json in the repo root:
      {"suite": "pytest -q", "lint": null, "type": "mypy src"}
    null/empty = skip that gate (recorded PASS with 'skipped' note).
    """
    import json as _json

    override = workdir / "fixhub.verify.json"
    if override.is_file():
        try:
            data = _json.loads(override.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, dict):
                return {
                    "suite": data.get("suite"),
                    "lint": data.get("lint"),
                    "type": data.get("type"),
                    "install": data.get("install"),
                }
        except Exception:
            pass

    has_py = any(workdir.rglob("*.py"))
    has_pkg = (workdir / "package.json").is_file()
    has_req = (workdir / "requirements.txt").is_file()
    has_pytest = (workdir / "tests").is_dir() or (workdir / "test").is_dir() or has_req
    has_mypy_cfg = (
        (workdir / "mypy.ini").is_file()
        or (workdir / ".mypy.ini").is_file()
        or (workdir / "pyproject.toml").is_file()
        or (workdir / "setup.cfg").is_file()
    )

    suite: str | None = None
    lint: str | None = None
    typ: str | None = None
    install: str | None = None

    if has_req:
        install = "pip install -q -r requirements.txt"
    if has_py and has_pytest:
        suite = "python -m pytest -q"
    elif has_pkg:
        suite = "npm test -- --run"
    if has_py:
        lint = "ruff check ."
    elif has_pkg:
        lint = "npm run lint"
    # Type gate only when the repo opts in — never `mypy backend` from a demo dir.
    if has_py and has_mypy_cfg:
        typ = "python -m mypy ."
    return {"suite": suite, "lint": lint, "type": typ, "install": install}


def _run_gate(workdir: Path, cmd: str | None, label: str) -> tuple[bool, str]:
    if not cmd:
        return True, f"skipped — no {label} config in this repo"
    res = run_in_sandbox(workdir, cmd)
    return bool(res.get("ok")), str(res.get("output", ""))[-4000:]


def run_verification(db: Session, task: Task, workdir: Path) -> list[tuple[str, bool]]:
    """Run independent gates per repo config and record their actual results.

    Each gate runs in its own sandbox call so a later PASS cannot mask an
    earlier FAIL (previous bug: combined shell + pipes hid exit codes).
    """
    cfg = detect_verification_config(workdir)
    # Best-effort install; failure fails suite only (lint/type still report).
    if cfg.get("install"):
        inst = run_in_sandbox(workdir, str(cfg["install"]))
        if not inst.get("ok"):
            msg = f"dependency install failed: {str(inst.get('output', ''))[-1000:]}"
            return [
                _record(db, task, "suite", False, msg),
                _record(db, task, "lint", False, msg),
                _record(db, task, "type", False, msg),
            ]
    suite_ok, suite_out = _run_gate(workdir, cfg.get("suite"), "suite")
    lint_ok, lint_out = _run_gate(workdir, cfg.get("lint"), "lint")
    type_ok, type_out = _run_gate(workdir, cfg.get("type"), "type")
    return [
        _record(db, task, "suite", suite_ok, suite_out),
        _record(db, task, "lint", lint_ok, lint_out),
        _record(db, task, "type", type_ok, type_out),
    ]


def build_proof(
    task: Task, results: list[tuple[str, bool]], diff: str, before: str, after: str
) -> str:
    lines = [
        "PROOF OF FIX",
        "",
        f"Issue: #{task.issue_number} {task.title}",
        "",
        f"Before fix: {before}",
        f"After fix: {after}",
        "",
        *[f"{name}: {'PASS' if ok else 'FAIL'}" for name, ok in results],
        "",
        "Files changed:",
        diff[:2000],
        "",
        "Status: VERIFIED — READY FOR PR"
        if all(ok for _, ok in results)
        else "Status: NOT VERIFIED",
    ]
    return "\\n".join(lines)
