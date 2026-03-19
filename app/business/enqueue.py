"""Business logic for turning webhook payloads into durable review jobs."""
from __future__ import annotations

from typing import Any

from app.database.review_jobs import ReviewJobRepository
from app.logging_config import get_logger

logger = get_logger(__name__)


def enqueue_pull_request_event(
    metadata: dict[str, Any],
    repository: ReviewJobRepository | None = None,
) -> dict[str, Any]:
    """Create a durable review job for a supported pull request event."""
    review_jobs = repository or ReviewJobRepository()

    if not metadata.get("supported"):
        logger.info(
            "enqueue.unsupported_event",
            delivery_id=metadata.get("delivery_id"),
            github_event=metadata.get("event_type"),
            action=metadata.get("action"),
            repo=metadata.get("repo"),
            pr_number=metadata.get("pr_number"),
        )
        return {"status": "ignored"}

    job, created = review_jobs.insert_review_job(
        repo=metadata["repo"],
        pr_number=metadata["pr_number"],
        head_sha=metadata["head_sha"],
    )

    logger.info(
        "enqueue.job_created" if created else "enqueue.duplicate_ignored",
        delivery_id=metadata.get("delivery_id"),
        repo=job.repo,
        pr_number=job.pr_number,
        head_sha=job.head_sha,
        job_id=job.job_id,
        action=metadata.get("action"),
    )
    return {"status": "queued" if created else "duplicate", "job_id": job.job_id}
