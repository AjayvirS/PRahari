"""Application configuration loaded from environment variables / .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime settings for PRahari."""

    model_config = SettingsConfigDict(
        env_file=".env.example",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # GitHub
    github_token: str = ""
    github_webhook_secret: str = ""

    # Application
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    database_path: str = "data/prahari.db"

    # Worker
    worker_poll_interval: int = 5


settings = Settings()
