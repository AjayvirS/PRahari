"""Reviewer identity resolution used for duplicate comment checks."""
from __future__ import annotations

from dataclasses import dataclass

from app.logging_config import get_logger
from app.services.github_client import Client

logger = get_logger(__name__)


@dataclass(slots=True)
class ReviewerIdentity:
    """Identity metadata for the account that posts review comments."""

    login: str


class ReviewerIdentityProvider:
    """Resolve the reviewer identity from the configured GitHub token."""

    def __init__(self) -> None:
        self._cached_identity: ReviewerIdentity | None = None

    async def get_identity(self, client: Client) -> ReviewerIdentity | None:
        """Return the reviewer identity, or None when it cannot be resolved."""
        if self._cached_identity is not None:
            return self._cached_identity

        try:
            user = await client.get_authenticated_user()
        except Exception:
            logger.exception("reviewer_identity.resolve.failed")
            return None

        login = str(user.get("login") or "").strip()
        if not login:
            logger.warning("reviewer_identity.resolve.missing_login")
            return None

        self._cached_identity = ReviewerIdentity(login=login)
        return self._cached_identity
