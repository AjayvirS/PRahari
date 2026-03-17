"""Background worker that claims durable review jobs and posts PR comments."""
from __future__ import annotations

import asyncio

from app.config import settings
from app.github_client import GitHubClient, github_client
from app.logging_config import get_logger
from app.review_jobs import ReviewJob, ReviewJobRepository
from app.reviewer import build_review_comment

logger = get_logger(__name__)


async def process_review_job(
    job: ReviewJob,
    *,
    repository: ReviewJobRepository | None = None,
    client: GitHubClient | None = None,
) -> ReviewJob:
    """Fetch the PR and post a review summary comment for a claimed review job."""
    review_jobs = repository or ReviewJobRepository()
    github = client or github_client
    owner, repo_name = _split_repo(job.repo)

    logger.info(
        "worker.process_job.start",
        job_id=job.job_id,
        repo=job.repo,
        pr_number=job.pr_number,
        head_sha=job.head_sha,
    )

    try:
        pull_request = await github.get_pull_request(owner, repo_name, job.pr_number)
        changed_files = await github.list_pull_request_files(owner, repo_name, job.pr_number)
        comment_body = build_review_comment(
            pull_request,
            changed_files,
            head_sha=job.head_sha,
        )
        await github.post_issue_comment(
            owner,
            repo_name,
            job.pr_number,
            comment_body,
        )
        completed_job = review_jobs.mark_job_completed(job.job_id)
        logger.info(
            "worker.process_job.completed",
            job_id=job.job_id,
            repo=job.repo,
            pr_number=job.pr_number,
        )
        return completed_job
    except Exception as exc:
        failed_job = review_jobs.mark_job_failed(job.job_id, str(exc))
        logger.exception(
            "worker.process_job.failed",
            job_id=job.job_id,
            repo=job.repo,
            pr_number=job.pr_number,
        )
        return failed_job


async def process_next_job(
    *,
    repository: ReviewJobRepository | None = None,
    client: GitHubClient | None = None,
) -> ReviewJob | None:
    """Claim and process a single pending review job, if one exists."""
    review_jobs = repository or ReviewJobRepository()
    job = review_jobs.claim_next_pending_job()
    if job is None:
        return None

    return await process_review_job(job, repository=review_jobs, client=client)


async def run_worker(
    *,
    repository: ReviewJobRepository | None = None,
    client: GitHubClient | None = None,
) -> None:
    """Poll the database for pending review jobs and process them serially."""
    logger.info("worker.start", poll_interval=settings.worker_poll_interval)

    try:
        while True:
            processed_job = await process_next_job(repository=repository, client=client)
            if processed_job is None:
                await asyncio.sleep(settings.worker_poll_interval)
    except asyncio.CancelledError:
        logger.info("worker.stopped")
        raise
    except Exception:
        logger.exception("worker.unexpected_error")
        await asyncio.sleep(settings.worker_poll_interval)


def _split_repo(full_name: str) -> tuple[str, str]:
    owner, repo_name = full_name.split("/", maxsplit=1)
    return owner, repo_name

