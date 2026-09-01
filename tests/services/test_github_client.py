"""Tests for GitHub client pagination behavior."""
from __future__ import annotations

import pytest

import app.services.github_client as github_client_module
from app.services.github_client import GitHubClient


class FakeResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class FakeAsyncClient:
    responses: dict[tuple[str, str, int], FakeResponse] = {}
    request_calls: list[tuple[str, str, int, int | None]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.base_url = kwargs.get("base_url")
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def request(self, method: str, path: str, *, headers: dict, **kwargs: object) -> FakeResponse:
        params = kwargs.get("params") or {}
        page = int(params.get("page", 1))
        per_page = params.get("per_page")
        self.__class__.request_calls.append((method, path, page, per_page))
        return self.__class__.responses.get((method, path, page), FakeResponse([]))


@pytest.fixture(autouse=True)
def _reset_fake_client_state(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.responses = {}
    FakeAsyncClient.request_calls = []
    monkeypatch.setattr(github_client_module.httpx, "AsyncClient", FakeAsyncClient)


@pytest.mark.asyncio
async def test_list_pull_request_files_fetches_all_pages() -> None:
    page_1 = [{"filename": f"file-{idx}.py"} for idx in range(100)]
    page_2 = [{"filename": "file-101.py"}]
    path = "/repos/acme/demo/pulls/123/files"
    FakeAsyncClient.responses = {
        ("GET", path, 1): FakeResponse(page_1),
        ("GET", path, 2): FakeResponse(page_2),
    }
    client = GitHubClient(token="test-token")

    files = await client.list_pull_request_files("acme", "demo", 123)

    assert len(files) == 101
    assert files[-1]["filename"] == "file-101.py"
    assert FakeAsyncClient.request_calls == [
        ("GET", path, 1, 100),
        ("GET", path, 2, 100),
    ]


@pytest.mark.asyncio
async def test_get_issue_comments_requests_next_page_when_page_is_full() -> None:
    path = "/repos/acme/demo/issues/321/comments"
    page_1 = [{"id": idx} for idx in range(100)]
    FakeAsyncClient.responses = {
        ("GET", path, 1): FakeResponse(page_1),
        ("GET", path, 2): FakeResponse([]),
    }
    client = GitHubClient(token="test-token")

    comments = await client.get_issue_comments("acme", "demo", 321)

    assert len(comments) == 100
    assert FakeAsyncClient.request_calls == [
        ("GET", path, 1, 100),
        ("GET", path, 2, 100),
    ]


@pytest.mark.asyncio
async def test_paginated_list_raises_type_error_for_non_list_payload() -> None:
    path = "/repos/acme/demo/issues/321/comments"
    FakeAsyncClient.responses = {
        ("GET", path, 1): FakeResponse({"unexpected": "shape"}),
    }
    client = GitHubClient(token="test-token")

    with pytest.raises(TypeError):
        await client.get_issue_comments("acme", "demo", 321)

