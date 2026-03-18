"""Tests for reviewer identity resolution."""
from __future__ import annotations

import pytest

from app.reviewer_identity import ReviewerIdentityProvider


class FakeGitHubClient:
    def __init__(self, *, login: str = "prahari-bot") -> None:
        self.login = login
        self.auth_calls = 0

    async def get_authenticated_user(self) -> dict[str, str]:
        self.auth_calls += 1
        return {"login": self.login}


@pytest.mark.asyncio
async def test_identity_provider_prefers_configured_login() -> None:
    provider = ReviewerIdentityProvider(configured_login="configured-reviewer")
    client = FakeGitHubClient(login="api-reviewer")

    identity = await provider.get_identity(client)

    assert identity is not None
    assert identity.login == "configured-reviewer"
    assert client.auth_calls == 0


@pytest.mark.asyncio
async def test_identity_provider_falls_back_to_authenticated_user() -> None:
    provider = ReviewerIdentityProvider(configured_login="")
    client = FakeGitHubClient(login="api-reviewer")

    identity = await provider.get_identity(client)

    assert identity is not None
    assert identity.login == "api-reviewer"
    assert client.auth_calls == 1
