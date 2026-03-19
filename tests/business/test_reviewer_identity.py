"""Tests for reviewer identity resolution."""
from __future__ import annotations

import pytest

from app.business.reviewer_identity import ReviewerIdentityProvider
from app.services.github_client import Client, JsonDict, JsonList


class FakeGitHubClient(Client):
    def __init__(self, *, login: str = "prahari-bot") -> None:
        self.login = login
        self.auth_calls = 0

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> JsonDict:
        raise NotImplementedError

    async def get_authenticated_user(self) -> JsonDict:
        self.auth_calls += 1
        return {"login": self.login}

    async def list_pull_request_files(
        self, owner: str, repo: str, pr_number: int
    ) -> JsonList:
        raise NotImplementedError

    async def post_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> JsonDict:
        raise NotImplementedError

    async def get_issue_comments(
        self, owner: str, repo: str, issue_number: int
    ) -> JsonList:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_identity_provider_uses_authenticated_user() -> None:
    provider = ReviewerIdentityProvider()
    client = FakeGitHubClient(login="api-reviewer")

    identity = await provider.get_identity(client)

    assert identity is not None
    assert identity.login == "api-reviewer"
    assert client.auth_calls == 1


@pytest.mark.asyncio
async def test_identity_provider_caches_authenticated_user() -> None:
    provider = ReviewerIdentityProvider()
    client = FakeGitHubClient(login="api-reviewer")

    first = await provider.get_identity(client)
    second = await provider.get_identity(client)

    assert first == second
    assert client.auth_calls == 1
