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

    logger.debug(
        "enqueue.received",
        delivery_id=metadata.get("delivery_id"),
        github_event=metadata.get("event_type"),
        action=metadata.get("action"),
        repo=metadata.get("repo"),
        pr_number=metadata.get("pr_number"),
        head_sha=metadata.get("head_sha"),
        supported=metadata.get("supported"),
    )

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

    if metadata.get("event_type") == "issue_comment":
        comment_author = str(metadata.get("comment_author") or "").strip()
        comment_id = str(metadata.get("comment_id") or "").strip()
        in_reply_to_id = str(metadata.get("in_reply_to_id") or "").strip()
        bot_login = str(metadata.get("bot_login") or "").strip()
        if not comment_id:
            logger.info(
                "enqueue.comment_reply_missing_id",
                delivery_id=metadata.get("delivery_id"),
                repo=metadata.get("repo"),
                pr_number=metadata.get("pr_number"),
            )
            return {"status": "ignored"}
        if not in_reply_to_id:
            logger.info(
                "enqueue.comment_reply_missing_parent",
                delivery_id=metadata.get("delivery_id"),
                repo=metadata.get("repo"),
                pr_number=metadata.get("pr_number"),
            )
            return {"status": "ignored"}
        if not metadata.get("reply_is_bot"):
            logger.info(
                "enqueue.comment_reply_not_bot_reply",
                delivery_id=metadata.get("delivery_id"),
                repo=metadata.get("repo"),
                pr_number=metadata.get("pr_number"),
            )
            return {"status": "ignored"}
        if bot_login and comment_author.lower() == bot_login.lower():
            logger.info(
                "enqueue.comment_reply_ignored_self",
                delivery_id=metadata.get("delivery_id"),
                repo=metadata.get("repo"),
                pr_number=metadata.get("pr_number"),
            )
            return {"status": "ignored"}

        job, created = review_jobs.insert_comment_reply_job(
            repo=metadata["repo"],
            pr_number=metadata["pr_number"],
            comment_id=comment_id,
            comment_body=str(metadata.get("comment_body") or ""),
            comment_author=comment_author,
            comment_type=str(metadata.get("comment_type") or "issue_comment"),
            in_reply_to_id=in_reply_to_id,
        )

        logger.info(
            "enqueue.comment_reply_created" if created else "enqueue.comment_reply_duplicate",
            delivery_id=metadata.get("delivery_id"),
            repo=job.repo,
            pr_number=job.pr_number,
            comment_id=job.comment_id,
            job_id=job.job_id,
            action=metadata.get("action"),
        )
        return {"status": "queued" if created else "duplicate", "job_id": job.job_id}

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
