"""Tests for the webhook receiver endpoint."""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient

import app.webhook as wh
from app.main import app


def _make_signature(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_webhook_ignores_non_pr_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wh.settings, "github_webhook_secret", "")
    enqueue_mock = Mock(return_value={"status": "ignored"})
    monkeypatch.setattr(wh, "enqueue_pull_request_event", enqueue_mock)

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
    enqueue_mock.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_queues_pr_opened_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wh.settings, "github_webhook_secret", "")
    payload = {
        "action": "opened",
        "number": 42,
        "repository": {"full_name": "org/repo"},
        "pull_request": {"head": {"sha": "abc123"}},
    }
    enqueue_mock = Mock(return_value={"status": "queued", "job_id": "job-1"})
    log_calls: list[tuple[str, dict[str, object]]] = []
    original_info = wh.logger.info

    def capture_log(event: str, **kwargs: object) -> None:
        log_calls.append((event, kwargs))
        original_info(event, **kwargs)

    monkeypatch.setattr(wh, "enqueue_pull_request_event", enqueue_mock)
    monkeypatch.setattr(wh.logger, "info", capture_log)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-123",
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    enqueue_mock.assert_called_once_with(
        {
            "delivery_id": "delivery-123",
            "event_type": "pull_request",
            "action": "opened",
            "repo": "org/repo",
            "pr_number": 42,
            "head_sha": "abc123",
            "supported": True,
        }
    )
    assert ("webhook.received", {
        "delivery_id": "delivery-123",
        "github_event": "pull_request",
        "action": "opened",
        "repo": "org/repo",
        "pr_number": 42,
        "head_sha": "abc123",
        "supported": True,
    }) in log_calls


@pytest.mark.asyncio
async def test_webhook_queues_pr_synchronize_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wh.settings, "github_webhook_secret", "")
    payload = {
        "action": "synchronize",
        "number": 7,
        "repository": {"full_name": "org/repo"},
        "pull_request": {"head": {"sha": "def456"}},
    }
    enqueue_mock = Mock(return_value={"status": "queued", "job_id": "job-2"})
    monkeypatch.setattr(wh, "enqueue_pull_request_event", enqueue_mock)

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
    enqueue_mock.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_queues_pr_reopened_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wh.settings, "github_webhook_secret", "")
    payload = {
        "action": "reopened",
        "number": 9,
        "repository": {"full_name": "org/repo"},
        "pull_request": {"head": {"sha": "ghi789"}},
    }
    enqueue_mock = Mock(return_value={"status": "queued", "job_id": "job-3"})
    monkeypatch.setattr(wh, "enqueue_pull_request_event", enqueue_mock)

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
    enqueue_mock.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_ignores_pr_closed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wh.settings, "github_webhook_secret", "")
    payload = {"action": "closed", "number": 1, "repository": {"full_name": "org/repo"}}
    enqueue_mock = Mock(return_value={"status": "ignored"})
    monkeypatch.setattr(wh, "enqueue_pull_request_event", enqueue_mock)

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
    enqueue_mock.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_accepts_valid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "supersecret"
    monkeypatch.setattr(wh.settings, "github_webhook_secret", secret)

    payload = {
        "action": "opened",
        "number": 10,
        "repository": {"full_name": "org/repo"},
        "pull_request": {"head": {"sha": "valid123"}},
    }
    payload_bytes = json.dumps(payload).encode()
    signature = _make_signature(payload_bytes, secret)
    enqueue_mock = Mock(return_value={"status": "queued", "job_id": "job-4"})
    monkeypatch.setattr(wh, "enqueue_pull_request_event", enqueue_mock)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhook",
            content=payload_bytes,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    enqueue_mock.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wh.settings, "github_webhook_secret", "supersecret")

    payload = json.dumps({"action": "opened"}).encode()
    enqueue_mock = Mock(return_value={"status": "queued"})
    monkeypatch.setattr(wh, "enqueue_pull_request_event", enqueue_mock)

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
    enqueue_mock.assert_not_called()
