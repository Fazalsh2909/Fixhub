"""Read-only GitHub client. The agent may use THIS. No write methods exist here by design."""

from __future__ import annotations

import httpx

API = "https://api.github.com"


class GitHubReadClient:
    def __init__(self, installation_token: str) -> None:
        self._token = installation_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }

    def _get(self, path: str) -> dict | list:
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{API}{path}", headers=self._headers())
            r.raise_for_status()
            return r.json()

    def get_repository(self, full_name: str) -> dict:
        return self._get(f"/repos/{full_name}")  # type: ignore[return-value]

    def get_issue(self, full_name: str, number: int) -> dict:
        return self._get(f"/repos/{full_name}/issues/{number}")  # type: ignore[return-value]

    def get_issue_comments(self, full_name: str, number: int) -> list:
        return self._get(f"/repos/{full_name}/issues/{number}/comments")  # type: ignore[return-value]

    def get_file(self, full_name: str, path: str, ref: str = "HEAD") -> dict:
        return self._get(f"/repos/{full_name}/contents/{path}?ref={ref}")  # type: ignore[return-value]

    def get_pull_request(self, full_name: str, number: int) -> dict:
        return self._get(f"/repos/{full_name}/pulls/{number}")  # type: ignore[return-value]

    def get_check_runs(self, full_name: str, ref: str) -> dict:
        return self._get(f"/repos/{full_name}/commits/{ref}/check-runs")  # type: ignore[return-value]

    def list_installation_repos(self) -> dict | list:
        return self._get("/installation/repositories")

    def list_repo_issues(
        self, full_name: str, state: str = "open", limit: int = 30
    ) -> dict | list:
        return self._get(
            f"/repos/{full_name}/issues?state={state}&per_page={max(1, min(limit, 100))}"
        )
