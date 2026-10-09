"""Skill doc loader. Reads .md files next to this module, cached, fails closed."""

from __future__ import annotations

import os
from functools import lru_cache

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))


@lru_cache(maxsize=8)
def load_skill_text(doc_path: str) -> str:
    """Return skill markdown text, or "" when missing/unreadable.

    Fails closed to empty string so the baseline prompt still runs; callers
    should log a SKILL_LOAD_FALLBACK event when this happens.
    """
    full = os.path.join(_SKILL_DIR, doc_path)
    try:
        with open(full, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""
