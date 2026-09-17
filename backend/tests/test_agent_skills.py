"""Skill injection tests: discover/select/render + graceful-off + prompt wiring."""

from pathlib import Path

from app.agent.orchestrator import _system_message
from app.agent.skills import (
    build_skill_context,
    bundled_dir,
    discover,
    render,
    resolve_skills_dir,
    select,
)


def _skill_dir(tmp_path: Path) -> Path:
    (tmp_path / "tdd-workflow").mkdir()
    (tmp_path / "tdd-workflow" / "SKILL.md").write_text(
        "---\nname: tdd-workflow\n"
        "description: Fixing bugs with test-driven development and regression tests.\n---\n\n# TDD\nWrite a failing test first.",
        encoding="utf-8",
    )
    (tmp_path / "security-review").mkdir()
    (tmp_path / "security-review" / "SKILL.md").write_text(
        "---\nname: security-review\n"
        "description: Authentication, XSS, SQL injection and secrets hardening.\n---\n\n# Security\nValidate all input.",
        encoding="utf-8",
    )
    return tmp_path


def test_discover_parses_frontmatter(tmp_path):
    skills = discover(_skill_dir(tmp_path))
    assert {s.name for s in skills} == {"tdd-workflow", "security-review"}
    assert "bugs" in next(s for s in skills if s.name == "tdd-workflow").description


def test_discover_missing_dir_is_empty(tmp_path):
    assert discover(tmp_path / "nope") == []


def test_select_picks_security_for_injection_issue(tmp_path):
    skills = discover(_skill_dir(tmp_path))
    picked = select("XSS injection in login auth bypass", skills, top_k=1)
    assert [s.name for s in picked] == ["security-review"]


def test_select_falls_back_to_tdd(tmp_path):
    skills = discover(_skill_dir(tmp_path))
    picked = select("flibber predicament quark", skills, top_k=1)
    assert [s.name for s in picked] == ["tdd-workflow"]


def test_render_respects_budget(tmp_path):
    skills = discover(_skill_dir(tmp_path))
    text = render(skills, max_chars=100)
    assert len(text) <= 100 + len(
        "Relevant expert workflows (follow their verification gates):\n"
    )
    assert "skill:" in text


def test_build_skill_context_off_when_missing(tmp_path):
    sel = build_skill_context("fix login bug", skills_dir=str(tmp_path / "nope"))
    assert sel.skills == [] and sel.text == ""


def test_build_skill_context_end_to_end(tmp_path):
    sel = build_skill_context(
        "regression test failing on checkout bug",
        skills_dir=str(_skill_dir(tmp_path)),
    )
    assert sel.skills and "tdd-workflow" in {s.name for s in sel.skills}
    assert "Write a failing test first." in sel.text


def test_system_message_includes_skills():
    plain = _system_message()
    assert "skill" not in plain and "careful engineer" in plain
    with_skills = _system_message("## skill: tdd-workflow\nWrite tests.")
    assert "skill: tdd-workflow" in with_skills
    assert "careful engineer" in with_skills


def test_resolve_prefers_explicit(tmp_path):
    assert resolve_skills_dir(str(tmp_path)) == str(tmp_path)


def test_resolve_auto_finds_bundled():
    assert Path(resolve_skills_dir("")) == bundled_dir()
    assert (bundled_dir() / "tdd-workflow" / "SKILL.md").is_file()


def test_explicit_missing_dir_stays_off(tmp_path):
    sel = build_skill_context("fix login bug", skills_dir=str(tmp_path / "nope"))
    assert sel.skills == [] and sel.text == ""


def test_bundled_end_to_end():
    sel = build_skill_context("fix checkout bug, add regression test", skills_dir="")
    names = {s.name for s in sel.skills}
    assert names and names <= {
        "tdd-workflow",
        "verification-loop",
        "security-review",
        "ai-regression-testing",
        "error-handling",
        "e2e-testing",
        "click-path-audit",
    }
    assert len(sel.text) <= 6000
