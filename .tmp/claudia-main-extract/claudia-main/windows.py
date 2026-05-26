"""Working-hours windows for Claudia's job types.

Pure module (apart from a single env-var read at import for the backend
name) — no DB, no IO at call time. All datetimes are UTC-aware; naive
datetimes are rejected via assertion.

Half-open interval semantics: a window `[start, end)` includes `start` and
excludes `end`. Both own and review windows wrap across midnight.

Two different motivations for gating, each enforced via its own list:

  * ALWAYS_GATED_JOB_TYPES — workflow gate. `implement` opens autonomous
    PRs; those should land overnight so the team can review them in the
    morning, not flood Slack mid-day. Backend-independent.

  * PEAK_HOUR_GATED_JOB_TYPES — Claude/Anthropic-API peak-hour
    avoidance. Anthropic's API has peak hours during which quota burns
    much faster, so on the claude backend we sleep through them. OpenAI
    has no comparable peak/off-peak dynamic — under codex these gates
    are dropped and the job types run 24/7.
"""

import os
from datetime import date, datetime, time, timedelta
from typing import Iterable

# ── Window boundaries ─────────────────────────────────────────────────────

OWN_WINDOW_START = time(19, 1)   # inclusive
OWN_WINDOW_END = time(7, 0)      # exclusive
REVIEW_WINDOW_START = time(19, 1)  # inclusive
REVIEW_WINDOW_END = time(12, 30)   # exclusive

JOB_TYPE_WINDOWS: dict[str, tuple[time, time]] = {
    "feedback":  (OWN_WINDOW_START, OWN_WINDOW_END),
    "ci_check":  (OWN_WINDOW_START, OWN_WINDOW_END),
    "hygiene":   (OWN_WINDOW_START, OWN_WINDOW_END),
    "implement": (OWN_WINDOW_START, OWN_WINDOW_END),
    "memory":    (OWN_WINDOW_START, OWN_WINDOW_END),
    "review":    (REVIEW_WINDOW_START, REVIEW_WINDOW_END),
}

# Workflow gate — applies under every backend.
ALWAYS_GATED_JOB_TYPES: frozenset[str] = frozenset({"implement"})

# Peak-hour-avoidance gate — applies under claude only.
PEAK_HOUR_GATED_JOB_TYPES: frozenset[str] = frozenset({
    "feedback", "ci_check", "hygiene", "memory", "review",
})


def _current_backend() -> str:
    """Read backend name from env each call so tests + worker stay in sync.

    Cheap (single os.getenv), and avoids a stale module-level cache when
    tests monkeypatch CLAUDIA_BACKEND between cases.
    """
    return os.getenv("CLAUDIA_BACKEND", "codex")


def _is_window_gated(job_type: str, backend: str | None = None) -> bool:
    """True iff `job_type` should be window-gated under `backend`."""
    if backend is None:
        backend = _current_backend()
    if job_type in ALWAYS_GATED_JOB_TYPES:
        return True
    if job_type in PEAK_HOUR_GATED_JOB_TYPES and backend == "claude":
        return True
    return False


def _assert_utc(dt: datetime) -> None:
    assert dt.tzinfo is not None and dt.utcoffset() == timedelta(0), \
        f"datetime must be UTC-aware, got {dt!r}"


def _in_wrapping_window(t: time, start: time, end: time) -> bool:
    """Half-open `[start, end)` where start > end wraps across midnight."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def is_allowed_now(job_type: str, now: datetime) -> bool:
    """True if `job_type` may run at `now` (UTC-aware).

    A job type that is not window-gated for the current backend is
    always allowed; a gated type is allowed iff `now` falls within its
    window in JOB_TYPE_WINDOWS.
    """
    _assert_utc(now)
    if not _is_window_gated(job_type):
        return True
    window = JOB_TYPE_WINDOWS.get(job_type)
    if window is None:
        return False
    start, end = window
    return _in_wrapping_window(now.timetz().replace(tzinfo=None), start, end)


def next_allowed_after(job_type: str, now: datetime) -> datetime | None:
    """Datetime of the next window open for `job_type`, or None if allowed now.

    Always returns a UTC-aware datetime with seconds/microseconds set to zero
    (window boundaries are whole minutes). Returns None if `job_type` is
    not window-gated under the current backend (it's always allowed).
    """
    _assert_utc(now)
    if not _is_window_gated(job_type):
        return None
    if is_allowed_now(job_type, now):
        return None
    window = JOB_TYPE_WINDOWS.get(job_type)
    if window is None:
        # Unknown type — no gating, caller should handle however it wants.
        return None
    start, _end = window
    candidate = now.replace(
        hour=start.hour, minute=start.minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate


def next_allowed_for_types(
    types: Iterable[str], now: datetime
) -> datetime | None:
    """Earliest next-window-open across `types`, or None if any is allowed now."""
    _assert_utc(now)
    candidates: list[datetime] = []
    for t in types:
        nxt = next_allowed_after(t, now)
        if nxt is None:
            # At least one type is allowed right now — nothing to wait for.
            return None
        candidates.append(nxt)
    if not candidates:
        return None
    return min(candidates)


def current_own_session_day(now: datetime) -> date:
    """UTC date of the 19:01 own-window start for the session `now` belongs to.

    Rule: if now.time() >= 19:01 → today's date; else → yesterday's date.
    Sessions span [19:01 UTC D, 07:00 UTC D+1); PRs landed after 07:00 but
    before 19:01 belong to the most recently opened session (D-1).
    """
    _assert_utc(now)
    if now.timetz().replace(tzinfo=None) >= OWN_WINDOW_START:
        return now.date()
    return (now - timedelta(days=1)).date()
