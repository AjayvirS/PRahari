"""GitHub REST API client used by the worker."""
from __future__ import annotations

import httpx

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://api.github.com"


class GitHubClient:
    """Thin async wrapper around the GitHub REST API."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or settings.github_token
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            self._headers["Authorization"] = f"Bearer {self._token}"

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        """Fetch a single pull request from GitHub."""
        return await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
        )

    async def list_pull_request_files(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict]:
        """Fetch the changed files for a pull request."""
        return await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
        )

    async def post_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict:
        """Post a comment on a pull request via the issues comments API."""
        return await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )

    async def list_issue_comments(
        self, owner: str, repo: str, issue_number: int
    ) -> list[dict]:
        """List issue comments for a pull request."""
        return await self._request(
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            params={"per_page": 100},
        )

    async def _request(self, method: str, path: str, **kwargs: object) -> dict | list[dict]:
        async with httpx.AsyncClient(base_url=_BASE_URL, timeout=30.0) as client:
            response = await client.request(
                method,
                path,
                headers=self._headers,
                **kwargs,
            )

        response.raise_for_status()
        payload = response.json()
        logger.info("github.request", method=method, path=path, status_code=response.status_code)
        return payload


# Module-level singleton for convenience.
github_client = GitHubClient()
