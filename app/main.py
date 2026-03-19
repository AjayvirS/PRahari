"""FastAPI application factory and entry point."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.webhook import router as webhook_router
from app.business.worker import run_worker
from app.config import settings
from app.database.connection import initialize_database
from app.logging_config import configure_logging, get_logger

configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown."""
    logger.info("app.startup", app_env=settings.app_env)
    if not settings.github_token:
        logger.warning(
            "app.startup.missing_github_token",
            message="GITHUB_TOKEN is empty. Runtime config is loaded from .env or process env, not .env.example.",
        )
    if not settings.github_webhook_secret:
        logger.warning(
            "app.startup.missing_webhook_secret",
            message="GITHUB_WEBHOOK_SECRET is empty. Signature validation is disabled.",
        )
    initialize_database()
    worker_task = asyncio.create_task(run_worker())
    yield
    worker_task.cancel()
    logger.info("app.shutdown")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="PRahari",
        description="Webhook-driven automated PR review bot",
        version="0.1.0",
        lifespan=_lifespan,
    )

    @app.get("/health", tags=["ops"])
    async def health() -> JSONResponse:
        """Return service health status."""
        return JSONResponse({"status": "ok"})

    app.include_router(webhook_router, tags=["webhook"])
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_env == "development",
    )
