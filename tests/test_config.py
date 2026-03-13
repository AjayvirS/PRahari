"""Tests for application configuration loading."""
from __future__ import annotations

import pytest

from app.config import Settings


def test_default_settings() -> None:
    s = Settings()
    assert s.app_env == "development"
    assert s.app_port == 8000
    assert s.worker_poll_interval == 5
    assert s.log_level == "INFO"
    assert s.database_path == "data/prahari.db"


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_PORT", "9000")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("GITHUB_TOKEN", "tok123")
    monkeypatch.setenv("DATABASE_PATH", "tmp/test.db")

    s = Settings()
    assert s.app_env == "production"
    assert s.app_port == 9000
    assert s.log_level == "DEBUG"
    assert s.github_token == "tok123"
    assert s.database_path == "tmp/test.db"
