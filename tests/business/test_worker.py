"""Tests for the durable review job worker."""
from __future__ import annotations

from pathlib import Path

import pytest

import app.business.reviewer as reviewer
from app.business.reviewer import build_review_comment_marker
from app.business.reviewer_identity import ReviewerIdentityProvider
from app.business.worker import process_next_job
from app.database.connection import initialize_database
from app.database.review_jobs import COMPLETED_STATUS, FAILED_STATUS, ReviewJobRepository
from app.services.github_client import Client, JsonDict, JsonList


class FakeGitHubClient(Client):
    def __init__(
        self,
        *,
        fail_on_comment: bool = False,
        existing_comments: list[dict[str, object]] | None = None,
        authenticated_login: str = "prahari-bot",
    ) -> None:
        self.fail_on_comment = fail_on_comment
        self.existing_comments = existing_comments or []
        self.authenticated_login = authenticated_login
        self.auth_calls = 0
        self.fetch_calls: list[tuple[str, str, int]] = []
        self.comments_fetch_calls: list[tuple[str, str, int]] = []
        self.files_calls: list[tuple[str, str, int]] = []
        self.comment_calls: list[tuple[str, str, int, str]] = []

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> JsonDict:
        self.fetch_calls.append((owner, repo, pr_number))
        return {
            "number": pr_number,
            "title": "Add structured PR review summary",
            "additions": 42,
            "deletions": 7,
        }

    async def list_pull_request_files(
        self, owner: str, repo: str, pr_number: int
    ) -> JsonList:
        self.files_calls.append((owner, repo, pr_number))
        return [
            {"filename": "app/reviewer.py"},
            {"filename": "tests/test_reviewer.py"},
        ]

    async def get_issue_comments(
        self, owner: str, repo: str, issue_number: int
    ) -> JsonList:
        self.comments_fetch_calls.append((owner, repo, issue_number))
        return self.existing_comments

    async def get_authenticated_user(self) -> JsonDict:
        self.auth_calls += 1
        return {"login": self.authenticated_login}

    async def post_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> JsonDict:
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
    assert client.comments_fetch_calls == [("AjayvirS", "PRahari", 14)]
    assert client.auth_calls == 1
    assert client.files_calls == [("AjayvirS", "PRahari", 14)]
    assert len(client.comment_calls) == 1
    assert client.comment_calls[0][0:3] == ("AjayvirS", "PRahari", 14)
    assert "PRahari review summary" in client.comment_calls[0][3]
    assert "Summary" in client.comment_calls[0][3]
    assert "Potential findings" in client.comment_calls[0][3]
    assert "Open questions" in client.comment_calls[0][3]
    assert build_review_comment_marker("abc123") in client.comment_calls[0][3]


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


@pytest.mark.asyncio
async def test_worker_skips_generation_when_duplicate_review_comment_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))
    job, _ = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=18,
        head_sha="already-reviewed",
    )
    client = FakeGitHubClient(
        existing_comments=[
            {
                "body": (
                    "PRahari review summary\n\n"
                    f"{build_review_comment_marker('already-reviewed')}"
                ),
                "user": {"login": "prahari-bot"},
            }
        ]
    )

    async def broken_review(*args: object, **kwargs: object) -> str:
        raise AssertionError("review generation should be skipped for duplicates")

    monkeypatch.setattr("app.business.worker.build_review_comment", broken_review)

    processed = await process_next_job(
        repository=repository,
        client=client,
        identity_provider=ReviewerIdentityProvider(),
    )

    assert processed is not None
    assert processed.job_id == job.job_id
    assert processed.status == COMPLETED_STATUS
    assert client.fetch_calls == [("AjayvirS", "PRahari", 18)]
    assert client.comments_fetch_calls == [("AjayvirS", "PRahari", 18)]
    assert client.auth_calls == 1
    assert client.files_calls == []
    assert client.comment_calls == []


@pytest.mark.asyncio
async def test_worker_does_not_skip_when_marker_matches_but_login_does_not(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))
    repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=19,
        head_sha="mismatch-sha",
    )
    client = FakeGitHubClient(
        existing_comments=[
            {
                "body": (
                    "PRahari review summary\n\n"
                    f"{build_review_comment_marker('mismatch-sha')}"
                ),
                "user": {"login": "some-other-user"},
            }
        ]
    )

    processed = await process_next_job(repository=repository, client=client)

    assert processed is not None
    assert processed.status == COMPLETED_STATUS
    assert client.files_calls == [("AjayvirS", "PRahari", 19)]
    assert len(client.comment_calls) == 1


@pytest.mark.asyncio
async def test_worker_uses_placeholder_comment_when_review_generation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))
    repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=17,
        head_sha="fallback123",
    )
    client = FakeGitHubClient()

    def broken_review(*args: object, **kwargs: object) -> str:
        raise RuntimeError("review generation failed")

    monkeypatch.setattr(reviewer, "_build_structured_review_sections", broken_review)

    processed = await process_next_job(repository=repository, client=client)

    assert processed is not None
    assert processed.status == COMPLETED_STATUS
    assert len(client.comment_calls) == 1
    assert client.comment_calls[0][3] == (
        "Review pipeline connected successfully for this PR head SHA fallback123\n\n"
        "<!-- prahari:review head_sha=fallback123 -->"
    )
