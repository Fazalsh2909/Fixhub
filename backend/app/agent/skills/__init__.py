"""Phase 1 skill package. Small explicit registry, no plugin framework."""

from __future__ import annotations

from .loader import load_skill_text
from .registry import SKILLS, UnknownSkill, resolve_skill, skill_prompt

__all__ = ["SKILLS", "UnknownSkill", "load_skill_text", "resolve_skill", "skill_prompt"]
