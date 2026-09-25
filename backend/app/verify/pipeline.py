"""Verification-first pipeline with explicit evidence for each quality gate.

P0-5 semantics: every gate reports PASS | FAIL | SKIPPED | NOT_RUN | ERROR.
SKIPPED is never PASS. Overall verdicts: VERIFIED | VERIFIED_WITH_LIMITATIONS
| FAILED | BLOCKED. Only VERIFIED counts as `verified` for approval/auto-PR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Task, VerificationRun
from ..sandbox.docker_runner import run_in_sandbox
from .signatures import compare_signatures, gate_signatures, parse_process_markers

PASS, FAIL, SKIPPED, NOT_RUN, ERROR = "PASS", "FAIL", "SKIPPED", "NOT_RUN", "ERROR"
VERIFIED, WITH_LIMITATIONS, FAILED, BLOCKED = (
    "VERIFIED",
    "VERIFIED_WITH_LIMITATIONS",
    "FAILED",
    "BLOCKED",
)
# INCONCLUSIVE: a required gate failed but no baseline exists to attribute
# against — genuine unknown, routed like legacy FAIL (human-triaged DEBUG).
INCONCLUSIVE = "INCONCLUSIVE"

# Failure attribution vocabulary. Execution status (above) says WHAT happened;
# attribution says WHOSE fault it is — decided deterministically in
# attribute_gate(), never by the LLM.
ATTR_NONE = "NONE"
ATTR_TASK = "TASK_FAILURE"
ATTR_BASELINE = "BASELINE_FAILURE"
ATTR_ENV = "ENVIRONMENT_FAILURE"
ATTR_DEP = "DEPENDENCY_FAILURE"
ATTR_INFRA = "INFRASTRUCTURE_FAILURE"
ATTR_TIMEOUT = "TIMEOUT"
ATTR_CONFIG = "CONFIGURATION_FAILURE"
ATTR_UNRELATED = "UNRELATED_REPOSITORY_FAILURE"
ATTR_UNKNOWN = "UNKNOWN"

# Row phases: the pre-patch snapshot vs post-patch gates.
PHASE_BASELINE = "BASELINE"
PHASE_AFTER = "AFTER"


@dataclass
class GateResult:
    check: str
    status: str  # one of PASS/FAIL/SKIPPED/NOT_RUN/ERROR
    required: bool
    output: str
    # Attribution layer (baseline comparison). Defaults keep every existing
    # constructor call working unchanged.
    attribution: str = ATTR_NONE
    signature: tuple = ()
    duration_ms: int = 0

    @property
    def passed(self) -> bool:
        return self.status == PASS


def overall_status(results: list[GateResult]) -> str:
    """Verdict from gate results. Required gates decide; optional gates can
    only downgrade to WITH_LIMITATIONS. BLOCKED = nothing could execute."""
    if not results:
        return BLOCKED
    if all(r.status in (ERROR, NOT_RUN) for r in results):
        return BLOCKED
    if any(r.status != PASS for r in results if r.required):
        return FAILED
    if any(r.status in (FAIL, ERROR) for r in results if not r.required):
        return WITH_LIMITATIONS
    return VERIFIED


def is_verified(results: list[GateResult]) -> bool:
    return overall_status(results) == VERIFIED


def verdict_for_task(db: Session, task_id: int) -> tuple[str, list[int], list[str]]:
    """Recompute the overall verdict from persisted VerificationRun rows.

    Attribution-aware: AFTER rows are compared against the BASELINE snapshot
    (same task) so pre-existing failures don't block approval. Legacy rows
    without a baseline fall back to exact legacy semantics. Returns
    (verdict, run_ids, gate_lines). Used by the publisher so approval
    derives from recorded evidence, never from LLM claims. No rows at all →
    BLOCKED (nothing was ever verified).
    """
    rows = (
        db.query(VerificationRun)
        .filter_by(task_id=task_id)
        .order_by(VerificationRun.id.asc())
        .all()
    )
    if not rows:
        return BLOCKED, [], []
    after = load_results(db, task_id, PHASE_AFTER)
    # Legacy rows predate phases (empty string): treat as AFTER evidence so
    # old publishes keep working.
    if not after:
        legacy: dict[str, VerificationRun] = {}
        for r in rows:
            if not (r.phase or ""):
                legacy[r.check] = r
        after = [
            GateResult(
                check=r.check,
                status=r.status or (PASS if r.passed else FAIL),
                required=bool(r.required),
                output=r.output or "",
                attribution=r.attribution or ATTR_NONE,
                signature=tuple((r.signature or "").split(";")) if r.signature else (),
                duration_ms=int(r.duration_ms or 0),
            )
            for r in legacy.values()
        ]
    if not after:
        return BLOCKED, [], []
    baseline = load_results(db, task_id, PHASE_BASELINE)
    changed = _changed_from_patch(db, task_id)
    verdict, summary, _debug, _state = attribute_verdict(after, baseline, changed)
    persist_attribution(db, task_id, after)
    run_ids = [
        r.id
        for r in rows
        if r.id is not None and (r.phase or PHASE_AFTER) == PHASE_AFTER
    ]
    gate_lines = [
        f"{r.check}: {r.status}"
        + ("" if r.required else " (optional)")
        + (f" · {r.attribution}" if r.attribution not in ("", ATTR_NONE) else "")
        for r in after
    ]
    if summary.get("pre_existing"):
        gate_lines.append(
            f"pre-existing unchanged: {summary['pre_existing']} "
            f"(new failures: {len(summary.get('new', []))})"
        )
    return verdict, run_ids, gate_lines


def _changed_from_patch(db: Session, task_id: int) -> list[str]:
    """Changed files from the latest recorded Patch diff (post-hoc path)."""
    from ..models import Patch

    patch = db.query(Patch).filter_by(task_id=task_id).order_by(Patch.id.desc()).first()
    if patch is None or not (patch.diff or "").strip():
        return []
    return changed_files_from_diff(patch.diff)


def changed_files_from_diff(diff_text: str) -> list[str]:
    """Workdir-relative changed paths from a unified diff."""
    return sorted(
        {
            line[6:].strip()
            for line in (diff_text or "").splitlines()
            if line.startswith(("+++ b/", "--- a/"))
            and line[6:].strip() not in ("dev/null", "/dev/null")
        }
    )


def load_results(db: Session, task_id: int, phase: str) -> list[GateResult]:
    """Rebuild GateResults from persisted rows of one phase."""
    rows = (
        db.query(VerificationRun)
        .filter_by(task_id=task_id, phase=phase)
        .order_by(VerificationRun.id.asc())
        .all()
    )
    latest: dict[str, VerificationRun] = {}
    for r in rows:
        latest[r.check] = r
    return [
        GateResult(
            check=r.check,
            status=r.status or (PASS if r.passed else FAIL),
            required=bool(r.required),
            output=r.output or "",
            attribution=r.attribution or ATTR_NONE,
            signature=tuple((r.signature or "").split(";")) if r.signature else (),
            duration_ms=int(r.duration_ms or 0),
        )
        for r in latest.values()
    ]


def persist_attribution(db: Session, task_id: int, results: list[GateResult]) -> None:
    """Write recomputed attributions back so API/proof readers see them."""
    rows = db.query(VerificationRun).filter_by(task_id=task_id, phase=PHASE_AFTER).all()
    by_check = {r.check: r for r in results}
    dirty = False
    for row in rows:
        res = by_check.get(row.check)
        if res is not None and (row.attribution or ATTR_NONE) != res.attribution:
            row.attribution = res.attribution
            dirty = True
    if dirty:
        db.commit()


ISOLATION_UNAVAILABLE = (
    "Verification unavailable: isolated execution environment required. "
    "Start Docker Desktop and press Run again — zero tokens were burned."
)


def _record(
    db: Session, task: Task, result: GateResult, phase: str = PHASE_AFTER
) -> GateResult:
    sigs = list(result.signature) or gate_signatures(result.check, result.output)
    # Keep the in-memory result carrying signatures too: later stages
    # (verdict summary, proof) compare these objects, not just DB rows.
    result.signature = tuple(sigs[:50])
    db.add(
        VerificationRun(
            task_id=task.id,
            check=result.check,
            passed=result.passed,
            status=result.status,
            required=result.required,
            output=result.output[-4000:],
            phase=phase,
            signature=";".join(sigs[:50]),
            attribution=result.attribution or ATTR_NONE,
            duration_ms=int(result.duration_ms or 0),
        )
    )
    db.commit()
    return result


def record_gate(
    db: Session,
    task: Task,
    check: str,
    status: str,
    required: bool,
    output: str,
    phase: str = PHASE_AFTER,
    signature: list[str] | None = None,
    attribution: str = ATTR_NONE,
    duration_ms: int = 0,
) -> GateResult:
    """Persist one gate row outside run_verification (e.g. the before/after
    regression record). Returns the GateResult for proof rendering."""
    return _record(
        db,
        task,
        GateResult(
            check,
            status,
            required,
            output,
            attribution=attribution,
            signature=tuple(signature or ()),
            duration_ms=duration_ms,
        ),
        phase,
    )


def _exit_code(output: str, name: str) -> int | None:
    match = re.search(rf"^{name}_EXIT=(\\d+)$", output, re.MULTILINE)
    return int(match.group(1)) if match else None


def _probe_project(root: Path) -> dict | None:
    """Config for one directory, or None when it has no project markers."""
    has_py = any(root.rglob("*.py"))
    has_pkg = (root / "package.json").is_file()
    has_req = (root / "requirements.txt").is_file()
    has_pytest = (root / "tests").is_dir() or (root / "test").is_dir() or has_req
    has_mypy_cfg = (
        (root / "mypy.ini").is_file()
        or (root / ".mypy.ini").is_file()
        or (root / "pyproject.toml").is_file()
        or (root / "setup.cfg").is_file()
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
    cfg = {"suite": suite, "lint": lint, "type": typ, "install": install}
    if all(v is None for v in cfg.values()):
        return None
    return cfg


def detect_verification_config(workdir: Path) -> dict:
    """Per-repo verification plan. Never hardcodes paths like `backend`.

    Override with fixhub.verify.json in the repo root:
      {"suite": "pytest -q", "lint": null, "type": "mypy src"}
    null/empty = skip that gate (recorded PASS with 'skipped' note).

    Optional keys:
      "regression": explicit before/after command (default: the suite command)
      "targeted": explicit impacted-tests command (default: derived from
        changed files; see select_targeted_tests)

    Monorepos: when the root itself has no project markers, the first
    immediate subdirectory that does (e.g. apps/api with requirements.txt +
    tests/) becomes the verification root, returned as `project_root`
    ("" = the workdir itself). Gates run with cwd set there so a root-level
    `pytest` never misreports a nested suite.
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
                    "regression": data.get("regression"),
                    "targeted": data.get("targeted"),
                    "project_root": "",
                }
        except Exception:
            pass

    root_cfg = _probe_project(workdir)
    # The suite is the meaningful signal: a root that only yields lint
    # (e.g. stray *.py matched recursively in a monorepo) must not shadow
    # a real nested suite.
    if root_cfg is not None and root_cfg.get("suite") is not None:
        return {**root_cfg, "regression": None, "targeted": None, "project_root": ""}
    # One and two levels down (covers both `api/` and `apps/api/` layouts).
    # Bounded and sorted — deterministic, no deep tree walk.
    candidates: list[Path] = []
    try:
        level1 = sorted(p for p in workdir.iterdir() if p.is_dir() and p.name != ".git")
    except OSError:
        level1 = []
    for sub in level1:
        candidates.append(sub)
        try:
            candidates.extend(sorted(p for p in sub.iterdir() if p.is_dir()))
        except OSError:
            pass
    for sub in candidates:
        cfg = _probe_project(sub)
        if cfg is not None and cfg.get("suite") is not None:
            return {
                **cfg,
                "regression": None,
                "targeted": None,
                "project_root": sub.relative_to(workdir).as_posix(),
            }
    # No nested suite: keep a lint-only root config (single-script repos),
    # else report no config at all.
    if root_cfg is not None:
        return {**root_cfg, "regression": None, "targeted": None, "project_root": ""}
    return {
        "suite": None,
        "lint": None,
        "type": None,
        "install": None,
        "regression": None,
        "targeted": None,
        "project_root": "",
    }


def _stem(path: str) -> str:
    """Comparable stem: basename without extension, test_ prefix stripped."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if base.endswith(".py"):
        base = base[:-3]
    if base.startswith("test_"):
        base = base[5:]
    elif base.endswith("_test"):
        base = base[:-5]
    return base.lower()


def select_targeted_tests(
    root: Path, changed_files: list[str], limit: int = 20
) -> list[str]:
    """Map changed source files to test files by naming convention.

    No index needed: a changed `app/auth/middleware.py` maps to
    `tests/**/test_middleware.py`; a changed test file maps to itself.
    Returns paths relative to root (posix), capped. Empty = nothing found.
    """
    nodes: list[str] = []
    try:
        test_files = (list(root.rglob("test_*.py")) + list(root.rglob("*_test.py")))[
            :400
        ]
    except OSError:
        return []
    by_stem: dict[str, list[str]] = {}
    for t in test_files:
        try:
            rel = t.relative_to(root).as_posix()
        except ValueError:
            continue
        by_stem.setdefault(_stem(rel), []).append(rel)
    for changed in changed_files or []:
        norm = changed.replace("\\", "/").lstrip("./")
        if not norm.endswith(".py"):
            continue
        if norm in (
            t.relative_to(root).as_posix() for t in test_files if _in_root(t, root)
        ):
            if norm not in nodes:
                nodes.append(norm)
            continue
        for rel in by_stem.get(_stem(norm), []):
            if rel not in nodes:
                nodes.append(rel)
        if len(nodes) >= limit:
            break
    return nodes[:limit]


def _in_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _touches_changed(new_sigs: list[str], changed_files: list[str]) -> bool:
    """True when a new failure plausibly comes from this task's patch.

    Unknown change set => True (fail safe toward debugging, matching legacy
    behavior). Otherwise the failing file must be changed itself or share a
    filename stem (test_middleware <-> middleware).
    """
    if not changed_files:
        return True
    changed_set = {c.replace("\\", "/").lstrip("./") for c in changed_files}
    changed_stems = {_stem(c) for c in changed_set}
    for sig in new_sigs:
        parts = sig.split(":", 1)
        fpath = parts[1].split("::")[0] if len(parts) > 1 else ""
        if not fpath:
            continue
        if fpath in changed_set or _stem(fpath) in changed_stems:
            return True
    return False


def attribute_gate(
    after: GateResult,
    base: "GateResult | None",
    changed_files: list[str] | None = None,
) -> str:
    """Classify one AFTER gate against its baseline twin. Deterministic —
    no LLM involved. See module docstring vocabulary."""
    changed = list(changed_files or [])
    if after.status == PASS:
        return ATTR_NONE
    if after.check == "regression" and (after.attribution or "") not in (
        "",
        ATTR_NONE,
        ATTR_UNKNOWN,
    ):
        # The orchestrator computed this from the true repro-vs-suite pair
        # (the row's mixed BEFORE|AFTER text must not be re-compared).
        return after.attribution
    if after.status in (SKIPPED, NOT_RUN):
        # Required-but-unconfigured suite = no coverage (blocks, not a fault).
        # Optional skips never matter.
        if after.check == "suite" and after.required:
            return ATTR_CONFIG
        return ATTR_NONE
    if "isolated execution environment required" in (after.output or ""):
        return ATTR_INFRA
    cat, _marker = parse_process_markers(after.output or "")
    if cat == "TIMEOUT":
        return ATTR_TIMEOUT
    if cat == "CONFIGURATION":
        return ATTR_CONFIG
    if cat == "ENVIRONMENT":
        return ATTR_ENV
    if cat == "DEPENDENCY":
        return ATTR_DEP
    if after.status == ERROR:
        return ATTR_INFRA if base is None else ATTR_UNKNOWN
    # FAIL from here on: compare normalized signatures.
    after_sigs = list(after.signature) or gate_signatures(after.check, after.output)
    if base is None:
        return ATTR_UNKNOWN
    base_sigs = list(base.signature) or gate_signatures(base.check, base.output)
    if base.status == PASS:
        # Suite passed clean, now fails: task-attributed even when the
        # output parses to nothing (a crash with no FAILED lines is new).
        return ATTR_TASK
    if base.status != FAIL:
        # Base SKIPPED/ERROR/NOT_RUN: nothing comparable — genuine unknown.
        return ATTR_UNKNOWN
    if after_sigs and after_sigs == base_sigs:
        return ATTR_BASELINE
    if _norm_outputs_equal(base.output, after.output):
        return ATTR_BASELINE
    diff = compare_signatures(base_sigs, after_sigs)
    if diff["new"]:
        return ATTR_TASK if _touches_changed(diff["new"], changed) else ATTR_UNRELATED
    if diff["unchanged"]:
        return ATTR_BASELINE
    return ATTR_UNKNOWN


def _norm_outputs_equal(a: str, b: str) -> bool:
    """Identical (modulo unstable tokens) raw outputs = unchanged failure,
    even when neither parses to signatures."""
    from .signatures import _norm_msg

    return bool((a or "").strip()) and _norm_msg(a) == _norm_msg(b)


def attribute_verdict(
    after: list[GateResult],
    baseline: list[GateResult],
    changed_files: list[str] | None = None,
) -> tuple[str, dict, bool, str]:
    """Attribution-aware verdict.

    Returns (verdict, summary, debug_task, task_state):
    - debug_task True ONLY for new task-attributed failures (the single
      condition that sends the agent back to change code).
    - task_state is the honest Task.state for the verdict.
    With no baseline rows, falls back to legacy overall_status() exactly.
    """
    changed = list(changed_files or [])
    base_by = {r.check: r for r in baseline}
    for r in after:
        base = base_by.get(r.check) or (
            base_by.get("suite") if r.check == "regression" else None
        )
        r.attribution = attribute_gate(r, base, changed)
    summary: dict = {"new": [], "resolved": [], "unchanged": [], "pre_existing": 0}
    for r in after:
        base = base_by.get(r.check) or (
            base_by.get("suite") if r.check == "regression" else None
        )
        base_sigs = list(base.signature) if base else []
        after_sigs = list(r.signature) or gate_signatures(r.check, r.output)
        diff = compare_signatures(base_sigs, after_sigs)
        summary["new"].extend(diff["new"])
        summary["resolved"].extend(diff["resolved"])
        summary["unchanged"].extend(diff["unchanged"])
        if r.attribution == ATTR_BASELINE and not diff["unchanged"]:
            # Matched by identical raw output rather than parsed signatures
            # (unparseable but byte-stable failure): still pre-existing.
            summary["unchanged"].append(f"{r.check}:unchanged-failure")
    for key in ("new", "resolved", "unchanged"):
        summary[key] = sorted(set(summary[key]))
    summary["pre_existing"] = len(summary["unchanged"])

    req = [r for r in after if r.required]
    debug_task = any(
        r.status == FAIL and r.attribution == ATTR_TASK for r in req
    ) or any(
        r.check == "regression" and r.status == FAIL and r.attribution == ATTR_TASK
        for r in after
    )

    if not after:
        return BLOCKED, summary, False, "BLOCKED"
    if all(r.status in (ERROR, NOT_RUN) for r in after):
        summary["verdict"] = BLOCKED
        return BLOCKED, summary, False, "BLOCKED"
    if not baseline:
        # Legacy path: no baseline to compare against — old semantics exactly.
        v = overall_status(after)
        summary["verdict"] = v
        legacy_state = {
            VERIFIED: "READY_FOR_APPROVAL",
            WITH_LIMITATIONS: "DEBUGGING",
            FAILED: "DEBUGGING",
            BLOCKED: "BLOCKED",
        }[v]
        return v, summary, False, legacy_state
    if debug_task:
        summary["verdict"] = FAILED
        return FAILED, summary, True, "DEBUGGING"
    if any(
        r.status == FAIL and r.attribution == ATTR_UNKNOWN and r.required for r in after
    ):
        summary["verdict"] = INCONCLUSIVE
        return INCONCLUSIVE, summary, False, "DEBUGGING"
    if any(r.attribution in (ATTR_INFRA, ATTR_TIMEOUT) and r.required for r in after):
        summary["verdict"] = BLOCKED
        return BLOCKED, summary, False, "BLOCKED"
    if any(
        r.attribution in (ATTR_ENV, ATTR_DEP, ATTR_CONFIG) and r.required for r in after
    ) or any(r.status in (SKIPPED, NOT_RUN) and r.required for r in after):
        summary["verdict"] = FAILED
        return FAILED, summary, False, "FAILED"
    if any(
        r.status == FAIL and r.required and r.attribution == ATTR_UNRELATED
        for r in after
    ) and not any(
        r.status == FAIL and r.required
        for r in after
        if r.attribution not in (ATTR_BASELINE, ATTR_UNRELATED)
    ):
        # New failures exist but none touch this task: document, don't debug.
        summary["verdict"] = FAILED
        return FAILED, summary, False, "FAILED"
    if any(r.status != PASS and r.required for r in after):
        # Remaining required FAILs are all baseline-attributed.
        summary["verdict"] = WITH_LIMITATIONS
        return WITH_LIMITATIONS, summary, False, "READY_FOR_APPROVAL"
    if any(r.status in (FAIL, ERROR) for r in after if not r.required):
        summary["verdict"] = WITH_LIMITATIONS
        return WITH_LIMITATIONS, summary, False, "READY_FOR_APPROVAL"
    summary["verdict"] = VERIFIED
    return VERIFIED, summary, False, "READY_FOR_APPROVAL"


def regression_attribution(
    repro_output: str,
    suite_after: "GateResult | None",
    changed_files: list[str] | None = None,
) -> str:
    """Attribution for the before/after regression record, computed from the
    TRUE pair (repro output vs suite-after output) — never from the row's
    mixed BEFORE|AFTER display text."""
    changed = list(changed_files or [])
    if suite_after is None or suite_after.status in (PASS, SKIPPED, NOT_RUN):
        return ATTR_NONE
    if suite_after.status == ERROR:
        cat, _ = parse_process_markers(suite_after.output or "")
        if "isolated execution environment required" in (suite_after.output or ""):
            return ATTR_INFRA
        return {
            "TIMEOUT": ATTR_TIMEOUT,
            "CONFIGURATION": ATTR_CONFIG,
            "ENVIRONMENT": ATTR_ENV,
            "DEPENDENCY": ATTR_DEP,
        }.get(cat or "", ATTR_UNKNOWN)
    cat, _ = parse_process_markers(suite_after.output or "")
    if cat == "TIMEOUT":
        return ATTR_TIMEOUT
    if cat == "CONFIGURATION":
        return ATTR_CONFIG
    if cat == "ENVIRONMENT":
        return ATTR_ENV
    if cat == "DEPENDENCY":
        return ATTR_DEP
    before_sigs = gate_signatures("suite", repro_output or "")
    after_sigs = list(suite_after.signature) or gate_signatures(
        "suite", suite_after.output or ""
    )
    if not before_sigs and not after_sigs:
        return (
            ATTR_BASELINE
            if _norm_outputs_equal(repro_output or "", suite_after.output or "")
            else ATTR_UNKNOWN
        )
    diff = compare_signatures(before_sigs, after_sigs)
    if diff["new"]:
        return ATTR_TASK if _touches_changed(diff["new"], changed) else ATTR_UNRELATED
    if diff["unchanged"]:
        return ATTR_BASELINE
    return ATTR_UNKNOWN


def build_verify_feedback(
    after: list[GateResult], summary: dict, changed_files: list[str] | None = None
) -> str:
    """Deterministic, bounded feedback for the agent loop. New task failures
    only — pre-existing counts are context, never action items. No LLM call."""
    verdict = summary.get("verdict", "")
    new = list(summary.get("new", []))[:5]
    pre = int(summary.get("pre_existing", 0))
    if summary.get("debug_task"):
        lines = [
            f"Verification found {len(summary.get('new', []))} NEW failure(s) "
            "caused by this change — fix these, then re-verify:",
            *[f"- {s}" for s in new],
        ]
        if changed_files:
            lines.append(f"Changed files: {', '.join(changed_files[:10])}")
        if pre:
            lines.append(
                f"Note: {pre} pre-existing baseline failure(s) are NOT your "
                "task — do not touch them."
            )
        return "\n".join(lines)[:1500]
    if verdict == WITH_LIMITATIONS:
        return (
            f"Verified with limitations: {pre} pre-existing failure(s) "
            f"unchanged, {len(summary.get('new', []))} new. No action required."
        )
    return f"Verification verdict: {verdict}."


def _run_gate(
    workdir: Path,
    cmd: str | None,
    label: str,
    required: bool,
    volume: str | None = None,
) -> GateResult:
    """Run one gate with mandatory isolation. P0-4 fail-closed: without an
    isolated executor the gate records ERROR (never host execution, never a
    silent PASS). P0-5: unconfigured gates record SKIPPED, never PASS."""
    if not cmd:
        return GateResult(
            label, SKIPPED, required, f"skipped — no {label} config in this repo"
        )
    res = run_in_sandbox(workdir, cmd, require_isolation=True, deps_volume=volume)
    duration_ms = int(res.get("duration_ms", 0) or 0)
    if res.get("sandbox") == "unavailable":
        return GateResult(
            label, ERROR, required, ISOLATION_UNAVAILABLE, duration_ms=duration_ms
        )
    if res.get("sandbox") == "policy":
        return GateResult(
            label,
            ERROR,
            required,
            str(res.get("output", ""))[-4000:],
            duration_ms=duration_ms,
        )
    ok = bool(res.get("ok"))
    return GateResult(
        label,
        PASS if ok else FAIL,
        required,
        str(res.get("output", ""))[-4000:],
        duration_ms=duration_ms,
    )


def run_verification(
    db: Session,
    task: Task,
    workdir: Path,
    phase: str = PHASE_AFTER,
    changed_files: list[str] | None = None,
) -> list[GateResult]:
    """Run independent gates per repo config and record their actual results.

    Each gate runs in its own sandbox call so a later PASS cannot mask an
    earlier FAIL (previous bug: combined shell + pipes hid exit codes).
    Gates execute in the detected project root (monorepo-aware), never
    assumed to be the workdir itself.

    phase=BASELINE snapshots pre-patch evidence; AFTER gates are compared
    against it by attribute_verdict(). changed_files drives the impacted
    gate (targeted tests for this task's patch); without them it SKIPs.
    """
    cfg = detect_verification_config(workdir)
    root = workdir / cfg.get("project_root", "") if cfg.get("project_root") else workdir
    from ..sandbox.docker_runner import deps_volume_for_task

    volume = deps_volume_for_task(task.id)
    # Required = the suite always (a fix with no tests proves nothing) plus
    # lint/type only when the repo configures them. Unconfigured optional
    # gates SKIP without blocking; configured ones must PASS.
    lint_required = cfg.get("lint") is not None
    type_required = cfg.get("type") is not None
    # Best-effort install into the per-task /deps volume (containers are
    # ephemeral — site-packages installs would vanish before the suite runs).
    # Failure means nothing below could run honestly: suite records ERROR,
    # lint/type record NOT_RUN (never attempted). Like every gate, the
    # install itself requires isolation (P0-4).
    if cfg.get("install"):
        install_cmd = str(cfg["install"]).replace(
            "pip install", "pip install --target /deps", 1
        )
        inst = run_in_sandbox(
            root, install_cmd, require_isolation=True, deps_volume=volume
        )
        if inst.get("sandbox") == "unavailable":
            msg = ISOLATION_UNAVAILABLE
            return [
                _record(db, task, GateResult("suite", ERROR, True, msg), phase),
                _record(db, task, GateResult("lint", ERROR, lint_required, msg), phase),
                _record(db, task, GateResult("type", ERROR, type_required, msg), phase),
            ]
        if not inst.get("ok"):
            msg = f"dependency install failed: {str(inst.get('output', ''))[-1000:]}"
            return [
                _record(db, task, GateResult("suite", ERROR, True, msg), phase),
                _record(
                    db, task, GateResult("lint", NOT_RUN, lint_required, msg), phase
                ),
                _record(
                    db, task, GateResult("type", NOT_RUN, type_required, msg), phase
                ),
            ]
    impacted = _impacted_gate(root, cfg, changed_files or [], volume)
    return [
        _record(
            db,
            task,
            _run_gate(root, cfg.get("suite"), "suite", True, volume=volume),
            phase,
        ),
        _record(db, task, impacted, phase),
        _record(
            db,
            task,
            _run_gate(root, cfg.get("lint"), "lint", lint_required, volume=volume),
            phase,
        ),
        _record(
            db,
            task,
            _run_gate(root, cfg.get("type"), "type", type_required, volume=volume),
            phase,
        ),
    ]


def _impacted_gate(
    root: Path, cfg: dict, changed_files: list[str], volume: str | None
) -> GateResult:
    """Layer-1 targeted gate: tests covering this task's changed files run
    BEFORE the broad suite counts as health. Explicit `targeted` command in
    fixhub.verify.json wins; otherwise derive pytest nodes from changed
    files. No nodes (or non-pytest suite) => SKIPPED, never blocking."""
    if cfg.get("targeted"):
        return _run_gate(root, str(cfg["targeted"]), "impacted", True, volume=volume)
    suite = cfg.get("suite") or ""
    if "pytest" not in suite:
        return GateResult(
            "impacted",
            SKIPPED,
            False,
            "skipped — targeted selection needs a pytest suite",
        )
    nodes = select_targeted_tests(root, changed_files)
    if not nodes:
        return GateResult(
            "impacted",
            SKIPPED,
            False,
            "skipped — no targeted tests found for changed files",
        )
    return _run_gate(
        root, f"{suite} {' '.join(nodes)}", "impacted", True, volume=volume
    )


def ensure_deps(
    workdir: Path, project_root: str, install_cmd: str | None, task_id: int
) -> dict:
    """Best-effort dependency install for the repro path (orchestrator).

    Same /deps volume the gates use, so before/after run in identical envs.
    Never raises: failure returns the result dict and the repro proceeds —
    its output then honestly shows the missing modules.
    """
    from ..sandbox.docker_runner import deps_volume_for_task, run_in_sandbox

    if not install_cmd:
        return {"ok": True, "output": "no install configured", "sandbox": "none"}
    root = workdir / project_root if project_root else workdir
    cmd = install_cmd.replace("pip install", "pip install --target /deps", 1)
    try:
        return run_in_sandbox(
            root, cmd, require_isolation=True, deps_volume=deps_volume_for_task(task_id)
        )
    except Exception as e:
        return {"ok": False, "output": f"install crashed: {e}", "sandbox": "error"}


def build_proof(
    task: Task,
    results: list[GateResult],
    diff: str,
    before: str,
    after: str,
    attribution: dict | None = None,
) -> str:
    """Evidence-backed Proof of Fix. Gate lines carry real statuses (never
    PASS-by-skip); the Status line is the computed overall verdict.

    With an attribution summary (from attribute_verdict), the proof records
    the baseline comparison: new vs pre-existing vs resolved failures, so a
    reader can tell task correctness apart from repository health.
    """
    verdict = (attribution or {}).get("verdict") or overall_status(results)
    gate_lines = [
        f"{r.check}: {r.status}"
        + ("" if r.required else " (optional)")
        + (f" — {r.output.splitlines()[0][:160]}" if r.status in (FAIL, ERROR) else "")
        + (
            f" [{r.attribution}]"
            if getattr(r, "attribution", ATTR_NONE) not in ("", ATTR_NONE)
            else ""
        )
        for r in results
    ]
    issue_ref = (
        f"Issue: #{task.issue_number} {task.title}"
        if task.issue_number
        else f"Issue: {task.title}"
    )
    lines = [
        "PROOF OF FIX",
        "",
        issue_ref,
        "",
        f"Before fix: {before}",
        f"After fix: {after}",
        "",
        *gate_lines,
    ]
    if attribution:
        new = list(attribution.get("new", []))
        resolved = list(attribution.get("resolved", []))
        pre = int(attribution.get("pre_existing", 0))
        lines += [
            "",
            f"New failures introduced: {len(new)}",
            *[f"- {s}" for s in new[:10]],
            f"Pre-existing failures (unchanged): {pre}",
            f"Resolved by this change: {len(resolved)}",
        ]
    lines += [
        "",
        "Files changed:",
        diff[:2000],
        "",
        f"Status: {verdict}" + (" — READY FOR PR" if verdict == VERIFIED else ""),
    ]
    return "\n".join(lines)
