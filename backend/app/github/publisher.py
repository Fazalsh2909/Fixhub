"""PR Publisher — the ONLY GitHub write path. Tightly scoped by design.

Accepts a VerifiedArtifact dataclass, never natural language. Can only:
  create_branch / push verified patch / create_pull_request.
Refuses default-branch targets. Agent has no other write tool.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

API = "https://api.github.com"


@dataclass
class VerifiedArtifact:
    repo_full_name: str
    base_branch: str
    new_branch: str
    patch_diff: str  # unified diff, already verified
    title: str
    body: str  # includes Proof of Fix
    proof_passed: bool


class PolicyDeniedError(PermissionError):
    pass


class PRPublisher:
    def __init__(self, installation_token: str, default_branch: str = "main") -> None:
        self._token = installation_token
        self._default = default_branch

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }

    def publish(self, artifact: VerifiedArtifact) -> dict:
        if not artifact.proof_passed:
            raise PolicyDeniedError("proof did not pass — refusing to publish")
        if artifact.new_branch in ("main", "master"):
            raise PolicyDeniedError("must not push directly to default branch")
        # Real implementation: create branch ref, push via git-over-https in worker, open PR.
        # Thin scaffold performs the PR creation call; git push happens in sandbox worker
        # with the same branch guard. Kept explicit so tests can assert the guard.
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{API}/repos/{artifact.repo_full_name}/pulls",
                headers=self._headers(),
                json={
                    "title": artifact.title,
                    "head": artifact.new_branch,
                    "base": artifact.base_branch,
                    "body": artifact.body,
                },
            )
            r.raise_for_status()
            return r.json()
