"""Background worker that consumes PR events from the queue and runs reviews."""
from __future__ import annotations

import asyncio
from typing import Any

from app import queue as q
from app.config import settings
from app.logging_config import get_logger
from app.reviewer import review_pull_request

logger = get_logger(__name__)


async def process_event(event: dict[str, Any]) -> None:
    """Process a single PR event.

    Args:
        event: The dequeued PR event payload.
    """
    logger.info("worker.process_event.start", event_action=event.get("action"))
    try:
        result = await review_pull_request(event)
        logger.info("worker.process_event.done", result=result)
    except Exception:
        logger.exception("worker.process_event.error")
    finally:
        q.task_done()


async def run_worker() -> None:
    """Continuously dequeue and process PR events.

    This is the main worker loop.  It runs indefinitely and is intended to be
    started as an asyncio task alongside the FastAPI application.
    """
    logger.info(
        "worker.start",
        poll_interval=settings.worker_poll_interval,
    )
    while True:
        try:
            event = await q.dequeue()
            asyncio.create_task(process_event(event))
        except asyncio.CancelledError:
            logger.info("worker.stopped")
            break
        except Exception:
            logger.exception("worker.unexpected_error")
            await asyncio.sleep(settings.worker_poll_interval)
