"""Webhook receiver endpoint."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.business.enqueue import enqueue_pull_request_event
from app.config import settings
from app.logging_config import get_logger
from app.services.github_client import github_client

logger = get_logger(__name__)

router = APIRouter()
SUPPORTED_PR_ACTIONS = {"opened", "synchronize", "reopened"}
SUPPORTED_COMMENT_ACTIONS = {"created"}


def _verify_signature(
    payload: bytes,
    signature_header: str | None,
    *,
    delivery_id: str | None = None,
    github_event: str | None = None,
) -> None:
    """Verify the GitHub webhook HMAC-SHA256 signature."""
    secret = settings.github_webhook_secret
    if not secret:
        return

    if not signature_header:
        logger.warning(
            "webhook.signature_missing",
            delivery_id=delivery_id,
            github_event=github_event,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Hub-Signature-256 header",
        )

    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        logger.warning(
            "webhook.signature_invalid",
            delivery_id=delivery_id,
            github_event=github_event,
        )
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
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    pr_number = payload.get("number") or pull_request.get("number") or issue.get("number")
    is_pull_request_issue = bool(issue.get("pull_request"))

    supported_pr_event = github_event == "pull_request" and action in SUPPORTED_PR_ACTIONS
    supported_comment_event = (
        github_event == "issue_comment"
        and action in SUPPORTED_COMMENT_ACTIONS
        and is_pull_request_issue
    )

    return {
        "delivery_id": delivery_id,
        "event_type": github_event,
        "action": action,
        "repo": repository.get("full_name"),
        "pr_number": pr_number,
        "head_sha": head.get("sha"),
        "comment_id": comment.get("id"),
        "comment_body": comment.get("body"),
        "comment_author": (comment.get("user") or {}).get("login"),
        "comment_type": "issue_comment" if github_event == "issue_comment" else None,
        "in_reply_to_id": comment.get("in_reply_to_id"),
        "supported": supported_pr_event or supported_comment_event,
    }


async def _resolve_bot_login() -> str:
    configured_login = settings.github_app_user.strip()
    if configured_login:
        return configured_login

    try:
        user = await github_client.get_authenticated_user()
    except Exception:
        logger.exception("webhook.bot_login_lookup_failed")
        return ""

    return str(user.get("login") or "").strip()


async def _is_reply_to_bot_comment(
    *,
    repo: str | None,
    pr_number: int | None,
    in_reply_to_id: str | None,
    bot_login: str,
) -> bool:
    if not repo or not pr_number or not in_reply_to_id or not bot_login:
        return False

    owner, repo_name = repo.split("/", maxsplit=1)
    comments = await github_client.get_issue_comments(owner, repo_name, pr_number)
    for comment in comments:
        if str(comment.get("id") or "") != str(in_reply_to_id):
            continue
        author = str((comment.get("user") or {}).get("login") or "")
        return author.lower() == bot_login.lower()
    return False


@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, Any]:
    """Receive a GitHub webhook event and accept supported PR events."""
    payload_bytes = await request.body()
    _verify_signature(
        payload_bytes,
        x_hub_signature_256,
        delivery_id=x_github_delivery,
        github_event=x_github_event,
    )

    payload: dict[str, Any] = await request.json()
    metadata = _parse_webhook_metadata(
        github_event=x_github_event,
        delivery_id=x_github_delivery,
        payload=payload,
    )

    if metadata.get("event_type") == "issue_comment":
        in_reply_to_id = str(metadata.get("in_reply_to_id") or "").strip()
        if not in_reply_to_id:
            metadata["supported"] = False
        else:
            bot_login = await _resolve_bot_login()
            metadata["bot_login"] = bot_login
            metadata["reply_is_bot"] = await _is_reply_to_bot_comment(
                repo=metadata.get("repo"),
                pr_number=metadata.get("pr_number"),
                in_reply_to_id=in_reply_to_id,
                bot_login=bot_login,
            )
            metadata["supported"] = metadata.get("supported") and metadata["reply_is_bot"]

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
