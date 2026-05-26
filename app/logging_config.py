"""Structured logging configuration using structlog."""
from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structlog for readable structured logging."""
    normalized_level = (log_level or "INFO").strip().strip("\"").strip("'")
    level = getattr(logging, normalized_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
        level=level,
    )
    logging.getLogger().setLevel(level)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("watchfiles.main").setLevel(logging.WARNING)
    logging.getLogger("watchfiles.watcher").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.KeyValueRenderer(key_order=["event", "level", "timestamp"]),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
