"""Unit tests for windows.py — pure time-of-day window logic."""

from datetime import datetime, time, timedelta, timezone

import pytest

import windows


UTC = timezone.utc


def dt(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=UTC)


# ── is_allowed_now under CLAUDIA_BACKEND=claude ────────────────────────────
# Under claude, the peak-hour gate applies to all PEAK_HOUR_GATED job
# types plus the workflow gate on `implement`. Identical to pre-codex
# behavior.

@pytest.fixture(autouse=False)
def _claude_backend(monkeypatch):
    monkeypatch.setenv("CLAUDIA_BACKEND", "claude")


@pytest.fixture(autouse=False)
def _codex_backend(monkeypatch):
    monkeypatch.setenv("CLAUDIA_BACKEND", "codex")


@pytest.mark.usefixtures("_claude_backend")
@pytest.mark.parametrize("now,expected", [
    (dt(2026, 4, 10, 19,  0, 59, 999_999), False),  # one us before open
    (dt(2026, 4, 10, 19,  1,  0),           True),   # exactly at open (inclusive)
    (dt(2026, 4, 10, 23, 59, 59),           True),
    (dt(2026, 4, 11,  0,  0,  0),           True),   # midnight wrap
    (dt(2026, 4, 11,  3,  0,  0),           True),
    (dt(2026, 4, 11,  6, 59, 59, 999_000),  True),
    (dt(2026, 4, 11,  7,  0,  0),           False),  # exactly at close (exclusive)
    (dt(2026, 4, 11,  7,  0,  0,      1),   False),
    (dt(2026, 4, 11, 12,  0,  0),           False),
    (dt(2026, 4, 11, 19,  0, 59, 999_999), False),
])
def test_is_allowed_now_own_window_under_claude(now, expected):
    for job_type in ("feedback", "ci_check", "hygiene", "implement", "memory"):
        assert windows.is_allowed_now(job_type, now) is expected


@pytest.mark.usefixtures("_claude_backend")
@pytest.mark.parametrize("now,expected", [
    (dt(2026, 4, 10, 19,  0, 59, 999_999), False),
    (dt(2026, 4, 10, 19,  1,  0),           True),
    (dt(2026, 4, 11,  7,  0,  0),           True),   # review is still allowed here
    (dt(2026, 4, 11,  8,  0,  0),           True),
    (dt(2026, 4, 11, 12, 29, 59, 999_000),  True),
    (dt(2026, 4, 11, 12, 30,  0),           False),  # exactly at close (exclusive)
    (dt(2026, 4, 11, 12, 30,  0,      1),   False),
    (dt(2026, 4, 11, 18,  0,  0),           False),
])
def test_is_allowed_now_review_window_under_claude(now, expected):
    assert windows.is_allowed_now("review", now) is expected


# ── is_allowed_now under CLAUDIA_BACKEND=codex ─────────────────────────────
# Peak-hour gate removed (OpenAI has no peak/off-peak); only the
# workflow gate on `implement` remains.

@pytest.mark.usefixtures("_codex_backend")
@pytest.mark.parametrize("now", [
    dt(2026, 4, 11,  3,  0,  0),  # middle of the night
    dt(2026, 4, 11,  8,  0,  0),  # morning
    dt(2026, 4, 11, 12,  0,  0),  # noon
    dt(2026, 4, 11, 13, 30,  0),  # early afternoon — review window CLOSED under claude
    dt(2026, 4, 11, 18,  0,  0),  # late afternoon
])
@pytest.mark.parametrize("job_type", ["feedback", "ci_check", "hygiene", "memory", "review"])
def test_is_allowed_now_peak_gated_types_run_24_7_under_codex(now, job_type):
    assert windows.is_allowed_now(job_type, now) is True


@pytest.mark.usefixtures("_codex_backend")
@pytest.mark.parametrize("now,expected", [
    (dt(2026, 4, 11,  3,  0,  0), True),   # inside overnight window
    (dt(2026, 4, 11,  7,  0,  0), False),  # exactly at close
    (dt(2026, 4, 11, 12,  0,  0), False),  # mid-day
    (dt(2026, 4, 11, 19,  1,  0), True),   # exactly at open
])
def test_is_allowed_now_implement_still_gated_under_codex(now, expected):
    """`implement` is ALWAYS_GATED (workflow gate, not peak-hour gate),
    so the overnight window applies under codex too."""
    assert windows.is_allowed_now("implement", now) is expected


# ── next_allowed_after ─────────────────────────────────────────────────────

@pytest.mark.usefixtures("_claude_backend")
def test_next_allowed_after_returns_none_when_inside_window():
    now = dt(2026, 4, 11, 3, 0, 0)  # inside own window
    assert windows.next_allowed_after("feedback", now) is None
    assert windows.next_allowed_after("review", now) is None


@pytest.mark.usefixtures("_claude_backend")
def test_next_allowed_after_own_window_blocked_morning():
    # 08:00 UTC — own is blocked until 19:01 today (claude only)
    now = dt(2026, 4, 11, 8, 0, 0)
    assert windows.next_allowed_after("feedback", now) == dt(2026, 4, 11, 19, 1, 0)


@pytest.mark.usefixtures("_claude_backend")
def test_next_allowed_after_review_window_blocked_afternoon():
    # 14:00 UTC — review is blocked until 19:01 today (claude only)
    now = dt(2026, 4, 11, 14, 0, 0)
    assert windows.next_allowed_after("review", now) == dt(2026, 4, 11, 19, 1, 0)


@pytest.mark.usefixtures("_claude_backend")
def test_next_allowed_after_exact_close_own():
    now = dt(2026, 4, 11, 7, 0, 0)  # exactly at own close — blocked
    assert windows.next_allowed_after("feedback", now) == dt(2026, 4, 11, 19, 1, 0)


@pytest.mark.usefixtures("_claude_backend")
def test_next_allowed_after_exact_close_review():
    now = dt(2026, 4, 11, 12, 30, 0)  # exactly at review close — blocked
    assert windows.next_allowed_after("review", now) == dt(2026, 4, 11, 19, 1, 0)


@pytest.mark.usefixtures("_claude_backend")
def test_next_allowed_after_just_before_open():
    # 19:00:59 — blocked, next open is 19:01 today
    now = dt(2026, 4, 11, 19, 0, 59)
    assert windows.next_allowed_after("feedback", now) == dt(2026, 4, 11, 19, 1, 0)


@pytest.mark.usefixtures("_codex_backend")
def test_next_allowed_after_peak_gated_type_is_none_under_codex():
    # Under codex the peak-hour gate is dropped — `review` is always
    # allowed, so `next_allowed_after` returns None regardless of time.
    now = dt(2026, 4, 11, 14, 0, 0)  # would be blocked under claude
    assert windows.next_allowed_after("review", now) is None
    assert windows.next_allowed_after("feedback", now) is None


@pytest.mark.usefixtures("_codex_backend")
def test_next_allowed_after_implement_still_blocked_under_codex():
    # `implement` is always gated. At 14:00 UTC the overnight window is
    # closed, so the next opening is 19:01 today.
    now = dt(2026, 4, 11, 14, 0, 0)
    assert windows.next_allowed_after("implement", now) == dt(2026, 4, 11, 19, 1, 0)


# ── next_allowed_for_types ─────────────────────────────────────────────────

@pytest.mark.usefixtures("_claude_backend")
def test_next_allowed_for_types_returns_none_if_any_allowed():
    now = dt(2026, 4, 11, 8, 0, 0)  # review allowed, feedback blocked
    assert windows.next_allowed_for_types(["feedback", "review"], now) is None


@pytest.mark.usefixtures("_claude_backend")
def test_next_allowed_for_types_returns_min_of_blocked():
    # 13:00 UTC — everything blocked; all types open at 19:01 today
    now = dt(2026, 4, 11, 13, 0, 0)
    result = windows.next_allowed_for_types(["feedback", "review"], now)
    assert result == dt(2026, 4, 11, 19, 1, 0)


def test_next_allowed_for_types_empty_input():
    now = dt(2026, 4, 11, 13, 0, 0)
    assert windows.next_allowed_for_types([], now) is None


# ── Completeness ───────────────────────────────────────────────────────────

def test_every_job_type_has_a_window():
    import db
    for job_type in db.JOB_TYPES:
        assert job_type in windows.JOB_TYPE_WINDOWS, \
            f"job type {job_type!r} missing from JOB_TYPE_WINDOWS"


# ── Input validation ──────────────────────────────────────────────────────

def test_naive_datetime_rejected():
    naive = datetime(2026, 4, 11, 8, 0, 0)  # no tzinfo
    with pytest.raises(AssertionError):
        windows.is_allowed_now("feedback", naive)


# ── SQL / windows.py constants sync ───────────────────────────────────────


def test_sql_literals_match_windows_constants():
    """If someone edits the SQL predicate in db.py, this test flags it."""
    import db
    assert db._SQL_OWN_WINDOW_START == windows.OWN_WINDOW_START.strftime("%H:%M")
    assert db._SQL_OWN_WINDOW_END == windows.OWN_WINDOW_END.strftime("%H:%M")
    assert db._SQL_REVIEW_WINDOW_START == windows.REVIEW_WINDOW_START.strftime("%H:%M")
    assert db._SQL_REVIEW_WINDOW_END == windows.REVIEW_WINDOW_END.strftime("%H:%M")


def test_sql_literals_appear_in_claim_query():
    """Guards against the SQL constants going out of date vs the inlined CASE."""
    import db
    import inspect
    src = inspect.getsource(db.claim_next_job)
    assert f"TIME '{db._SQL_OWN_WINDOW_START}'" in src
    assert f"TIME '{db._SQL_OWN_WINDOW_END}'" in src
    assert f"TIME '{db._SQL_REVIEW_WINDOW_END}'" in src


# ── Explicit gate categorization ───────────────────────────────────────────

def test_gate_categories_partition_all_job_types():
    """Every db.JOB_TYPES entry belongs to exactly one gate category."""
    import db
    always = windows.ALWAYS_GATED_JOB_TYPES
    peak = windows.PEAK_HOUR_GATED_JOB_TYPES
    assert always.isdisjoint(peak), \
        "a job type can't be in both gate categories"
    for jt in db.JOB_TYPES:
        assert jt in always or jt in peak, \
            f"{jt!r} missing from both gate categories"


def test_implement_is_always_gated():
    assert "implement" in windows.ALWAYS_GATED_JOB_TYPES


def test_review_feedback_etc_are_peak_hour_gated():
    for jt in ("review", "feedback", "ci_check", "hygiene", "memory"):
        assert jt in windows.PEAK_HOUR_GATED_JOB_TYPES
