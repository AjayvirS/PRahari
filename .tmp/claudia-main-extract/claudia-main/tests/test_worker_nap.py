"""Unit test for the worker's nap-state classifier (pure helper).

Pinned to CLAUDIA_BACKEND=claude because nap-state's `window_blocked`
case is driven by `windows.next_allowed_for_types`, which under codex
returns None for peak-hour-gated types (they're 24/7). The interesting
"sleep until next opening" target only exists under claude.
"""

from datetime import datetime, timezone

import pytest

import worker


UTC = timezone.utc


@pytest.fixture(autouse=True)
def _claude_backend(monkeypatch):
    monkeypatch.setenv("CLAUDIA_BACKEND", "claude")


def test_empty_queue_state():
    state, target = worker.classify_nap_state(
        pending_by_type={},
        allowed_types=["feedback", "review"],
        now=datetime(2026, 4, 11, 8, 0, 0, tzinfo=UTC),
    )
    assert state == "empty"
    assert target is None


def test_debounce_only_state():
    """Pending types overlap allowed_types, but claim returned nothing
    because run_after is in the future — normal debounce behaviour."""
    state, target = worker.classify_nap_state(
        pending_by_type={"feedback": 2},
        allowed_types=["feedback", "ci_check", "hygiene", "implement", "memory"],
        now=datetime(2026, 4, 11, 3, 0, 0, tzinfo=UTC),  # inside own window
    )
    assert state == "debounce"
    assert target is None


def test_window_blocked_state_own_window_only():
    """At 08:00 UTC: only feedback pending, own window closed, review window open."""
    state, target = worker.classify_nap_state(
        pending_by_type={"feedback": 1},
        allowed_types=["review"],  # own types are NOT in allowed_types at 08:00
        now=datetime(2026, 4, 11, 8, 0, 0, tzinfo=UTC),
    )
    assert state == "window_blocked"
    # Target is next open for the blocked pending type (feedback) = 19:01 today.
    assert target == datetime(2026, 4, 11, 19, 1, 0, tzinfo=UTC)


def test_window_blocked_state_all_blocked_afternoon():
    """At 13:00 UTC: review and feedback both blocked."""
    state, target = worker.classify_nap_state(
        pending_by_type={"feedback": 1, "review": 2},
        allowed_types=[],
        now=datetime(2026, 4, 11, 13, 0, 0, tzinfo=UTC),
    )
    assert state == "window_blocked"
    assert target == datetime(2026, 4, 11, 19, 1, 0, tzinfo=UTC)
