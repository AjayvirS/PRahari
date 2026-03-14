"""Tests for durable webhook enqueue behavior."""
from __future__ import annotations

from pathlib import Path

from app.database import initialize_database
from app.enqueue import enqueue_pull_request_event
from app.review_jobs import ReviewJobRepository


def test_supported_pr_event_creates_review_job(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))

    result = enqueue_pull_request_event(
        {
            "delivery_id": "delivery-1",
            "event_type": "pull_request",
            "action": "opened",
            "repo": "AjayvirS/PRahari",
            "pr_number": 12,
            "head_sha": "abc123",
            "supported": True,
        },
        repository=repository,
    )

    jobs = repository.list_jobs()

    assert result["status"] == "queued"
    assert len(jobs) == 1
    assert jobs[0].repo == "AjayvirS/PRahari"
    assert jobs[0].pr_number == 12
    assert jobs[0].head_sha == "abc123"


def test_duplicate_delivery_does_not_create_duplicate_job(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))
    metadata = {
        "delivery_id": "delivery-1",
        "event_type": "pull_request",
        "action": "synchronize",
        "repo": "AjayvirS/PRahari",
        "pr_number": 12,
        "head_sha": "abc123",
        "supported": True,
    }

    first = enqueue_pull_request_event(metadata, repository=repository)
    second = enqueue_pull_request_event(metadata, repository=repository)

    assert first["status"] == "queued"
    assert second["status"] == "duplicate"
    assert len(repository.list_jobs()) == 1

