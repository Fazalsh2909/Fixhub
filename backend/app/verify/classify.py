"""Deterministic failure classification for verification results.

Seven categories only (Phase 10). No LLM, no guessing — pure rules over the
before/after pair of one gate:

- TASK_FAILURE: baseline passed, after fails (the patch broke it) — the ONLY
  category that sends the agent back to application-code debugging.
- BASELINE_FAILURE: same failure before and after (pre-existing) — document
  and continue, never debug.
- ENVIRONMENT_FAILURE: missing executable/module/stubs, connection refused —
  repair the environment or block.
- INFRASTRUCTURE_FAILURE: Docker/isolation unavailable, daemon errors —
  retry infrastructure, never debug application code.
- TIMEOUT: the command timed out.
- SKIPPED: no command configured for this gate.
- UNKNOWN: anything else (no baseline to compare, unparseable new failure
  far from the patch) — block / triage, never silently debug.
"""

from __future__ import annotations

from dataclasses import dataclass

from .signatures import (
    _norm_msg,
    compare_signatures,
    gate_signatures,
    parse_process_markers,
)

TASK_FAILURE = "TASK_FAILURE"
BASELINE_FAILURE = "BASELINE_FAILURE"
ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
TIMEOUT = "TIMEOUT"
SKIPPED = "SKIPPED"
UNKNOWN = "UNKNOWN"

CATEGORIES = (
    TASK_FAILURE,
    BASELINE_FAILURE,
    ENVIRONMENT_FAILURE,
    INFRASTRUCTURE_FAILURE,
    TIMEOUT,
    SKIPPED,
    UNKNOWN,
)

ISOLATION_MSG = "isolated execution environment required"


@dataclass(frozen=True)
class Classification:
    category: str
    reason: str


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


def _touches_changed(new_sigs: list[str], changed_files: list[str]) -> bool:
    """True when a new failure plausibly comes from this task's patch."""
    if not changed_files:
        return False
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


def classify_gate(
    *,
    check: str,
    before_status: str | None,
    before_output: str = "",
    after_status: str,
    after_output: str = "",
    changed_files: list[str] | None = None,
    command_configured: bool = True,
) -> Classification | None:
    """Classify one AFTER gate against its BEFORE twin.

    before_status None = no baseline was recorded (cannot attribute).
    Statuses: PASS | FAIL | SKIPPED | NOT_RUN | ERROR.
    Returns None when there is no failure to classify (after PASS).
    """
    changed = list(changed_files or [])
    after = after_status or "ERROR"
    # Signature parsers know suite/regression/impacted/type; "targeted" is
    # FixHub's display name for the impacted-tests gate.
    parse_check = "impacted" if check == "targeted" else check

    if after == "PASS":
        return None
    if after in ("SKIPPED", "NOT_RUN"):
        if not command_configured:
            return Classification(SKIPPED, f"{check}: no command configured")
        if ISOLATION_MSG in (after_output or ""):
            return Classification(
                INFRASTRUCTURE_FAILURE, f"{check}: isolation unavailable"
            )
        return Classification(UNKNOWN, f"{check}: did not run ({after})")
    if after == "ERROR":
        if ISOLATION_MSG in (after_output or ""):
            return Classification(
                INFRASTRUCTURE_FAILURE, f"{check}: isolation unavailable"
            )
        cat, _ = parse_process_markers(after_output or "")
        if cat == "TIMEOUT":
            return Classification(TIMEOUT, f"{check}: timed out")
        return Classification(
            INFRASTRUCTURE_FAILURE, f"{check}: could not execute ({after})"
        )

    # after == FAIL from here on.
    cat, _ = parse_process_markers(after_output or "")
    if cat == "TIMEOUT":
        return Classification(TIMEOUT, f"{check}: timed out")
    if cat == "ENVIRONMENT":
        return Classification(ENVIRONMENT_FAILURE, f"{check}: {after_output[:160]}")
    if cat == "DEPENDENCY":
        return Classification(ENVIRONMENT_FAILURE, f"{check}: missing dependency")
    if cat == "CONFIGURATION":
        return Classification(UNKNOWN, f"{check}: configuration/policy refusal")

    if before_status is None:
        return Classification(UNKNOWN, f"{check}: no baseline to compare against")
    if before_status == "PASS":
        return Classification(
            TASK_FAILURE, f"{check}: passed before, fails after the patch"
        )
    if before_status != "FAIL":
        return Classification(
            UNKNOWN, f"{check}: baseline {before_status}, nothing comparable"
        )

    after_sigs = gate_signatures(parse_check, after_output or "")
    before_sigs = gate_signatures(parse_check, before_output or "")
    if after_sigs and after_sigs == before_sigs:
        return Classification(
            BASELINE_FAILURE, f"{check}: identical failure before and after"
        )
    if (before_output or "").strip() and _norm_msg(before_output) == _norm_msg(
        after_output or ""
    ):
        return Classification(
            BASELINE_FAILURE, f"{check}: byte-stable failure before and after"
        )
    diff = compare_signatures(before_sigs, after_sigs)
    if diff["new"]:
        if _touches_changed(diff["new"], changed):
            return Classification(
                TASK_FAILURE, f"{check}: new failure touches patched files"
            )
        return Classification(
            UNKNOWN, f"{check}: new failure far from patched files — triage"
        )
    if diff["unchanged"]:
        return Classification(
            BASELINE_FAILURE, f"{check}: failures unchanged by the patch"
        )
    return Classification(UNKNOWN, f"{check}: unparseable failure change")


def should_debug(category: str | None) -> bool:
    """Only TASK_FAILURE sends the agent back to application-code debugging."""
    return category == TASK_FAILURE
