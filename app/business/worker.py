"""Background worker that claims durable review jobs and posts PR comments."""
from __future__ import annotations

import asyncio

from app.config import settings
from app.database.review_jobs import (
    REPLY_COMMENT_JOB_TYPE,
    ReviewJob,
    ReviewJobRepository,
)
from app.logging_config import get_logger
from app.services.github_client import Client, github_client

from .reviewer import build_review_comment, build_review_reply_comment, comment_reviews_head_sha
from .reviewer_identity import ReviewerIdentityProvider

logger = get_logger(__name__)


async def process_review_job(
    job: ReviewJob,
    *,
    repository: ReviewJobRepository | None = None,
    client: Client | None = None,
    identity_provider: ReviewerIdentityProvider | None = None,
) -> ReviewJob:
    """Fetch the PR and post a review summary comment for a claimed review job."""
    review_jobs = repository or ReviewJobRepository()
    github = client or github_client
    reviewer_identity = identity_provider or ReviewerIdentityProvider()
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
        existing_comments = await github.get_issue_comments(owner, repo_name, job.pr_number)
        identity = await reviewer_identity.get_identity(github)
        if identity is None:
            logger.warning(
                "worker.process_job.duplicate_check_identity_unavailable",
                job_id=job.job_id,
                repo=job.repo,
                pr_number=job.pr_number,
                head_sha=job.head_sha,
            )
        elif _has_existing_review_for_sha(
            existing_comments,
            reviewer_login=identity.login,
            head_sha=job.head_sha,
        ):
            completed_job = review_jobs.mark_job_completed(job.job_id)
            logger.info(
                "worker.process_job.skipped_duplicate_review",
                job_id=job.job_id,
                repo=job.repo,
                pr_number=job.pr_number,
                head_sha=job.head_sha,
                reviewer_login=identity.login,
            )
            return completed_job

        changed_files = await github.list_pull_request_files(owner, repo_name, job.pr_number)
        repo_prompt_template = await github.get_repository_file_content(
            owner,
            repo_name,
            settings.review_prompt_file_path,
        )
        comment_body = await build_review_comment(
            pull_request,
            [file["filename"] for file in changed_files],
            head_sha=job.head_sha,
            repo_prompt_template=repo_prompt_template,
        )
        await github.post_issue_comment(owner, repo_name, job.pr_number, comment_body)
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


async def process_comment_reply_job(
    job: ReviewJob,
    *,
    repository: ReviewJobRepository | None = None,
    client: Client | None = None,
    identity_provider: ReviewerIdentityProvider | None = None,
) -> ReviewJob:
    """Fetch the PR and reply to a follow-up comment."""
    review_jobs = repository or ReviewJobRepository()
    github = client or github_client
    reviewer_identity = identity_provider or ReviewerIdentityProvider()
    owner, repo_name = _split_repo(job.repo)

    logger.info(
        "worker.process_reply.start",
        job_id=job.job_id,
        repo=job.repo,
        pr_number=job.pr_number,
        comment_id=job.comment_id,
    )

    try:
        pull_request = await github.get_pull_request(owner, repo_name, job.pr_number)
        user_comment = (job.comment_body or "").strip()
        if not user_comment:
            skipped_job = review_jobs.mark_job_completed(job.job_id)
            logger.info(
                "worker.process_reply.skipped_missing_comment",
                job_id=job.job_id,
                repo=job.repo,
                pr_number=job.pr_number,
                comment_id=job.comment_id,
            )
            return skipped_job

        identity = await reviewer_identity.get_identity(github)
        if identity and (job.comment_author or "").lower() == identity.login.lower():
            skipped_job = review_jobs.mark_job_completed(job.job_id)
            logger.info(
                "worker.process_reply.skipped_self_reply",
                job_id=job.job_id,
                repo=job.repo,
                pr_number=job.pr_number,
                comment_id=job.comment_id,
            )
            return skipped_job

        existing_comments = await github.get_issue_comments(owner, repo_name, job.pr_number)
        review_comment_body = _find_latest_review_comment(
            existing_comments,
            pull_request.head_sha,
            in_reply_to_id=job.in_reply_to_id,
        )
        if not review_comment_body:
            skipped_job = review_jobs.mark_job_completed(job.job_id)
            logger.info(
                "worker.process_reply.skipped_missing_review_comment",
                job_id=job.job_id,
                repo=job.repo,
                pr_number=job.pr_number,
                comment_id=job.comment_id,
            )
            return skipped_job

        reply_body = await build_review_reply_comment(
            pull_request,
            review_comment=review_comment_body,
            user_comment=user_comment,
        )
        await github.post_issue_comment(owner, repo_name, job.pr_number, reply_body)
        completed_job = review_jobs.mark_job_completed(job.job_id)
        logger.info(
            "worker.process_reply.completed",
            job_id=job.job_id,
            repo=job.repo,
            pr_number=job.pr_number,
            comment_id=job.comment_id,
        )
        return completed_job
    except Exception as exc:
        failed_job = review_jobs.mark_job_failed(job.job_id, str(exc))
        logger.exception(
            "worker.process_reply.failed",
            job_id=job.job_id,
            repo=job.repo,
            pr_number=job.pr_number,
            comment_id=job.comment_id,
        )
        return failed_job


async def process_next_job(
    *,
    repository: ReviewJobRepository | None = None,
    client: Client | None = None,
    identity_provider: ReviewerIdentityProvider | None = None,
) -> ReviewJob | None:
    """Claim and process a single pending review job, if one exists."""
    review_jobs = repository or ReviewJobRepository()
    job = review_jobs.claim_next_pending_job()
    if job is None:
        return None

    if job.job_type == REPLY_COMMENT_JOB_TYPE:
        return await process_comment_reply_job(
            job,
            repository=review_jobs,
            client=client,
            identity_provider=identity_provider,
        )

    return await process_review_job(
        job,
        repository=review_jobs,
        client=client,
        identity_provider=identity_provider,
    )


async def run_worker(
    *,
    repository: ReviewJobRepository | None = None,
    client: Client | None = None,
    identity_provider: ReviewerIdentityProvider | None = None,
) -> None:
    """Poll the database for pending review jobs and process them."""
    logger.info(
        "worker.start",
        poll_interval=settings.worker_poll_interval,
        concurrency=settings.worker_concurrency,
    )
    review_jobs = repository or ReviewJobRepository()

    try:
        while True:
            job = await process_next_job(
                repository=review_jobs,
                client=client,
                identity_provider=identity_provider,
            )
            if job is None:
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


def _has_existing_review_for_sha(
    comments: list[dict],
    *,
    reviewer_login: str,
    head_sha: str,
) -> bool:
    for comment in comments:
        user = comment.get("user") or {}
        if str(user.get("login") or "") != reviewer_login:
            continue

        body = str(comment.get("body") or "")
        if comment_reviews_head_sha(body, head_sha):
            return True

    return False


def _find_latest_review_comment(
    comments: list[dict], head_sha: str, *, in_reply_to_id: str | None = None
) -> str | None:
    if in_reply_to_id:
        for comment in comments:
            if str(comment.get("id") or "") != str(in_reply_to_id):
                continue
            body = str(comment.get("body") or "")
            if body:
                return body

    for comment in reversed(comments):
        body = str(comment.get("body") or "")
        if comment_reviews_head_sha(body, head_sha):
            return body
    return None

