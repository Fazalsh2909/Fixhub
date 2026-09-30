"""Memory persistence, retrieval, version-awareness. Memory must come from real use."""
from app.memory import store as _m


def test_save_and_overview(db):
    _m.save_memory(db, repository="acme/demo", path="__overview__", summary="py web app, auth in backend/auth/", rev="aaa")
    assert "auth" in _m.get_repository_overview(db, repository="acme/demo")


def test_update_is_version_aware(db):
    _m.save_memory(db, repository="acme/demo", path="__overview__", summary="v1", rev="aaa")
    row = _m.update_repository_memory(db, repository="acme/demo", summary="v2", rev="bbb")
    assert row.last_analyzed_rev == "bbb" and row.commit_sha == "bbb"
    assert _m.get_repository_overview(db, repository="acme/demo") == "v2"


def test_search_relevance(db):
    _m.save_memory(db, repository="acme/demo", path="backend/auth/login.py", summary="JWT login handler", rev="a")
    _m.save_memory(db, repository="acme/demo", path="frontend/app.js", summary="landing page", rev="a")
    hits = _m.search_memory(db, repository="acme/demo", query="JWT login")
    assert hits and hits[0].path == "backend/auth/login.py"


def test_repositories_isolated(db):
    _m.save_memory(db, repository="acme/a", path="__overview__", summary="aaa-overview", rev="1")
    assert _m.get_repository_overview(db, repository="acme/b") == ""
