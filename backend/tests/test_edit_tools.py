"""Edit/create tool tests: constrained to workdir, unambiguous matches only."""

from pathlib import Path

from app.tools.registry import _resolve, create_file, edit_file


def test_edit_file_happy_path(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello world")
    out = edit_file(tmp_path, "a.txt", "world", "fixhub")
    assert out["ok"] is True
    assert (tmp_path / "a.txt").read_text() == "hello fixhub"


def test_edit_file_missing_old_string(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    assert edit_file(tmp_path, "a.txt", "bye", "x")["ok"] is False


def test_edit_file_refuses_ambiguous(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x x")
    assert edit_file(tmp_path, "a.txt", "x", "y")["ok"] is False


def test_edit_file_refuses_escape(tmp_path: Path):
    assert edit_file(tmp_path, "../evil.txt", "a", "b")["ok"] is False
    assert _resolve(tmp_path, "../evil.txt") is None


def test_create_file_and_refuses_overwrite(tmp_path: Path):
    assert create_file(tmp_path, "new/d.txt", "hi")["ok"] is True
    assert create_file(tmp_path, "new/d.txt", "hi")["ok"] is False
    assert create_file(tmp_path, "../out.txt", "hi")["ok"] is False


def test_edit_file_rejects_syntax_breaking_python(tmp_path: Path):
    (tmp_path / "m.py").write_text("x = 1\n")
    out = edit_file(tmp_path, "m.py", "x = 1", "x = \u2192 broken")
    assert out["ok"] is False
    assert (tmp_path / "m.py").read_text() == "x = 1\n"  # original preserved
