"""LLM-backed review generation services and shared review data models."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)


class ReviewGenerationError(RuntimeError):
    """Raised when a review generator cannot return a usable result."""


@dataclass(slots=True)
class ReviewInput:
    """Structured PR context passed into the review generator."""

    pr_number: int | None
    title: str
    body: str
    additions: int
    deletions: int
    changed_files: list[str]
    head_sha: str


@dataclass(slots=True)
class ReviewSections:
    """Normalized review output returned by a review generator."""

    summary: str
    findings: list[str]
    open_questions: list[str]


class ReviewGenerator(Protocol):
    """Interface for generating structured review sections."""

    async def generate(self, review_input: ReviewInput) -> ReviewSections:
        """Return review sections for the provided PR context."""


class OpenAIReviewGenerator:
    """Generate PR review sections using the OpenAI Chat Completions API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._api_key = api_key or settings.openai_api_key
        self._model = model or settings.openai_model
        self._base_url = (base_url or settings.openai_base_url).rstrip("/")
        self._timeout_seconds = timeout_seconds or settings.openai_timeout_seconds

    async def generate(self, review_input: ReviewInput) -> ReviewSections:
        if not self._api_key:
            raise ReviewGenerationError("OpenAI API key is not configured")

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are reviewing a pull request. Return compact JSON with "
                        "keys summary, findings, and open_questions. Keep findings and "
                        "open_questions short and limited to at most 3 items each."
                    ),
                },
                {
                    "role": "user",
                    "content": self._build_prompt(review_input),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "pr_review",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "summary": {"type": "string"},
                            "findings": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "open_questions": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["summary", "findings", "open_questions"],
                    },
                },
            },
        }

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.post(
                    "/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return ReviewSections(
                summary=str(parsed["summary"]).strip(),
                findings=_normalize_items(parsed["findings"]),
                open_questions=_normalize_items(parsed["open_questions"]),
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "review_service.openai.http_error",
                error=str(exc),
                model=self._model,
            )
            raise ReviewGenerationError(f"OpenAI request failed: {exc}") from exc
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "review_service.openai.invalid_response",
                error=str(exc),
                model=self._model,
            )
            raise ReviewGenerationError("OpenAI returned an invalid review payload") from exc

    def _build_prompt(self, review_input: ReviewInput) -> str:
        changed_files = "\n".join(f"- {filename}" for filename in review_input.changed_files[:50])
        body = review_input.body.strip() or "(no PR body provided)"
        return (
            f"PR #{review_input.pr_number or 'unknown'}\n"
            f"Title: {review_input.title}\n"
            f"Head SHA: {review_input.head_sha}\n"
            f"Additions: {review_input.additions}\n"
            f"Deletions: {review_input.deletions}\n"
            f"Body:\n{body}\n\n"
            f"Changed files:\n{changed_files or '- none reported'}"
        )


def build_review_generator() -> ReviewGenerator | None:
    """Return the configured review generator, if one is enabled."""
    if settings.review_provider == "openai":
        return OpenAIReviewGenerator()
    return None


def _normalize_items(items: Any) -> list[str]:
    if not isinstance(items, list):
        raise TypeError("Expected a list of review items")
    return [str(item).strip() for item in items if str(item).strip()][:3]
