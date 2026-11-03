"""Pagination regression tests for the user-token GitHub surfaces.

Audit finding (Task-1 independent audit, issue #5): both
GET /user/installations and GET /user/installations/{id}/repositories
stopped after page 1 (per_page <= 100), so a repository omitted from the
first page was un-connectable (404 at connect time) and invisible in
/api/github/repos. These tests pin Link-header pagination for both list
surfaces and that API errors still fail safely.
"""

import httpx
import pytest

from app.github import client as gh


INSTALLATION_PAGES = [
    {"installations": [{"id": 1, "account": {"login": "a", "type": "User"}}]},
    {
        "installations": [
            {"id": 2, "account": {"login": "b", "type": "Organization"}},
            {"id": 3, "account": {"login": "c", "type": "Organization"}},
        ]
    },
]

REPO_PAGES = [
    {
        "repositories": [
            {"full_name": "o/r1", "private": True, "default_branch": "main"}
        ]
    },
    {
        "repositories": [
            {"full_name": "o/r2", "private": False, "default_branch": "dev"}
        ]
    },
]


def _stub_httpx(monkeypatch, pages, *, fail_after=None):
    """Two pages chained by a Link: rel=next header; failure via fail_after."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        idx = len(calls) - 1
        if fail_after is not None and idx >= fail_after:
            return httpx.Response(
                404,
                json={"message": "Not Found"},
                request=httpx.Request("GET", url),
            )
        link_headers = {}
        if idx < len(pages) - 1:
            link_headers = {
                "link": (
                    f'<https://api.github.com/next{idx + 2}>; rel="next", '
                    '<https://api.github.com/last>; rel="last"'
                )
            }
        return httpx.Response(
            200,
            json=pages[idx],
            headers=link_headers,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


def test_installations_follow_link_pages(monkeypatch):
    calls = _stub_httpx(monkeypatch, INSTALLATION_PAGES)
    out = gh.list_user_installations(token="tok")
    assert [i["id"] for i in out] == ["1", "2", "3"]
    assert len(calls) == 2  # follow-up driven by the Link rel=next url
    assert calls[1].startswith("https://api.github.com/next2")


def test_installation_repos_follow_link_pages(monkeypatch):
    _stub_httpx(monkeypatch, REPO_PAGES)
    out = gh.list_user_installation_repos(token="tok", installation_id="i1")
    assert [r["full_name"] for r in out] == ["o/r1", "o/r2"]


def test_limit_still_slices_aggregated_pages(monkeypatch):
    _stub_httpx(monkeypatch, INSTALLATION_PAGES)
    out = gh.list_user_installations(token="tok", limit=1)
    assert [i["id"] for i in out] == ["1"]


def test_api_error_mid_chain_fails_safe(monkeypatch):
    calls = _stub_httpx(monkeypatch, INSTALLATION_PAGES, fail_after=1)
    with pytest.raises(httpx.HTTPStatusError):
        gh.list_user_installations(token="tok")
    assert len(calls) == 2  # the error surfaced from the second page request
