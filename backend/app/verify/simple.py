"""Simplified verification: FOUR core checks, deterministic verdict.

Replaces the "8 gates must all go green" philosophy with:
  1. regression — the same command before and after (strongest evidence)
  2. targeted — tests covering the changed files (or explicit config)
  3. lint — only when the repo configures it
  4. typecheck/build — supporting evidence only, NEVER required

Each AFTER gate is classified against its BEFORE twin with
verify/classify.py (7 categories). Only TASK_FAILURE debugs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Task
from .classify import (
    BASELINE_FAILURE,
    ENVIRONMENT_FAILURE,
    INFRASTRUCTURE_FAILURE,
    SKIPPED,
    TASK_FAILURE,
    TIMEOUT,
    UNKNOWN,
    Classification,
    classify_gate,
)
from .pipeline import (
    PHASE_AFTER,
    GateResult,
    detect_verification_config,
    ensure_deps,
    load_results,
    record_gate,
    select_targeted_tests,
)

VERIFIED = "VERIFIED"
WITH_LIMITATIONS = "VERIFIED_WITH_LIMITATIONS"
FAILED = "FAILED"
BLOCKED = "BLOCKED"

FOUR_CHECKS = ("regression", "targeted", "lint", "type")


@dataclass
class SimpleVerdict:
    verdict: str
    task_state: str
    debug: bool
    classifications: dict[str, Classification | None] = field(default_factory=dict)
    new_failure_lines: list[str] = field(default_factory=list)
    pre_existing: int = 0

    @property
    def publishable(self) -> bool:
        return self.verdict in (VERIFIED, WITH_LIMITATIONS)

    @property
    def verified(self) -> bool:
        return self.verdict == VERIFIED


def _commands(workdir: Path, changed_files: list[str]) -> dict:
    """Resolve the four check commands for a repo (None = not configured)."""
    cfg = detect_verification_config(workdir)
    root = workdir / cfg.get("project_root", "") if cfg.get("project_root") else workdir
    regression = cfg.get("regression") or cfg.get("suite")
    if cfg.get("targeted"):
        targeted: str | None = str(cfg["targeted"])
        targeted_required = True
    else:
        suite = cfg.get("suite") or ""
        nodes = select_targeted_tests(root, changed_files) if "pytest" in suite else []
        targeted = f"{suite} {' '.join(nodes)}" if nodes else None
        targeted_required = False
    return {
        "root": root,
        "install": cfg.get("install"),
        "project_root": cfg.get("project_root", "") or "",
        "regression": regression,
        "targeted": targeted,
        "targeted_required": targeted_required,
        "lint": cfg.get("lint"),
        "lint_required": cfg.get("lint") is not None,
        "type": cfg.get("type"),
    }


def run_four_checks(
    db: Session,
    task: Task,
    workdir: Path,
    phase: str = PHASE_AFTER,
    changed_files: list[str] | None = None,
) -> list[GateResult]:
    """Run the four checks and persist one VerificationRun row per check."""
    from .pipeline import _run_gate
    from ..sandbox.docker_runner import deps_volume_for_task

    changed = list(changed_files or [])
    cmds = _commands(workdir, changed)
    ensure_deps(workdir, cmds["project_root"], cmds["install"], task.id)
    volume = deps_volume_for_task(task.id)
    root: Path = cmds["root"]
    plan = [
        ("regression", cmds["regression"], True),
        ("targeted", cmds["targeted"], bool(cmds["targeted_required"])),
        ("lint", cmds["lint"], bool(cmds["lint_required"])),
        ("type", cmds["type"], False),
    ]
    out: list[GateResult] = []
    for check, cmd, required in plan:
        gate = _run_gate(root, cmd, check, required, volume=volume)
        out.append(
            record_gate(
                db,
                task,
                gate.check,
                gate.status,
                gate.required,
                gate.output,
                phase=phase,
            )
        )
    return out


def decide_verdict(
    before: list[GateResult],
    after: list[GateResult],
    changed_files: list[str] | None = None,
) -> SimpleVerdict:
    """Deterministic verdict from before/after pairs. See module docstring."""
    changed = list(changed_files or [])
    base_by = {r.check: r for r in before}
    classifications: dict[str, Classification | None] = {}
    for r in after:
        base = base_by.get(r.check)
        output = r.output or ""
        unconfigured = (
            f"no {r.check} config" in output
            or "no targeted tests found" in output
            or "needs a pytest suite" in output
        )
        classifications[r.check] = classify_gate(
            check=r.check,
            before_status=base.status if base is not None else None,
            before_output=base.output if base is not None else "",
            after_status=r.status,
            after_output=r.output,
            changed_files=changed,
            command_configured=not unconfigured,
        )
    req = [r for r in after if r.required]
    cats: dict[str, str | None] = {}
    for r in req:
        c = classifications.get(r.check)
        cats[r.check] = c.category if c is not None else None

    def _has(*names: str) -> bool:
        return any(c in names for c in cats.values())

    new_lines: list[str] = []
    for r in after:
        c = classifications.get(r.check)
        if c is not None and c.category == TASK_FAILURE and r.required:
            first = (r.output or "").splitlines()
            new_lines.append(f"{r.check}: {first[0][:160] if first else 'failed'}")
    pre = sum(1 for r in req if cats.get(r.check) == BASELINE_FAILURE)

    if _has(TASK_FAILURE):
        return SimpleVerdict(
            FAILED, "DEBUGGING", True, classifications, new_lines[:5], pre
        )
    if _has(UNKNOWN):
        return SimpleVerdict(
            BLOCKED, "BLOCKED", False, classifications, new_lines[:5], pre
        )
    if _has(INFRASTRUCTURE_FAILURE, TIMEOUT):
        return SimpleVerdict(
            BLOCKED, "BLOCKED", False, classifications, new_lines[:5], pre
        )
    if _has(ENVIRONMENT_FAILURE):
        return SimpleVerdict(
            FAILED, "FAILED", False, classifications, new_lines[:5], pre
        )
    if any(r.status in ("SKIPPED", "NOT_RUN") and r.required for r in after):
        unconfigured_checks = [
            r.check for r in after if r.required and r.status in ("SKIPPED", "NOT_RUN")
        ]
        for check in unconfigured_checks:
            classifications[check] = Classification(
                SKIPPED, f"{check}: no command configured"
            )
        return SimpleVerdict(FAILED, "FAILED", False, classifications, [], pre)
    if _has(BASELINE_FAILURE):
        return SimpleVerdict(
            WITH_LIMITATIONS, "READY_FOR_APPROVAL", False, classifications, [], pre
        )
    if any(r.status in ("FAIL", "ERROR") for r in after if not r.required):
        return SimpleVerdict(
            WITH_LIMITATIONS, "READY_FOR_APPROVAL", False, classifications, [], pre
        )
    if any(r.status != "PASS" for r in req):
        # Any remaining non-passing required gate (defensive; the cases above
        # should have caught it) blocks with triage, never fake-verifies.
        return SimpleVerdict(BLOCKED, "BLOCKED", False, classifications, [], pre)
    return SimpleVerdict(
        VERIFIED, "READY_FOR_APPROVAL", False, classifications, [], pre
    )


def first_line(output: str) -> str:
    lines = (output or "").splitlines()
    return lines[0][:200] if lines else "no output"


ATTRIBUTION_MAP = {
    TASK_FAILURE: "TASK_FAILURE",
    BASELINE_FAILURE: "BASELINE_FAILURE",
    ENVIRONMENT_FAILURE: "ENVIRONMENT_FAILURE",
    INFRASTRUCTURE_FAILURE: "INFRASTRUCTURE_FAILURE",
    TIMEOUT: "TIMEOUT",
    UNKNOWN: "UNKNOWN",
}


def persist_simple_attribution(
    db: Session, task_id: int, verdict: SimpleVerdict
) -> None:
    """Write classifications back to AFTER rows so API/proof readers see WHY.

    Without this the rows keep attribution=NONE and the UI can only say
    "FAILED" with no reason (live task 239).
    """
    from ..models import VerificationRun

    rows = db.query(VerificationRun).filter_by(task_id=task_id, phase=PHASE_AFTER).all()
    dirty = False
    for row in rows:
        c = verdict.classifications.get(row.check)
        want = ATTRIBUTION_MAP.get(c.category) if c is not None else None
        if want and (row.attribution or "NONE") != want:
            row.attribution = want
            dirty = True
    if dirty:
        db.commit()


def verdict_summary(verdict: SimpleVerdict) -> str:
    """One-line per-check account for the VERDICT event (no raw outputs)."""
    parts = []
    for check, c in verdict.classifications.items():
        parts.append(f"{check}={c.category if c is not None else 'PASS'}")
    return "; ".join(parts)


def build_simple_proof(
    *,
    issue_ref: str,
    base_sha: str,
    before: list[GateResult],
    after: list[GateResult],
    verdict: SimpleVerdict,
    changed_files: list[str],
    diff_head: str = "",
) -> str:
    """Honest Proof of Fix. Never prints 8/8 PASS when that isn't true."""
    base_by = {r.check: r for r in before}
    after_by = {r.check: r for r in after}

    def _arrow(check: str) -> str:
        b = base_by.get(check)
        a = after_by.get(check)
        bs = b.status if b else "—"
        as_ = a.status if a else "—"
        extra = ""
        if a is not None and a.status in ("FAIL", "ERROR"):
            extra = f" — {first_line(a.output)}"
        return f"{bs} → {as_}{extra}"

    lines = [
        "PROOF OF FIX",
        "",
        issue_ref,
        f"Base commit: {base_sha or 'snapshot'}",
        "",
        "Regression:",
        _arrow("regression"),
        "",
        "Targeted tests:",
        _arrow("targeted"),
        "",
        "Lint:",
        _arrow("lint"),
        "",
        "Typecheck:",
        _arrow("type"),
        "",
        f"New failures: {len(verdict.new_failure_lines)}",
        *[f"- {line}" for line in verdict.new_failure_lines[:5]],
        f"Pre-existing failures (unchanged): {verdict.pre_existing}",
        "",
        "Files changed:",
        ", ".join(changed_files[:20]) if changed_files else "(none)",
    ]
    if diff_head.strip():
        lines += ["", diff_head[:1000]]
    lines += ["", f"Result: {verdict.verdict}"]
    return "\n".join(lines)


__all__ = [
    "BLOCKED",
    "FAILED",
    "FOUR_CHECKS",
    "VERIFIED",
    "WITH_LIMITATIONS",
    "SimpleVerdict",
    "build_simple_proof",
    "decide_verdict",
    "first_line",
    "load_results",
    "persist_simple_attribution",
    "run_four_checks",
    "verdict_summary",
]
