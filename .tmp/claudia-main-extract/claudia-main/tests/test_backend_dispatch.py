"""Tests for backends.get_backend factory and re-exports."""
import pytest

import backends


def test_re_exports():
    # Convenience re-exports so callers don't dig into submodules.
    assert hasattr(backends, "run_with_heartbeat")
    assert hasattr(backends, "RunResult")
    assert hasattr(backends, "RunContext")
    assert hasattr(backends, "ParsedRun")


def test_get_backend_claude():
    b = backends.get_backend("claude")
    assert b.name == "claude"
    assert b.requires_delta_for_success is False


def test_get_backend_codex():
    b = backends.get_backend("codex")
    assert b.name == "codex"
    assert b.requires_delta_for_success is True


def test_get_backend_unknown_raises():
    with pytest.raises(ValueError):
        backends.get_backend("anthropic-v2")
