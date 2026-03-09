"""In-memory asyncio queue used to pass PR events from the webhook to the worker.

This module intentionally exposes a thin interface so that the backing store
(e.g. Redis, RabbitMQ) can be swapped out without touching the rest of the
application.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.logging_config import get_logger

logger = get_logger(__name__)

# Module-level queue instance shared by the webhook receiver and the worker.
_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()


def get_queue() -> asyncio.Queue[dict[str, Any]]:
    """Return the shared event queue."""
    return _queue


async def enqueue(event: dict[str, Any]) -> None:
    """Put a PR event onto the queue."""
    await _queue.put(event)
    logger.info("event.enqueued", queue_size=_queue.qsize())


async def dequeue() -> dict[str, Any]:
    """Block until an event is available and return it."""
    event = await _queue.get()
    logger.info("event.dequeued", queue_size=_queue.qsize())
    return event


def task_done() -> None:
    """Signal that the last dequeued event has been processed."""
    _queue.task_done()
