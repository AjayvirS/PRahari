"""Tests for reviewer output formatting."""
from __future__ import annotations

import pytest

import app.reviewer as reviewer
from app.review_service import ReviewGenerationError, ReviewSections


class FakeGenerator:
    async def generate(self, review_input) -> ReviewSections:
        return ReviewSections(
            summary=f"LLM summary for {review_input.title}",
            findings=["Check branch protection coverage."],
            open_questions=["Should this be split into smaller commits?"],
        )


class BrokenGenerator:
    async def generate(self, review_input) -> ReviewSections:
        raise ReviewGenerationError("service unavailable")


@pytest.mark.asyncio
async def test_build_review_comment_formats_structured_summary() -> None:
    pull_request = {
        "number": 18,
        "title": "Add structured review summaries",
        "additions": 24,
        "deletions": 5,
    }
    changed_files = [
        {"filename": "app/reviewer.py"},
        {"filename": "app/worker.py"},
        {"filename": "tests/test_reviewer.py"},
    ]

    comment = await reviewer.build_review_comment(
        pull_request,
        changed_files,
        head_sha="abc123",
    )

    assert comment.startswith("PRahari review summary")
    assert "\nSummary\n" in comment
    assert "\nPotential findings\n" in comment
    assert "\nOpen questions\n" in comment
    assert "Add structured review summaries" in comment
    assert "Primary areas: app, tests." in comment
    assert reviewer.build_review_comment_marker("abc123") in comment


@pytest.mark.asyncio
async def test_build_review_comment_uses_llm_generator_when_available() -> None:
    comment = await reviewer.build_review_comment(
        {"number": 20, "title": "Adopt review service"},
        [{"filename": "app/review_service.py"}],
        head_sha="llm123",
        generator=FakeGenerator(),
    )

    assert "LLM summary for Adopt review service" in comment
    assert "Check branch protection coverage." in comment
    assert "Should this be split into smaller commits?" in comment
    assert reviewer.build_review_comment_marker("llm123") in comment


@pytest.mark.asyncio
async def test_build_review_comment_falls_back_to_deterministic_review_on_generator_error() -> None:
    comment = await reviewer.build_review_comment(
        {"number": 21, "title": "Keep fallback path alive", "additions": 9, "deletions": 1},
        [{"filename": "app/worker.py"}],
        head_sha="fallback-sections",
        generator=BrokenGenerator(),
    )

    assert comment.startswith("PRahari review summary")
    assert "Keep fallback path alive" in comment
    assert "Should retry behavior or stale-job handling change for this path?" in comment


@pytest.mark.asyncio
async def test_build_review_comment_falls_back_to_placeholder_when_generation_fails(
    monkeypatch,
) -> None:
    def broken_summary(*args: object, **kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(reviewer, "_build_structured_review_sections", broken_summary)

    comment = await reviewer.build_review_comment(
        {"number": 19},
        [],
        head_sha="fallback-sha",
        generator=BrokenGenerator(),
    )

    assert comment == (
        "Review pipeline connected successfully for this PR head SHA fallback-sha\n\n"
        "<!-- prahari:review head_sha=fallback-sha -->"
    )
