"""Publisher guard tests: no proof → no PR; default branch → no push."""

import pytest

from app.github.publisher import PolicyDeniedError, PRPublisher, VerifiedArtifact


def _art(**kw):
    base = {
        "repo_full_name": "a/b",
        "base_branch": "main",
        "new_branch": "fixhub/issue-1",
        "patch_diff": "diff",
        "title": "t",
        "body": "b",
        "proof_passed": True,
    }
    base.update(kw)
    return VerifiedArtifact(**base)


def test_refuses_without_proof():
    pub = PRPublisher("tok")
    with pytest.raises(PolicyDeniedError):
        pub.publish(_art(proof_passed=False))


def test_refuses_default_branch_push():
    pub = PRPublisher("tok")
    with pytest.raises(PolicyDeniedError):
        pub.publish(_art(new_branch="main"))
