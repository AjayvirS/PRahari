"""Application configuration loaded from environment variables / .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime settings for PRahari."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # GitHub
    github_token: str = ""
    github_webhook_secret: str = ""

    # Review generation
    review_provider: str = "deterministic"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_seconds: float = 30.0

    # Application
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    database_path: str = "data/prahari.db"

    # Worker
    worker_poll_interval: int = 5


settings = Settings()
