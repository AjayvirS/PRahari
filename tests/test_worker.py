"""Tests for the durable review job worker."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.database import initialize_database
from app.review_jobs import COMPLETED_STATUS, FAILED_STATUS, ReviewJobRepository
from app.worker import process_next_job


class FakeGitHubClient:
    def __init__(self, *, fail_on_comment: bool = False) -> None:
        self.fail_on_comment = fail_on_comment
        self.fetch_calls: list[tuple[str, str, int]] = []
        self.comment_calls: list[tuple[str, str, int, str]] = []

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict:
        self.fetch_calls.append((owner, repo, pr_number))
        return {"number": pr_number}

    async def post_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict:
        self.comment_calls.append((owner, repo, issue_number, body))
        if self.fail_on_comment:
            raise RuntimeError("comment failed")
        return {"id": 1}


@pytest.mark.asyncio
async def test_worker_claims_one_pending_job_and_posts_comment(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))
    job, _ = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=14,
        head_sha="abc123",
    )
    client = FakeGitHubClient()

    processed = await process_next_job(repository=repository, client=client)

    assert processed is not None
    assert processed.job_id == job.job_id
    assert processed.status == COMPLETED_STATUS
    assert client.fetch_calls == [("AjayvirS", "PRahari", 14)]
    assert client.comment_calls == [
        (
            "AjayvirS",
            "PRahari",
            14,
            "Review pipeline connected successfully for this PR head SHA abc123",
        )
    ]


@pytest.mark.asyncio
async def test_worker_marks_job_failed_on_error(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))
    job, _ = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=15,
        head_sha="deadbeef",
    )
    client = FakeGitHubClient(fail_on_comment=True)

    processed = await process_next_job(repository=repository, client=client)

    assert processed is not None
    assert processed.job_id == job.job_id
    assert processed.status == FAILED_STATUS
    assert processed.last_error == "comment failed"


@pytest.mark.asyncio
async def test_worker_does_not_process_same_job_twice(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))
    repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=16,
        head_sha="once-only",
    )
    client = FakeGitHubClient()

    first = await process_next_job(repository=repository, client=client)
    second = await process_next_job(repository=repository, client=client)

    assert first is not None
    assert second is None
    assert len(client.comment_calls) == 1
