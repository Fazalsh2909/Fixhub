"""Issue enrichment: references resolve to real fetched content (fail-open)."""

from app.github.enrich import enrich_issue_context, parse_references


def test_parse_references_all_shapes():
    text = "Fix #1: https://github.com/acme/api/issues/42 " "also acme/web#7 and #9"
    refs = parse_references(text, "acme/api")
    assert ("acme/api", 42) in refs
    assert ("acme/web", 7) in refs
    # Same-repo #N resolves against the connected repo ("Fix #1" → issue 1).
    assert ("acme/api", 1) in refs
    # Output capped so the prompt stays compact.
    assert len(refs) <= 3
    assert parse_references("see #9", "acme/api") == [("acme/api", 9)]


def test_parse_same_repo_hash_without_default():
    assert parse_references("see #12", "") == []


def test_parse_dedupes_and_caps():
    text = " ".join(f"acme/api#{i}" for i in range(1, 10))
    refs = parse_references(text, "acme/api")
    assert len(refs) <= 3
    assert refs[0] == ("acme/api", 1)


class _FakeClient:
    def __init__(self, issues):
        self._issues = issues
        self.calls = []

    def get_issue(self, full_name, number):
        self.calls.append((full_name, number))
        key = (full_name, number)
        if key not in self._issues:
            raise RuntimeError("not found")
        title, body = self._issues[key]
        return {"title": title, "body": body}

    def get_issue_comments(self, full_name, number):
        return [{"user": {"login": "reporter"}, "body": "still broken on main"}]


def test_enrich_fetches_primary_and_linked():
    issues = {
        ("acme/api", 1): ("Login loop", "redirects forever"),
        ("acme/web", 7): ("Button dead", "click does nothing"),
    }
    out = enrich_issue_context(
        repo_full_name="acme/api",
        issue_number=1,
        title="Fix it",
        body="see acme/web#7",
        installation_id="inst",
        _client_factory=lambda inst: _FakeClient(issues),
    )
    assert "redirects forever" in out
    assert "click does nothing" in out
    assert "still broken on main" in out


def test_enrich_fail_open_when_fetch_raises():
    def boom(inst):
        raise RuntimeError("auth exploded")

    out = enrich_issue_context(
        repo_full_name="acme/api",
        issue_number=1,
        title="Fix https://github.com/acme/api/issues/2",
        body="",
        installation_id="inst",
        _client_factory=boom,
    )
    assert out == ""


def test_enrich_skips_missing_linked_issue():
    issues = {("acme/api", 1): ("T", "primary body here")}
    out = enrich_issue_context(
        repo_full_name="acme/api",
        issue_number=1,
        title="see acme/ghost#99",
        body="",
        installation_id="inst",
        _client_factory=lambda inst: _FakeClient(issues),
    )
    assert "primary body here" in out
    assert "ghost" not in out


def test_enrich_empty_without_refs_or_number():
    out = enrich_issue_context(
        repo_full_name="",
        issue_number=0,
        title="vague",
        body="",
        installation_id="",
        _client_factory=lambda inst: _FakeClient({}),
    )
    assert out == ""
