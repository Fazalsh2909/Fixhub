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


def run_verification(db: Session, task: Task, workdir: Path) -> list[tuple[str, bool]]:
    """Run independent gates and record their actual exit codes.

    Each gate is evaluated independently, so a successful later command cannot
    mask a failed lint or type-check result.
    """
    combined = run_in_sandbox(
        workdir,
        "pip install -q -r requirements.txt || { echo INSTALL_EXIT=1; exit 1; }; "
        "python -m pytest -q > /tmp/suite.log 2>&1; SUITE=$?; "
        "ruff check . > /tmp/lint.log 2>&1; LINT=$?; "
        "python -m mypy backend > /tmp/type.log 2>&1; TYPE=$?; "
        "cat /tmp/suite.log; echo '---LINT---'; cat /tmp/lint.log; "
        "echo '---TYPE---'; cat /tmp/type.log; "
        "echo SUITE_EXIT=$SUITE; echo LINT_EXIT=$LINT; echo TYPE_EXIT=$TYPE; exit 0",
    )

    output = combined["output"]
    if "INSTALL_EXIT=1" in output:
        failure = "dependency installation failed"
        return [
            _record(db, task, "suite", False, failure),
            _record(db, task, "lint", False, failure),
            _record(db, task, "type", False, failure),
        ]

    suite_exit = _exit_code(output, "SUITE")
    lint_exit = _exit_code(output, "LINT")
    type_exit = _exit_code(output, "TYPE")

    return [
        _record(db, task, "suite", suite_exit == 0, output),
        _record(db, task, "lint", lint_exit == 0, output),
        _record(db, task, "type", type_exit == 0, output),
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
