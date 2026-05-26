"""Operational endpoints for debugging queues and workers."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from app.business.worker import get_worker_state
from app.config import settings
from app.database.review_jobs import ReviewJobRepository

router = APIRouter()


@router.get("/ops/jobs")
async def list_jobs(limit: int = 20) -> dict:
    """Return recent review jobs and counts by status."""
    repository = ReviewJobRepository()
    recent = repository.list_recent_jobs(limit=limit)
    counts = repository.count_jobs_by_status()
    db_path = str(Path(settings.database_path).resolve())

    return {
        "counts": counts,
        "database_path": db_path,
        "worker": get_worker_state(),
        "jobs": [
            {
                "job_id": job.job_id,
                "status": job.status,
                "repo": job.repo,
                "pr_number": job.pr_number,
                "head_sha": job.head_sha,
                "retry_count": job.retry_count,
                "last_error": job.last_error,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
            for job in recent
        ],
    }
