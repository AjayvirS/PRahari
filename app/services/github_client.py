"""GitHub REST API client used by the worker."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://api.github.com"
JsonDict = dict[str, Any]
JsonList = list[JsonDict]


@dataclass(slots=True)
class PullRequest:
    """A simplified pull request data model."""

    number: int
    title: str
    body: str
    additions: int
    deletions: int
    head_sha: str

    @classmethod
    def from_api_response(cls, data: JsonDict) -> PullRequest:
        """Create a PullRequest from the GitHub API response."""
        return cls(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            head_sha=data["head"]["sha"],
        )


class Client(ABC):
    """Interface for GitHub API operations used by the app."""

    @abstractmethod
    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> PullRequest:
        """Fetch a single pull request from GitHub."""

    @abstractmethod
    async def get_authenticated_user(self) -> JsonDict:
        """Fetch the authenticated GitHub user."""

    @abstractmethod
    async def list_pull_request_files(
        self, owner: str, repo: str, pr_number: int
    ) -> JsonList:
        """Fetch the changed files for a pull request."""

    @abstractmethod
    async def post_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> JsonDict:
        """Post a comment on a pull request."""

    @abstractmethod
    async def get_issue_comments(
        self, owner: str, repo: str, issue_number: int
    ) -> JsonList:
        """List issue comments for a pull request."""

    @abstractmethod
    async def get_repository_file_content(
        self, owner: str, repo: str, path: str
    ) -> str | None:
        """Fetch the content of a file in the repository, if it exists."""


class GitHubClient(Client):
    """Thin async wrapper around the GitHub REST API."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or settings.github_token
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            self._headers["Authorization"] = f"Bearer {self._token}"

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> PullRequest:
        """Fetch a single pull request from GitHub."""
        data = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
        if isinstance(data, list):
            raise TypeError("Expected a single pull request, but got a list.")
        return PullRequest.from_api_response(data)

    async def get_authenticated_user(self) -> JsonDict:
        """Fetch the authenticated GitHub user for the configured token."""
        return await self._request("GET", "/user")

    async def list_pull_request_files(
        self, owner: str, repo: str, pr_number: int
    ) -> JsonList:
        """Fetch the changed files for a pull request."""
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/files")

    async def post_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> JsonDict:
        """Post a comment on a pull request via the issues comments API."""
        return await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )

    async def get_issue_comments(
        self, owner: str, repo: str, issue_number: int
    ) -> JsonList:
        """List issue comments for a pull request."""
        return await self._request(
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            params={"per_page": 100},
        )

    async def get_repository_file_content(
        self, owner: str, repo: str, path: str
    ) -> str | None:
        """Fetch the content of a file in the repository, if it exists."""
        headers = {
            **self._headers,
            "Accept": "application/vnd.github.raw",
        }
        async with httpx.AsyncClient(base_url=_BASE_URL, timeout=30.0) as client:
            response = await client.request(
                "GET",
                f"/repos/{owner}/{repo}/contents/{path}",
                headers=headers,
            )

        if response.status_code == 404:
            return None

        response.raise_for_status()
        logger.info(
            "github.request",
            method="GET",
            path=f"/repos/{owner}/{repo}/contents/{path}",
            status_code=response.status_code,
        )
        return response.text

    async def _request(self, method: str, path: str, **kwargs: object) -> JsonDict | JsonList:
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


github_client = GitHubClient()
