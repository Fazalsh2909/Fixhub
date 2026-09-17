"""Clone-guard tests: only public github.com owner/repo URLs pass."""

import pytest

from app.repo.clone_guard import validate_github_url


def test_accepts_plain_and_git_suffix():
    assert validate_github_url("https://github.com/owner/repo") == ("owner", "repo")
    assert validate_github_url("https://github.com/owner/repo.git") == ("owner", "repo")


def test_rejects_non_github_hosts():
    for bad in [
        "https://gitlab.com/owner/repo",
        "https://github.com.evil.com/owner/repo",
        "http://github.com/owner/repo",
        "git@github.com:owner/repo.git",
    ]:
        with pytest.raises(ValueError):
            validate_github_url(bad)


def test_rejects_credentials_and_extra_paths():
    for bad in [
        "https://user:pass@github.com/owner/repo",
        "https://github.com/owner/repo/issues/1",
        "https://github.com/owner",
        "https://github.com/owner/repo/extra/path",
        "",
    ]:
        with pytest.raises(ValueError):
            validate_github_url(bad)
