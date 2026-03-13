"""Tests for durable review job storage."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.database import initialize_database
from app.review_jobs import REVIEW_JOB_TYPE, PENDING_STATUS, ReviewJobRepository


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

