"""Tests for CodexBackend.query_quota against fake_codex_app_server."""
import os
import time
from pathlib import Path

import pytest

from backends.codex import CodexBackend

FIX = Path(__file__).parent / "fixtures"
FAKE = FIX / "fake_codex_app_server.py"


@pytest.fixture(autouse=True)
def _wire_fake(monkeypatch):
    """Replace `codex` in build_command's quota path with a wrapper that
    runs the fake. CodexBackend.query_quota uses CODEX_BIN env to find codex."""
    # Wrapper that ignores the codex sub-args and runs the fake script instead.
    wrapper = FIX / "codex_wrapper.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f'exec {FAKE} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(wrapper))
    try:
        yield
    finally:
        wrapper.unlink(missing_ok=True)


def test_quota_success(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CODEX_MODE", "success")
    b = CodexBackend(prices_path=tmp_path / "p.json")
    q = b.query_quota(timeout=5.0)
    assert q is not None
    assert "session" in q and "weekly" in q
    assert q["session"]["used_pct"] == 12.5
    assert q["session"]["remaining_pct"] == 87.5
    assert q["weekly"]["used_pct"] == 60.0


def test_quota_notifications_interleaved(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CODEX_MODE", "notifications_interleaved")
    b = CodexBackend(prices_path=tmp_path / "p.json")
    q = b.query_quota(timeout=5.0)
    assert q is not None
    assert q["session"]["used_pct"] == 12.5


def test_quota_auth_error_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CODEX_MODE", "auth_error")
    b = CodexBackend(prices_path=tmp_path / "p.json")
    assert b.query_quota(timeout=5.0) is None


def test_quota_malformed_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CODEX_MODE", "malformed_response")
    b = CodexBackend(prices_path=tmp_path / "p.json")
    assert b.query_quota(timeout=5.0) is None


def test_quota_hang_times_out_kills_child(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CODEX_MODE", "hang")
    b = CodexBackend(prices_path=tmp_path / "p.json")
    t0 = time.monotonic()
    result = b.query_quota(timeout=2.0)
    elapsed = time.monotonic() - t0
    assert result is None
    assert elapsed < 4.0
