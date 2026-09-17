"""Policy engine tests (TDD guard for the security boundary)."""

from app.policy.engine import Decision, PolicyRequest, evaluate


def test_default_branch_write_is_always_deny():
    req = PolicyRequest(action="MODIFY_DEFAULT_BRANCH", branch="main")
    assert (
        evaluate(req, overrides={"MODIFY_DEFAULT_BRANCH": Decision.ALLOW})
        == Decision.DENY
    )


def test_create_pr_on_default_branch_denied():
    assert evaluate(PolicyRequest(action="CREATE_PR", branch="main")) == Decision.DENY


def test_read_and_test_allowed():
    assert evaluate(PolicyRequest(action="READ_REPOSITORY")) == Decision.ALLOW
    assert evaluate(PolicyRequest(action="RUN_TEST")) == Decision.ALLOW


def test_create_pr_feature_branch_needs_approval():
    assert (
        evaluate(PolicyRequest(action="CREATE_PR", branch="fixhub/issue-142"))
        == Decision.APPROVAL_REQUIRED
    )
