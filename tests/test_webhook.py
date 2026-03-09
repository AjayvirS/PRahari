"""Tests for the webhook receiver endpoint."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


def _make_signature(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_webhook_ignores_non_pr_event() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhook",
            json={"action": "created"},
            headers={"X-GitHub-Event": "push"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_queues_pr_opened_event() -> None:
    payload = {"action": "opened", "number": 42, "repository": {"full_name": "org/repo"}}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhook",
            json=payload,
            headers={"X-GitHub-Event": "pull_request"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_webhook_queues_pr_synchronize_event() -> None:
    payload = {"action": "synchronize", "number": 7, "repository": {"full_name": "org/repo"}}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhook",
            json=payload,
            headers={"X-GitHub-Event": "pull_request"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_webhook_ignores_pr_closed_event() -> None:
    payload = {"action": "closed", "number": 1, "repository": {"full_name": "org/repo"}}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhook",
            json=payload,
            headers={"X-GitHub-Event": "pull_request"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "supersecret")

    # Re-import settings so the env var is picked up.
    import importlib

    import app.config as cfg
    import app.webhook as wh

    importlib.reload(cfg)
    importlib.reload(wh)

    payload = json.dumps({"action": "opened"}).encode()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhook",
            content=payload,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": "sha256=invalidsignature",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 401
