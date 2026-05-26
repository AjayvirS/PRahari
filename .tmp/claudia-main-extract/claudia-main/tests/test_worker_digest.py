"""TDD tests for the digest transition detection helper and worker shim."""
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

import review_requests as rr


UTC = timezone.utc


# ── should_fire_digest (pure helper) ────────────────────────────────────

@pytest.mark.parametrize("prev,is_now_in,expected", [
    (True,  False, True),
    (True,  True,  False),
    (False, False, False),
    (False, True,  False),
    (None,  False, False),
    (None,  True,  False),
])
def test_should_fire_digest(prev, is_now_in, expected):
    assert rr.should_fire_digest(prev, is_now_in) is expected


# ── worker._run_digest_tick (real driver) ──────────────────────────────

def test_run_digest_tick_calls_fire_on_transition_and_returns_new_state():
    """Drives the actual worker shim. Patches _maybe_fire_digest at the
    review_requests boundary so we verify the shim uses the same code
    path the plan integrates into the main loop."""
    import worker
    conn = MagicMock(name="conn")
    with patch.object(rr, "_maybe_fire_digest") as fire:
        now = datetime(2026, 4, 11, 7, 0, tzinfo=UTC)
        new_state = worker._run_digest_tick(
            conn, now, prev_in_own=True, github_user="claudia-bot"
        )
    fire.assert_called_once_with(conn, now, github_user="claudia-bot")
    assert new_state is False


def test_run_digest_tick_no_call_when_still_inside_window():
    import worker
    conn = MagicMock()
    with patch.object(rr, "_maybe_fire_digest") as fire:
        now = datetime(2026, 4, 11, 6, 0, tzinfo=UTC)
        new_state = worker._run_digest_tick(
            conn, now, prev_in_own=True, github_user="claudia-bot"
        )
    fire.assert_not_called()
    assert new_state is True


def test_run_digest_tick_cold_start_after_close_does_not_fire():
    import worker
    conn = MagicMock()
    with patch.object(rr, "_maybe_fire_digest") as fire:
        now = datetime(2026, 4, 11, 8, 0, tzinfo=UTC)
        new_state = worker._run_digest_tick(
            conn, now, prev_in_own=None, github_user="claudia-bot"
        )
    fire.assert_not_called()
    assert new_state is False


def test_run_digest_tick_swallows_fire_exceptions():
    import worker
    conn = MagicMock()
    with patch.object(rr, "_maybe_fire_digest", side_effect=RuntimeError("boom")):
        now = datetime(2026, 4, 11, 7, 0, tzinfo=UTC)
        new_state = worker._run_digest_tick(
            conn, now, prev_in_own=True, github_user="claudia-bot"
        )
    assert new_state is False
