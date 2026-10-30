"""Task-instruction doc loader. Cached reads, fails closed.

Docs live next to this module (skills/*.md) or in ../instructions/ (task
instruction documents such as CI_REPAIR.md). Missing/unreadable files resolve
to "" so the baseline prompt still runs; callers log SKILL_LOAD_FALLBACK.
"""

from __future__ import annotations

import os
from functools import lru_cache

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
# Task instruction documents, e.g. backend/app/agent/instructions/.
_INSTRUCTIONS_DIR = os.path.join(os.path.dirname(_SKILL_DIR), "instructions")


@lru_cache(maxsize=8)
def load_skill_text(doc_path: str) -> str:
    """Return doc text (skills/ first, then instructions/), or "" when
    missing/unreadable.

    Fails closed to empty string so the baseline prompt still runs; callers
    should log a SKILL_LOAD_FALLBACK event when this happens.
    """
    for base in (_SKILL_DIR, _INSTRUCTIONS_DIR):
        full = os.path.join(base, doc_path)
        try:
            with open(full, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            continue
    return ""
