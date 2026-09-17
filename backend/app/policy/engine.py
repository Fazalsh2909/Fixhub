"""Policy engine. Permissions decided HERE, never by the LLM."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    ALLOW = "ALLOW"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DENY = "DENY"


@dataclass
class PolicyRequest:
    action: str  # e.g. READ_REPOSITORY, RUN_TEST, MODIFY_SANDBOX, CREATE_BRANCH, CREATE_PR, MODIFY_DEFAULT_BRANCH
    task_id: int = 0
    branch: str = ""


_DEFAULTS: dict[str, Decision] = {
    "READ_REPOSITORY": Decision.ALLOW,
    "SEARCH_CODE": Decision.ALLOW,
    "RUN_TEST": Decision.ALLOW,
    "MODIFY_SANDBOX": Decision.ALLOW,
    "INSTALL_DEPENDENCY": Decision.APPROVAL_REQUIRED,
    "CREATE_BRANCH": Decision.APPROVAL_REQUIRED,
    "CREATE_PR": Decision.APPROVAL_REQUIRED,
    "MODIFY_DEFAULT_BRANCH": Decision.DENY,
}


def evaluate(
    req: PolicyRequest, overrides: dict[str, Decision] | None = None
) -> Decision:
    rules = {**_DEFAULTS, **(overrides or {})}
    if req.action == "MODIFY_DEFAULT_BRANCH":
        return Decision.DENY  # can never be overridden to ALLOW
    if req.action in ("CREATE_BRANCH", "CREATE_PR") and req.branch in (
        "main",
        "master",
    ):
        return Decision.DENY
    return rules.get(req.action, Decision.APPROVAL_REQUIRED)
