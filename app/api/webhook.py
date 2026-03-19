"""Webhook receiver endpoint."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.business.enqueue import enqueue_pull_request_event
from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter()
SUPPORTED_PR_ACTIONS = {"opened", "synchronize", "reopened"}


def _verify_signature(payload: bytes, signature_header: str | None) -> None:
    """Verify the GitHub webhook HMAC-SHA256 signature."""
    secret = settings.github_webhook_secret
    if not secret:
        return

    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Hub-Signature-256 header",
        )

    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )


def _parse_webhook_metadata(
    github_event: str | None,
    delivery_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Extract normalized metadata from a GitHub webhook payload."""
    action = payload.get("action")
    repository = payload.get("repository") or {}
    pull_request = payload.get("pull_request") or {}
    head = pull_request.get("head") or {}
    pr_number = payload.get("number") or pull_request.get("number")

    return {
        "delivery_id": delivery_id,
        "event_type": github_event,
        "action": action,
        "repo": repository.get("full_name"),
        "pr_number": pr_number,
        "head_sha": head.get("sha"),
        "supported": github_event == "pull_request" and action in SUPPORTED_PR_ACTIONS,
    }


@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, Any]:
    """Receive a GitHub webhook event and accept supported PR events."""
    payload_bytes = await request.body()
    _verify_signature(payload_bytes, x_hub_signature_256)

    payload: dict[str, Any] = await request.json()
    metadata = _parse_webhook_metadata(
        github_event=x_github_event,
        delivery_id=x_github_delivery,
        payload=payload,
    )

    logger.info(
        "webhook.received",
        delivery_id=metadata["delivery_id"],
        github_event=metadata["event_type"],
        action=metadata["action"],
        repo=metadata["repo"],
        pr_number=metadata["pr_number"],
        head_sha=metadata["head_sha"],
        supported=metadata["supported"],
    )

    return enqueue_pull_request_event(metadata)
