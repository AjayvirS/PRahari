"""Tests for reviewer output formatting."""
from __future__ import annotations

import app.reviewer as reviewer


def test_build_review_comment_formats_structured_summary() -> None:
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

    comment = reviewer.build_review_comment(
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


def test_build_review_comment_falls_back_to_placeholder_when_generation_fails(
    monkeypatch,
) -> None:
    def broken_summary(*args: object, **kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(reviewer, "_build_structured_review_comment", broken_summary)

    comment = reviewer.build_review_comment(
        {"number": 19},
        [],
        head_sha="fallback-sha",
    )

    assert comment == "Review pipeline connected successfully for this PR head SHA fallback-sha"
