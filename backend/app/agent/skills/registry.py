"""Deterministic trigger -> skill resolution. No plugins, no config knobs."""

from __future__ import annotations

from dataclasses import dataclass

from .loader import load_skill_text


class UnknownSkill(ValueError):
    """Raised for unknown/unsupported/malformed trigger types."""


@dataclass(frozen=True)
class Skill:
    name: str
    trigger: str  # "issue" | "ci"
    doc_path: str
    phases: tuple[str, ...]


SKILLS: dict[str, Skill] = {
    "fix-issues": Skill(
        name="fix-issues",
        trigger="issue",
        doc_path="fix_issues.md",
        phases=(
            "UNDERSTAND",
            "INVESTIGATE",
            "REPRODUCE",
            "DIAGNOSE",
            "IMPLEMENT",
            "VALIDATE",
            "FINISH",
        ),
    ),
    "fix-ci-cd": Skill(
        name="fix-ci-cd",
        trigger="ci",
        doc_path="fix_ci_cd.md",
        phases=(
            "IDENTIFY",
            "INSPECT",
            "READ_ERROR",
            "TRACE",
            "REPRODUCE",
            "DIAGNOSE",
            "FIX",
            "VALIDATE",
            "FINISH",
        ),
    ),
}


def resolve_skill(trigger_type: str, **_ignored) -> Skill:
    """Map a task trigger to its skill. Raises UnknownSkill on anything else.

    Only "issue" and "ci" are supported in Phase 1. Empty, None-equivalent,
    or any other value fails safely instead of defaulting to the wrong skill.
    """
    trig = (trigger_type or "").strip().lower()
    if trig == "issue":
        return SKILLS["fix-issues"]
    if trig == "ci":
        return SKILLS["fix-ci-cd"]
    raise UnknownSkill(f"unsupported trigger type: {trigger_type!r}")


def skill_prompt(skill: Skill) -> str:
    """Task-specific instructions layered on top of the baseline prompt."""
    return load_skill_text(skill.doc_path)
