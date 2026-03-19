"""Tests for durable review job storage."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.database.connection import initialize_database
from app.database.review_jobs import (
    COMPLETED_STATUS,
    FAILED_STATUS,
    PENDING_STATUS,
    PROCESSING_STATUS,
    REVIEW_JOB_TYPE,
    ReviewJobRepository,
)


def test_initialize_database_creates_review_job_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))

    assert database_path.exists()

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(review_jobs)").fetchall()
        }
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(review_jobs)").fetchall()
        }

    assert {
        "job_id",
        "job_type",
        "status",
        "repo",
        "pr_number",
        "head_sha",
        "retry_count",
        "max_retries",
        "created_at",
        "updated_at",
        "claimed_at",
        "completed_at",
        "failed_at",
    }.issubset(columns)
    assert "idx_review_jobs_dedup" in indexes


def test_inserted_review_jobs_can_be_queried(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))

    job, created = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=7,
        head_sha="abc123",
    )

    assert created is True
    assert job.job_type == REVIEW_JOB_TYPE
    assert job.status == PENDING_STATUS
    assert job.repo == "AjayvirS/PRahari"
    assert job.pr_number == 7
    assert job.head_sha == "abc123"

    loaded = repository.get_job(job.job_id)
    listed = repository.list_jobs()

    assert loaded == job
    assert listed == [job]


def test_duplicate_review_jobs_are_ignored_for_same_head_sha(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))

    first_job, first_created = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=11,
        head_sha="deadbeef",
    )
    second_job, second_created = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=11,
        head_sha="deadbeef",
    )

    assert first_created is True
    assert second_created is False
    assert second_job == first_job
    assert len(repository.list_jobs()) == 1


def test_new_head_sha_creates_a_new_review_job(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))

    first_job, first_created = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=11,
        head_sha="deadbeef",
    )
    second_job, second_created = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=11,
        head_sha="cafebabe",
    )

    assert first_created is True
    assert second_created is True
    assert second_job.job_id != first_job.job_id
    assert len(repository.list_jobs()) == 2


def test_claim_next_pending_job_marks_it_processing(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))

    created_job, _ = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=22,
        head_sha="claimme",
    )

    claimed = repository.claim_next_pending_job()

    assert claimed is not None
    assert claimed.job_id == created_job.job_id
    assert claimed.status == PROCESSING_STATUS
    assert claimed.claimed_at is not None


def test_completed_and_failed_jobs_are_marked_terminally(tmp_path: Path) -> None:
    database_path = tmp_path / "review-jobs.db"
    initialize_database(str(database_path))
    repository = ReviewJobRepository(str(database_path))

    completed_seed, _ = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=30,
        head_sha="complete",
    )
    claimed_for_complete = repository.claim_next_pending_job()
    assert claimed_for_complete is not None
    assert claimed_for_complete.job_id == completed_seed.job_id

    completed = repository.mark_job_completed(claimed_for_complete.job_id)
    assert completed.status == COMPLETED_STATUS
    assert completed.completed_at is not None
    assert completed.last_error is None

    failed_seed, _ = repository.insert_review_job(
        repo="AjayvirS/PRahari",
        pr_number=31,
        head_sha="fail",
    )
    claimed_for_fail = repository.claim_next_pending_job()
    assert claimed_for_fail is not None
    assert claimed_for_fail.job_id == failed_seed.job_id

    failed = repository.mark_job_failed(claimed_for_fail.job_id, "boom")
    assert failed.status == FAILED_STATUS
    assert failed.failed_at is not None
    assert failed.last_error == "boom"
    assert failed.retry_count == 1
