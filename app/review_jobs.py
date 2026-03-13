"""Review job repository and data model."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from app.database import get_connection

REVIEW_JOB_TYPE = "review_pr"
PENDING_STATUS = "pending"


@dataclass(slots=True)
class ReviewJob:
    """Durable review job record used by the worker pipeline."""

    job_id: str
    job_type: str
    status: str
    repo: str
    pr_number: int
    head_sha: str
    retry_count: int
    max_retries: int
    last_error: str | None
    created_at: str
    updated_at: str
    claimed_at: str | None
    completed_at: str | None
    failed_at: str | None


class ReviewJobRepository:
    """Data-access layer for creating and reading review jobs."""

    def __init__(self, database_path: str | None = None) -> None:
        self._database_path = database_path

    def insert_review_job(
        self,
        repo: str,
        pr_number: int,
        head_sha: str,
        *,
        job_type: str = REVIEW_JOB_TYPE,
        status: str = PENDING_STATUS,
        max_retries: int = 3,
    ) -> tuple[ReviewJob, bool]:
        """Insert a review job or return the existing one on dedup."""
        job_id = str(uuid.uuid4())

        with get_connection(self._database_path) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO review_jobs (
                    job_id,
                    job_type,
                    status,
                    repo,
                    pr_number,
                    head_sha,
                    max_retries
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, job_type, status, repo, pr_number, head_sha, max_retries),
            )
            connection.commit()

            if cursor.rowcount == 1:
                return self.get_job(job_id), True

            existing = connection.execute(
                """
                SELECT *
                FROM review_jobs
                WHERE job_type = ? AND repo = ? AND pr_number = ? AND head_sha = ?
                """,
                (job_type, repo, pr_number, head_sha),
            ).fetchone()
            return _row_to_review_job(existing), False

    def get_job(self, job_id: str) -> ReviewJob:
        """Load a single review job by primary key."""
        with get_connection(self._database_path) as connection:
            row = connection.execute(
                "SELECT * FROM review_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()

        if row is None:
            raise LookupError(f"Review job {job_id} was not found")

        return _row_to_review_job(row)

    def list_jobs(self) -> list[ReviewJob]:
        """List all review jobs ordered by creation time."""
        with get_connection(self._database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM review_jobs ORDER BY created_at ASC, job_id ASC"
            ).fetchall()

        return [_row_to_review_job(row) for row in rows]


def _row_to_review_job(row: sqlite3.Row | None) -> ReviewJob:
    if row is None:
        raise LookupError("Review job row was not found")

    return ReviewJob(
        job_id=row["job_id"],
        job_type=row["job_type"],
        status=row["status"],
        repo=row["repo"],
        pr_number=row["pr_number"],
        head_sha=row["head_sha"],
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        claimed_at=row["claimed_at"],
        completed_at=row["completed_at"],
        failed_at=row["failed_at"],
    )

