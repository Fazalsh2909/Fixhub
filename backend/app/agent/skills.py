"""ECC/opencode skill injection for the fix agent.

Discovers locally installed skills (`<skills_dir>/*/SKILL.md`), picks the
top-k by token overlap with the issue text, and renders a budgeted context
block for the agent system prompt.

Design notes:
- Opt-in and graceful: missing/unreadable dir => "" (agent runs as before).
- Token-budgeted: total rendered chars capped (default 6000), bodies truncated.
- Skills are local markdown treated as *guidance*, not instructions that can
  widen scope: the sandbox + policy gate stay the enforcement boundary.
- Never reads symlinks escaping skills_dir; skips files > 256KB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MAX_SKILL_FILE = 256 * 1024

# Always-useful fallback for a fix agent when nothing scores above threshold.
_FALLBACK_SKILL = "tdd-workflow"

# Local user pack (dev richness) — last resort after the vendored bundle.
_LOCAL_PACK = "~/.config/opencode/skills"


def bundled_dir() -> Path:
    """Vendored skills shipped with the backend image (deploy-safe)."""
    return Path(__file__).resolve().parent / "skills_bundle"


def resolve_skills_dir(configured: str = "") -> str:
    """Explicit SKILLS_DIR > vendored bundle > local pack > '' (off).

    Never raises; returns '' when nothing usable exists.
    """
    if configured.strip():
        return configured.strip()
    try:
        if any(
            (bundled_dir() / name / "SKILL.md").is_file() for name in ("tdd-workflow",)
        ):
            return str(bundled_dir())
    except OSError:
        pass
    try:
        if Path(_LOCAL_PACK).expanduser().is_dir():
            return _LOCAL_PACK
    except OSError:
        pass
    return ""


@dataclass
class Skill:
    name: str
    description: str = ""
    path: Path | None = None


@dataclass
class SkillSelection:
    skills: list[Skill] = field(default_factory=list)
    text: str = ""


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower())) - {
        "the",
        "and",
        "for",
        "with",
        "use",
        "when",
        "you",
        "your",
        "this",
        "that",
        "from",
    }


def _parse_frontmatter(text: str) -> tuple[str, str]:
    """Extract (name, description) from YAML frontmatter; tolerant, no deps."""
    name, description = "", ""
    if not text.startswith("---"):
        return name, description
    end = text.find("\n---", 3)
    head = text[3:end] if end != -1 else text[3:2000]
    for line in head.splitlines():
        low = line.strip().lower()
        if low.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("'\"")
        elif low.startswith("description:"):
            description = line.split(":", 1)[1].strip().strip("'\"")
    return name, description


def discover(skills_dir: str | Path) -> list[Skill]:
    """List installed skills. Never raises: returns [] when unusable."""
    try:
        root = Path(skills_dir).expanduser().resolve()
    except Exception:
        return []
    if not root.is_dir():
        return []
    found: list[Skill] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            if not entry.is_dir() or entry.is_symlink():
                continue
            skill_file = (entry / "SKILL.md").resolve()
            # Containment: resolved file must stay inside skills_dir.
            if root not in skill_file.parents:
                continue
            if not skill_file.is_file() or skill_file.stat().st_size > _MAX_SKILL_FILE:
                continue
            text = skill_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        name, description = _parse_frontmatter(text)
        found.append(
            Skill(name=name or entry.name, description=description, path=skill_file)
        )
    return found


def _haystack(skill: Skill) -> set[str]:
    hay = _tokens(f"{skill.name} {skill.description}")
    # Underscored names: also match parts (security_review -> security).
    hay |= {part for token in hay for part in token.split("_") if len(part) > 2}
    return hay


def select(issue_text: str, skills: list[Skill], top_k: int = 2) -> list[Skill]:
    """Rank by rarity-weighted overlap; fallback to tdd-workflow."""
    issue = _tokens(issue_text or "")
    if not issue or not skills:
        return []
    hays = [_haystack(s) for s in skills]
    # Rarity weight: tokens in few skills count more ("jwt" beats "api").
    doc_freq: dict[str, int] = {}
    for hay in hays:
        for token in hay:
            doc_freq[token] = doc_freq.get(token, 0) + 1
    scored: list[tuple[float, Skill]] = []
    for skill, hay in zip(skills, hays):
        overlap = issue & hay
        # Minimum evidence: 2+ shared tokens, or 1 rare token (in <=5 skills).
        # Kills single-common-word noise ("returns" -> reverse-logistics).
        if len(overlap) >= 2 or any(doc_freq[t] <= 5 for t in overlap):
            scored.append((sum(1.0 / doc_freq[t] for t in overlap), skill))
    scored.sort(key=lambda t: (-t[0], t[1].name))
    picked = [s for _, s in scored[: max(top_k, 1)]]
    if not picked:
        fallback = next((s for s in skills if s.name == _FALLBACK_SKILL), None)
        if fallback is not None:
            picked = [fallback]
    return picked


def render(skills: list[Skill], max_chars: int = 6000) -> str:
    """Render skills as a budgeted prompt block (bodies truncated)."""
    header = "Relevant expert workflows (follow their verification gates):\n"
    if not skills or max_chars <= len(header):
        return ""
    max_chars -= len(header)
    per_skill = max(500, max_chars // max(len(skills), 1))
    parts: list[str] = []
    used = 0
    for skill in skills:
        body = ""
        try:
            if skill.path is not None:
                text = skill.path.read_text(encoding="utf-8", errors="ignore")
                end = text.find("\n---", 3)
                body = (
                    text[end + 4 :].strip()
                    if text.startswith("---") and end != -1
                    else text.strip()
                )
        except OSError:
            body = ""
        chunk = (
            f"## skill: {skill.name}\n{skill.description}\n{body[:per_skill]}".strip()
        )
        sep = "\n\n" if parts else ""
        room = max_chars - used - len(sep)
        if room <= 0:
            break
        parts.append(chunk[:room])
        used += len(sep) + len(parts[-1])
    if not parts:
        return ""
    return header + "\n\n".join(parts)


def build_skill_context(
    issue_text: str,
    skills_dir: str = "",
    top_k: int = 2,
    max_chars: int = 6000,
) -> SkillSelection:
    """Full pipeline: resolve dir → discover → select → render. Never raises."""
    try:
        resolved = resolve_skills_dir(skills_dir)
        if not resolved:
            return SkillSelection()
        skills = discover(resolved)
        picked = select(issue_text, skills, top_k=top_k)
        if not picked:
            return SkillSelection()
        return SkillSelection(skills=picked, text=render(picked, max_chars=max_chars))
    except Exception:
        return SkillSelection()
