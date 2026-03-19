"""Tests for the OpenAI-backed review service boundary."""
from __future__ import annotations

import pytest

import app.services.review_service as review_service
from app.services.review_service import OpenAIReviewGenerator, ReviewGenerationError, ReviewInput


class FakeResponse:
    def __init__(self, payload: dict, *, raise_error: Exception | None = None) -> None:
        self._payload = payload
        self._raise_error = raise_error

    def raise_for_status(self) -> None:
        if self._raise_error is not None:
            raise self._raise_error

    def json(self) -> dict:
        return self._payload


class FakeAsyncClient:
    response = FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"summary":"LLM summary","findings":["One"],'
                            '"open_questions":["Question?"]}'
                        )
                    }
                }
            ]
        }
    )

    def __init__(self, *args, **kwargs) -> None:
        self.base_url = kwargs.get("base_url")
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, path: str, *, headers: dict, json: dict) -> FakeResponse:
        self.path = path
        self.headers = headers
        self.json_payload = json
        return self.response


@pytest.mark.asyncio
async def test_openai_review_generator_parses_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_service.httpx, "AsyncClient", FakeAsyncClient)
    generator = OpenAIReviewGenerator(
        api_key="test-key",
        model="gpt-test",
        base_url="https://api.openai.com/v1",
        timeout_seconds=5.0,
    )

    sections = await generator.generate(
        ReviewInput(
            pr_number=9,
            title="Add OpenAI integration",
            body="PR body",
            additions=10,
            deletions=2,
            changed_files=["app/reviewer.py"],
            head_sha="abc123",
        )
    )

    assert sections.summary == "LLM summary"
    assert sections.findings == ["One"]
    assert sections.open_questions == ["Question?"]


@pytest.mark.asyncio
async def test_openai_review_generator_wraps_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = review_service.httpx.HTTPStatusError(
        "bad request",
        request=review_service.httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        response=review_service.httpx.Response(400),
    )
    FakeAsyncClient.response = FakeResponse({}, raise_error=error)
    monkeypatch.setattr(review_service.httpx, "AsyncClient", FakeAsyncClient)
    generator = OpenAIReviewGenerator(api_key="test-key")

    with pytest.raises(ReviewGenerationError):
        await generator.generate(
            ReviewInput(
                pr_number=9,
                title="Add OpenAI integration",
                body="PR body",
                additions=10,
                deletions=2,
                changed_files=["app/reviewer.py"],
                head_sha="abc123",
            )
        )
