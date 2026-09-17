"""Intel indexer tests on a tiny fixture."""

from pathlib import Path

from app.intel.indexer import index_repo, search_code


def test_index_finds_symbols(tmp_path: Path):
    (tmp_path / "a.py").write_text("class Foo:\n    def bar(self):\n        pass\n")
    files, syms = index_repo(tmp_path)
    assert any(s["name"] == "Foo" and s["kind"] == "class" for s in syms)
    assert any(s["name"] == "bar" for s in syms)


def test_search_code(tmp_path: Path):
    (tmp_path / "x.py").write_text("TOKEN_EXPIRED = 1\n")
    hits = search_code(tmp_path, "TOKEN_EXPIRED")
    assert hits and hits[0]["file"].endswith("x.py")
