"""Placeholder GitHub API client.

Uses *httpx* for async HTTP calls.  Authentication and concrete API methods
will be added incrementally once the core service skeleton is in place.
"""
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
        """Fetch a single pull request from GitHub.

        Returns the raw JSON payload as a dictionary.

        NOTE: This is a stub implementation.  The real HTTP call will be added
        once the reviewer module is wired up.
        """
        logger.info(
            "github.get_pr.stub",
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            message="GitHub client is a placeholder; no real HTTP call made",
        )
        return {}


# Module-level singleton for convenience.
github_client = GitHubClient()
