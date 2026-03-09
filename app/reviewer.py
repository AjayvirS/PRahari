"""Placeholder reviewer module.

The review logic (e.g. LLM-based analysis) will live here once the
scaffolding is in place.  For now it simply acknowledges the event and
returns a stub result.
"""
from __future__ import annotations

from typing import Any

from app.logging_config import get_logger

logger = get_logger(__name__)


async def review_pull_request(event: dict[str, Any]) -> dict[str, Any]:
    """Analyse a pull request event and return review results.

    This is a **placeholder** implementation.  It logs the incoming event and
    returns a stub result without performing any real analysis.

    Args:
        event: The PR event payload as received from the webhook.

    Returns:
        A dictionary containing the (stub) review outcome.
    """
    pr_number = event.get("number")
    repo = event.get("repository", {}).get("full_name", "unknown")

    logger.info(
        "reviewer.review_pull_request.stub",
        repo=repo,
        pr_number=pr_number,
        message="LLM review logic not yet implemented",
    )

    return {
        "status": "pending",
        "repo": repo,
        "pr_number": pr_number,
        "message": "Review logic not yet implemented.",
    }
