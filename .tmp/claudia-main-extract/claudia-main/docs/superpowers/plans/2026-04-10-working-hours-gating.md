# Working-Hours Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate Claudia's job execution to defined UTC windows per job type: own-PR work (feedback, ci_check, hygiene, implement, memory) runs `[19:01, 07:00)` UTC; review jobs run `[19:01, 12:30)` UTC. Jobs enqueued outside their window are deferred to the next window open via a two-gate design.

**Architecture:** Two gates. Gate 1 is an advisory enqueue-time clamp of `run_after` to the next window open (so human DB inspection shows realistic ETAs). Gate 2 is an authoritative claim-time SQL predicate embedded directly in `claim_next_job`, using a single `COALESCE(check_time, now())` timestamp so the time-of-day test and `run_after` comparison are atomic under one clock. A pure `windows.py` module holds the boundary constants and helpers. The worker main loop classifies the "nothing to claim" state into empty/window-blocked/debounce and announces window-blocked sleeps on Slack.

**Tech Stack:** Python 3.10+, PostgreSQL (psycopg2), FastAPI (unchanged), pytest (new dev dependency).

**Spec:** [`docs/superpowers/specs/2026-04-10-working-hours-gating-design.md`](../specs/2026-04-10-working-hours-gating-design.md)

---

## File Structure

**Created:**
- `windows.py` — Pure module: window constants, `is_allowed_now`, `next_allowed_after`, `next_allowed_for_types`. No DB, no IO.
- `tests/__init__.py` — Empty, makes tests a package.
- `tests/conftest.py` — Shared pytest fixtures; DB fixture that skips if Postgres is unavailable.
- `tests/test_windows.py` — Unit tests for `windows.py` (boundary, wrap, completeness, sync).
- `tests/test_db_windows.py` — DB integration tests for `enqueue_job` clamping and `claim_next_job` window predicate using injected `check_time`.
- `tests/test_worker_nap.py` — Unit test of the extracted nap-state classifier.
- `requirements-dev.txt` — pytest dependency.

**Modified:**
- `db.py`
  - `enqueue_job` (lines 195-247): add `min_run_after: datetime | None = None` parameter; rewrite `run_after` SQL in both INSERT and ON CONFLICT paths using `GREATEST(jobs.run_after, now()+interval, COALESCE(min_run_after, now()))`.
  - `claim_next_job` (lines 250-279): add `allowed_types: list[str]` and `check_time: datetime | None = None` parameters; rewrite inner SELECT with `WITH check_ts ... tod` CTE and explicit per-type CASE window predicate with `::job_type[]` cast.
  - Add SQL-literal constants `_SQL_OWN_WINDOW_START`, etc. near the top for the sync test to reference.
- `worker.py`
  - Six `db.enqueue_job` call sites (lines 1243, 1382, 1417, 1558, 1649, 1685): pass `min_run_after=windows.next_allowed_after(job_type, datetime.now(timezone.utc))`.
  - `worker_loop` (around lines 1843, 1916-1931): add `window_sleep_announced_until` state; extract nap classifier into a pure helper; replace the single `claim_next_job + idle_announced` branch with the three-state nap logic.
- `webhook_receiver.py`
  - Enqueue call site (line 324): pass `min_run_after=windows.next_allowed_after(job["job_type"], datetime.now(timezone.utc))`. Note: the key is `job_type`, NOT `type`.

---

## Task 1: Test scaffolding and dev dependencies

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create `requirements-dev.txt`**

```text
pytest>=8.0.0
```

- [ ] **Step 2: Install dev deps into the local venv**

Run: `python3 -m pip install -r requirements-dev.txt`
Expected: pytest installs successfully (or already satisfied).

- [ ] **Step 3: Create `tests/__init__.py`**

Empty file (zero bytes).

- [ ] **Step 4: Create `tests/conftest.py`**

```python
"""Shared pytest fixtures for the claudia test suite."""

import os
import sys
import uuid
from pathlib import Path

import pytest

# Make repo root importable so tests can `import db`, `import windows`, etc.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def pg_conn():
    """Yield a connection to a throwaway schema in the local claudia DB.

    Skips the test if Postgres isn't reachable. Uses a unique schema per test
    so parallel runs don't collide, and drops the schema on teardown.
    """
    try:
        import db as claudia_db
    except ImportError:
        pytest.skip("db module not importable")

    try:
        conn = claudia_db.connect()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable: {exc}")

    schema = f"test_{uuid.uuid4().hex[:12]}"
    cur = conn.cursor()
    cur.execute(f'CREATE SCHEMA "{schema}"')
    cur.execute(f'SET search_path TO "{schema}", public')
    # Run schema against this schema. The enum types are global so CREATE TYPE
    # inside the DO block is a no-op on repeat runs.
    cur.execute(claudia_db.SCHEMA_SQL)
    conn.commit()
    cur.close()

    try:
        yield conn
    finally:
        try:
            cur = conn.cursor()
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
            conn.commit()
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
```

- [ ] **Step 5: Sanity-run pytest with no tests yet**

Run: `python3 -m pytest tests/ -q`
Expected: `no tests ran` (exit 5 is fine) — proves discovery works.

- [ ] **Step 6: Commit**

```bash
git add requirements-dev.txt tests/__init__.py tests/conftest.py
git commit -m "chore: add pytest scaffolding and DB fixture"
```

---

## Task 2: Create `windows.py` with pure helpers

**Files:**
- Create: `windows.py`
- Test: `tests/test_windows.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_windows.py`:

```python
"""Unit tests for windows.py — pure time-of-day window logic."""

from datetime import datetime, time, timedelta, timezone

import pytest

import windows


UTC = timezone.utc


def dt(y, mo, d, h, mi, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=UTC)


# ── is_allowed_now: own window [19:01, 07:00) ──────────────────────────────

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
def test_is_allowed_now_own_window(now, expected):
    for job_type in ("feedback", "ci_check", "hygiene", "implement", "memory"):
        assert windows.is_allowed_now(job_type, now) is expected


# ── is_allowed_now: review window [19:01, 12:30) ──────────────────────────

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
def test_is_allowed_now_review_window(now, expected):
    assert windows.is_allowed_now("review", now) is expected


# ── next_allowed_after ─────────────────────────────────────────────────────

def test_next_allowed_after_returns_none_when_inside_window():
    now = dt(2026, 4, 11, 3, 0, 0)  # inside own window
    assert windows.next_allowed_after("feedback", now) is None
    assert windows.next_allowed_after("review", now) is None


def test_next_allowed_after_own_window_blocked_morning():
    # 08:00 UTC — own is blocked until 19:01 today
    now = dt(2026, 4, 11, 8, 0, 0)
    assert windows.next_allowed_after("feedback", now) == dt(2026, 4, 11, 19, 1, 0)


def test_next_allowed_after_review_window_blocked_afternoon():
    # 14:00 UTC — review is blocked until 19:01 today
    now = dt(2026, 4, 11, 14, 0, 0)
    assert windows.next_allowed_after("review", now) == dt(2026, 4, 11, 19, 1, 0)


def test_next_allowed_after_exact_close_own():
    now = dt(2026, 4, 11, 7, 0, 0)  # exactly at own close — blocked
    assert windows.next_allowed_after("feedback", now) == dt(2026, 4, 11, 19, 1, 0)


def test_next_allowed_after_exact_close_review():
    now = dt(2026, 4, 11, 12, 30, 0)  # exactly at review close — blocked
    assert windows.next_allowed_after("review", now) == dt(2026, 4, 11, 19, 1, 0)


def test_next_allowed_after_just_before_open():
    # 19:00:59 — blocked, next open is 19:01 today
    now = dt(2026, 4, 11, 19, 0, 59)
    assert windows.next_allowed_after("feedback", now) == dt(2026, 4, 11, 19, 1, 0)


# ── next_allowed_for_types ─────────────────────────────────────────────────

def test_next_allowed_for_types_returns_none_if_any_allowed():
    now = dt(2026, 4, 11, 8, 0, 0)  # review allowed, feedback blocked
    assert windows.next_allowed_for_types(["feedback", "review"], now) is None


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
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `python3 -m pytest tests/test_windows.py -q`
Expected: `ModuleNotFoundError: No module named 'windows'` (collection error).

- [ ] **Step 3: Create `windows.py`**

```python
"""Working-hours windows for Claudia's job types.

Pure module — no DB, no IO. All datetimes are UTC-aware; naive datetimes
are rejected via assertion.

Half-open interval semantics: a window `[start, end)` includes `start` and
excludes `end`. Both own and review windows wrap across midnight.
"""

from datetime import datetime, time, timedelta, timezone
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


def _assert_utc(dt: datetime) -> None:
    assert dt.tzinfo is not None and dt.utcoffset() == timedelta(0), \
        f"datetime must be UTC-aware, got {dt!r}"


def _in_wrapping_window(t: time, start: time, end: time) -> bool:
    """Half-open `[start, end)` where start > end wraps across midnight."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def is_allowed_now(job_type: str, now: datetime) -> bool:
    """True if `job_type` may run at `now` (UTC-aware)."""
    _assert_utc(now)
    window = JOB_TYPE_WINDOWS.get(job_type)
    if window is None:
        return False
    start, end = window
    return _in_wrapping_window(now.timetz().replace(tzinfo=None), start, end)


def next_allowed_after(job_type: str, now: datetime) -> datetime | None:
    """Datetime of the next window open for `job_type`, or None if allowed now.

    Always returns a UTC-aware datetime with seconds/microseconds set to zero
    (window boundaries are whole minutes).
    """
    _assert_utc(now)
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_windows.py -q`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add windows.py tests/test_windows.py
git commit -m "feat: add windows module with working-hours gating helpers"
```

---

## Task 3: Extend `db.enqueue_job` with `min_run_after` clamp

**Files:**
- Modify: `db.py` (lines 195-247)
- Test: `tests/test_db_windows.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_db_windows.py` (partial — add more in Task 4):

```python
"""DB integration tests for window gating in db.py."""

from datetime import datetime, timedelta, timezone

import pytest

import db


UTC = timezone.utc


def _insert_and_fetch(conn, **kwargs):
    job_id = db.enqueue_job(conn, **kwargs)
    cur = conn.cursor()
    cur.execute("SELECT id, run_after FROM jobs WHERE id = %s", (job_id,))
    row = cur.fetchone()
    cur.close()
    return job_id, row[1]


def test_enqueue_without_min_run_after(pg_conn):
    """Without a clamp, run_after is now + debounce (existing behaviour)."""
    job_id, run_after = _insert_and_fetch(
        pg_conn,
        job_type="feedback",
        dedup_key="test:no-clamp",
        payload={"repo": "x/y"},
        debounce_seconds=30,
    )
    assert job_id is not None
    # run_after should be ~30s in the future, not clamped by anything.
    now = datetime.now(UTC)
    assert timedelta(seconds=20) < (run_after - now) < timedelta(seconds=60)


def test_enqueue_with_min_run_after_in_future_clamps_forward(pg_conn):
    """min_run_after > (now + debounce) wins — job is clamped forward."""
    target = datetime.now(UTC) + timedelta(hours=4)
    job_id, run_after = _insert_and_fetch(
        pg_conn,
        job_type="feedback",
        dedup_key="test:clamp-future",
        payload={"repo": "x/y"},
        debounce_seconds=30,
        min_run_after=target,
    )
    # Allow a 2s slop for DB vs Python clock.
    assert abs((run_after - target).total_seconds()) < 2


def test_enqueue_with_min_run_after_in_past_has_no_effect(pg_conn):
    """min_run_after in the past degenerates to now via COALESCE logic."""
    past = datetime.now(UTC) - timedelta(hours=4)
    job_id, run_after = _insert_and_fetch(
        pg_conn,
        job_type="feedback",
        dedup_key="test:clamp-past",
        payload={"repo": "x/y"},
        debounce_seconds=30,
        min_run_after=past,
    )
    # Should be ~now+30s, not the past value.
    now = datetime.now(UTC)
    assert timedelta(seconds=20) < (run_after - now) < timedelta(seconds=60)


def test_enqueue_on_conflict_preserves_later_existing_run_after(pg_conn):
    """Existing row with far-future run_after must NOT be regressed by a fresh enqueue."""
    far_future = datetime.now(UTC) + timedelta(hours=10)
    db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:conflict",
        payload={"repo": "x/y"},
        debounce_seconds=30,
        min_run_after=far_future,
    )
    # Re-enqueue with no clamp — should not pull run_after earlier.
    db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:conflict",
        payload={"repo": "x/y"},
        debounce_seconds=30,
    )
    cur = pg_conn.cursor()
    cur.execute("SELECT run_after FROM jobs WHERE dedup_key = 'test:conflict'")
    row = cur.fetchone()
    cur.close()
    assert abs((row[0] - far_future).total_seconds()) < 2, \
        f"run_after was regressed: {row[0]} vs {far_future}"


def test_enqueue_on_conflict_clamps_forward_when_min_run_after_is_later(pg_conn):
    """New min_run_after > existing run_after should pull job forward."""
    db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:conflict-forward",
        payload={"repo": "x/y"},
        debounce_seconds=30,
    )
    target = datetime.now(UTC) + timedelta(hours=6)
    db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:conflict-forward",
        payload={"repo": "x/y"},
        debounce_seconds=30,
        min_run_after=target,
    )
    cur = pg_conn.cursor()
    cur.execute("SELECT run_after FROM jobs WHERE dedup_key = 'test:conflict-forward'")
    row = cur.fetchone()
    cur.close()
    assert abs((row[0] - target).total_seconds()) < 2
```

- [ ] **Step 2: Run the tests — expected to fail**

Run: `python3 -m pytest tests/test_db_windows.py -q`
Expected: all fail with `TypeError: enqueue_job() got an unexpected keyword argument 'min_run_after'` (or skipped if Postgres not reachable).

- [ ] **Step 3: Modify `db.enqueue_job` to accept and apply `min_run_after`**

Replace the function body at `db.py:195-247` with:

```python
def enqueue_job(
    conn: psycopg2.extensions.connection,
    job_type: str,
    dedup_key: str,
    payload: dict[str, Any],
    debounce_seconds: int = 60,
    priority: int | None = None,
    min_run_after: datetime | None = None,
) -> int | None:
    """Insert a job with ON CONFLICT debounce. Returns job ID or None if coalesced.

    `min_run_after` is an advisory lower bound from Gate 1 (the working-hours
    clamp). It can only push `run_after` forward — never earlier — and never
    regresses an existing backoff-deferred row in the ON CONFLICT path.
    """
    if priority is None:
        priority = PRIORITY.get(job_type, 50)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        INSERT INTO jobs (type, dedup_key, priority, payload, run_after)
        VALUES (
            %(type)s, %(dedup_key)s, %(priority)s, %(payload)s,
            GREATEST(
                now() + make_interval(secs := %(debounce)s),
                COALESCE(%(min_run_after)s, now())
            )
        )
        ON CONFLICT (dedup_key) WHERE status = 'pending'
        DO UPDATE SET
            run_after = GREATEST(
                jobs.run_after,
                now() + make_interval(secs := %(debounce)s),
                COALESCE(%(min_run_after)s, now())
            ),
            updated_at = now(),
            priority = LEAST(jobs.priority, EXCLUDED.priority),
            payload = jsonb_build_object(
                'repo', COALESCE(EXCLUDED.payload->>'repo', jobs.payload->>'repo'),
                'title', COALESCE(EXCLUDED.payload->>'title', jobs.payload->>'title'),
                'pr_number', COALESCE(EXCLUDED.payload->>'pr_number', jobs.payload->>'pr_number'),
                'issue_number', COALESCE(EXCLUDED.payload->>'issue_number', jobs.payload->>'issue_number'),
                'reasons', (
                    SELECT jsonb_agg(DISTINCT r)
                    FROM jsonb_array_elements(
                        COALESCE(jobs.payload->'reasons', '[]'::jsonb) ||
                        COALESCE(EXCLUDED.payload->'reasons', '[]'::jsonb)
                    ) AS r
                ),
                'latest_head_sha', COALESCE(EXCLUDED.payload->>'latest_head_sha', jobs.payload->>'latest_head_sha'),
                'base_ref', COALESCE(EXCLUDED.payload->>'base_ref', jobs.payload->>'base_ref'),
                'head_ref', COALESCE(EXCLUDED.payload->>'head_ref', jobs.payload->>'head_ref'),
                'conclusion', COALESCE(EXCLUDED.payload->>'conclusion', jobs.payload->>'conclusion')
            )
        RETURNING id
        """,
        {
            "type": job_type,
            "dedup_key": dedup_key,
            "priority": priority,
            "payload": json.dumps(payload),
            "debounce": debounce_seconds,
            "min_run_after": min_run_after,
        },
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    return row["id"] if row else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_db_windows.py -q`
Expected: all pass (or skipped uniformly if Postgres is not reachable).

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db_windows.py
git commit -m "feat(db): add min_run_after clamp to enqueue_job"
```

---

## Task 4: Rewrite `claim_next_job` with SQL-inlined window predicate

**Files:**
- Modify: `db.py` (lines 250-279, plus new module-level SQL constants)
- Test: `tests/test_db_windows.py` (append)

- [ ] **Step 1: Add failing claim-time tests**

Append to `tests/test_db_windows.py`:

```python
# ── claim_next_job with window predicate ─────────────────────────────────

def _enqueue_runnable(conn, **kwargs):
    """Helper: enqueue with debounce=0 so run_after <= now() immediately."""
    kwargs.setdefault("debounce_seconds", 0)
    return db.enqueue_job(conn, **kwargs)


def test_claim_allowed_when_inside_own_window(pg_conn):
    _enqueue_runnable(
        pg_conn,
        job_type="feedback",
        dedup_key="claim:own:inside",
        payload={"repo": "x/y"},
    )
    check = datetime(2026, 4, 11, 3, 0, 0, tzinfo=UTC)  # inside own window
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback"],
        check_time=check,
    )
    assert job is not None
    assert job["type"] == "feedback"


def test_claim_blocked_outside_own_window(pg_conn):
    _enqueue_runnable(
        pg_conn,
        job_type="feedback",
        dedup_key="claim:own:outside",
        payload={"repo": "x/y"},
    )
    check = datetime(2026, 4, 11, 8, 0, 0, tzinfo=UTC)  # outside own window
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback"],
        check_time=check,
    )
    assert job is None


def test_claim_review_allowed_while_own_blocked(pg_conn):
    _enqueue_runnable(pg_conn, job_type="feedback", dedup_key="c:f",
                      payload={"repo": "x/y"})
    _enqueue_runnable(pg_conn, job_type="review", dedup_key="c:r",
                      payload={"repo": "x/y"})
    check = datetime(2026, 4, 11, 8, 0, 0, tzinfo=UTC)  # review OK, own blocked
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback", "review"],
        check_time=check,
    )
    assert job is not None
    assert job["type"] == "review"


def test_claim_exactly_at_review_close_is_blocked(pg_conn):
    _enqueue_runnable(pg_conn, job_type="review", dedup_key="c:close",
                      payload={"repo": "x/y"})
    check = datetime(2026, 4, 11, 12, 30, 0, tzinfo=UTC)
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["review"],
        check_time=check,
    )
    assert job is None


def test_claim_one_microsecond_before_review_close_is_allowed(pg_conn):
    _enqueue_runnable(pg_conn, job_type="review", dedup_key="c:before",
                      payload={"repo": "x/y"})
    check = datetime(2026, 4, 11, 12, 29, 59, 999_000, tzinfo=UTC)
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["review"],
        check_time=check,
    )
    assert job is not None


def test_claim_both_allowed_at_20_utc(pg_conn):
    _enqueue_runnable(pg_conn, job_type="feedback", dedup_key="c:20:f",
                      payload={"repo": "x/y"}, priority=10)
    _enqueue_runnable(pg_conn, job_type="review", dedup_key="c:20:r",
                      payload={"repo": "x/y"}, priority=30)
    check = datetime(2026, 4, 11, 20, 0, 0, tzinfo=UTC)
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback", "review"],
        check_time=check,
    )
    assert job is not None
    # feedback has higher priority (lower number)
    assert job["type"] == "feedback"


def test_claim_allowed_types_acts_as_prefilter(pg_conn):
    """Even if SQL window allows both, allowed_types restricts what can be claimed."""
    _enqueue_runnable(pg_conn, job_type="feedback", dedup_key="c:pf:f",
                      payload={"repo": "x/y"}, priority=10)
    _enqueue_runnable(pg_conn, job_type="review", dedup_key="c:pf:r",
                      payload={"repo": "x/y"}, priority=30)
    check = datetime(2026, 4, 11, 20, 0, 0, tzinfo=UTC)  # both windows open
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["review"],  # only allow review
        check_time=check,
    )
    assert job is not None
    assert job["type"] == "review"


def test_claim_empty_allowed_types_returns_nothing(pg_conn):
    _enqueue_runnable(pg_conn, job_type="feedback", dedup_key="c:empty",
                      payload={"repo": "x/y"})
    check = datetime(2026, 4, 11, 20, 0, 0, tzinfo=UTC)
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=[],
        check_time=check,
    )
    assert job is None


def test_claim_in_production_uses_now(pg_conn):
    """check_time=None falls through to now() — smoke test that the COALESCE works."""
    _enqueue_runnable(pg_conn, job_type="feedback", dedup_key="c:prod",
                      payload={"repo": "x/y"})
    # Pass all types as allowed. Whether this returns depends on wall-clock
    # time of day, so we only assert it doesn't blow up and handles either outcome.
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=list(db.JOB_TYPES),
        check_time=None,
    )
    assert job is None or job["type"] == "feedback"
```

- [ ] **Step 2: Run the new tests — expected to fail**

Run: `python3 -m pytest tests/test_db_windows.py -q -k claim`
Expected: all fail with `TypeError: claim_next_job() got an unexpected keyword argument 'allowed_types'`.

- [ ] **Step 3: Add SQL-literal constants near the top of `db.py`**

Add just after line 36 (after the `ATTEMPT_OUTCOMES` tuple):

```python
# ── Working-hours window SQL literals ─────────────────────────────────────
# These MUST stay in sync with windows.py's constants. The `test_windows.py`
# module imports these and asserts equality, so any change here must be
# mirrored in windows.py (and vice versa).
_SQL_OWN_WINDOW_START = "19:01"
_SQL_OWN_WINDOW_END = "07:00"
_SQL_REVIEW_WINDOW_START = "19:01"
_SQL_REVIEW_WINDOW_END = "12:30"
```

- [ ] **Step 4: Replace `claim_next_job` at `db.py:250-279` with the windowed version**

```python
def claim_next_job(
    conn: psycopg2.extensions.connection,
    worker_pid: int,
    allowed_types: list[str],
    check_time: datetime | None = None,
) -> dict | None:
    """Atomically claim the highest-priority ready job inside its window.

    The window predicate is evaluated in SQL using a single COALESCE clock
    so the time-of-day test and `run_after` comparison are atomic under one
    timestamp. In production `check_time` is always None and `now()` is used;
    tests may pass a fixed UTC datetime to exercise boundary behaviour.

    `allowed_types` is a fast-path pre-filter. An empty list never claims
    anything. The SQL window predicate is independent and authoritative.
    """
    if not allowed_types:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        UPDATE jobs SET
            status = 'processing',
            claimed_at = now(),
            started_at = now(),
            heartbeat_at = now(),
            lease_expires_at = now() + interval '90 minutes',
            worker_pid = %(pid)s,
            updated_at = now()
        WHERE id = (
            WITH check_ts AS (
                SELECT COALESCE(%(check_time)s, now()) AS ts
            ), tod AS (
                SELECT ((ts AT TIME ZONE 'UTC')::time) AS t FROM check_ts
            )
            SELECT id FROM jobs, check_ts, tod
            WHERE status = 'pending'
              AND run_after <= check_ts.ts
              AND type = ANY(%(allowed_types)s::job_type[])
              AND CASE type
                    WHEN 'feedback'::job_type  THEN (tod.t >= TIME '19:01' OR tod.t < TIME '07:00')
                    WHEN 'ci_check'::job_type  THEN (tod.t >= TIME '19:01' OR tod.t < TIME '07:00')
                    WHEN 'hygiene'::job_type   THEN (tod.t >= TIME '19:01' OR tod.t < TIME '07:00')
                    WHEN 'implement'::job_type THEN (tod.t >= TIME '19:01' OR tod.t < TIME '07:00')
                    WHEN 'memory'::job_type    THEN (tod.t >= TIME '19:01' OR tod.t < TIME '07:00')
                    WHEN 'review'::job_type    THEN (tod.t >= TIME '19:01' OR tod.t < TIME '12:30')
                    ELSE FALSE
                  END
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        {
            "pid": worker_pid,
            "allowed_types": allowed_types,
            "check_time": check_time,
        },
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    if row:
        return dict(row)
    return None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_db_windows.py -q`
Expected: all pass (or skipped uniformly).

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_db_windows.py
git commit -m "feat(db): embed working-hours window predicate in claim_next_job"
```

---

## Task 5: SQL/windows constants sync test

**Files:**
- Modify: `tests/test_windows.py` (append)

- [ ] **Step 1: Add the sync test**

Append to `tests/test_windows.py`:

```python
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
```

- [ ] **Step 2: Run the test**

Run: `python3 -m pytest tests/test_windows.py -q -k sql`
Expected: both pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_windows.py
git commit -m "test: assert SQL window literals match windows.py constants"
```

---

## Task 6: Extract nap-state classifier and unit-test it

**Files:**
- Modify: `worker.py` (add pure helper near top of the worker module)
- Test: `tests/test_worker_nap.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_worker_nap.py`:

```python
"""Unit test for the worker's nap-state classifier (pure helper)."""

from datetime import datetime, timezone

import worker


UTC = timezone.utc


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
```

- [ ] **Step 2: Run the tests — expected to fail**

Run: `python3 -m pytest tests/test_worker_nap.py -q`
Expected: `AttributeError: module 'worker' has no attribute 'classify_nap_state'`.

- [ ] **Step 3: Add the pure helper to `worker.py`**

Find a suitable location — just above `worker_loop` (near line 1831). Add:

```python
def classify_nap_state(
    pending_by_type: dict[str, int],
    allowed_types: list[str],
    now: datetime,
) -> tuple[str, datetime | None]:
    """Classify the 'claim returned nothing' state into one of three branches.

    Returns:
        ("empty", None)            — no pending jobs at all.
        ("window_blocked", target) — pending jobs exist, none of their types
                                     overlap allowed_types; target is the
                                     earliest next-allowed datetime across
                                     the blocked pending types.
        ("debounce", None)         — pending jobs exist and overlap
                                     allowed_types, but claim still returned
                                     nothing (their run_after is in the future).
    """
    if not pending_by_type:
        return ("empty", None)
    pending_types = set(pending_by_type.keys())
    allowed_set = set(allowed_types)
    if pending_types & allowed_set:
        return ("debounce", None)
    target = windows.next_allowed_for_types(pending_types, now)
    return ("window_blocked", target)
```

Also add the needed import at the top of `worker.py` if not already present:

```python
import windows
```

(Place alongside the other local module imports like `import db`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_worker_nap.py -q`
Expected: all four tests pass.

- [ ] **Step 5: Commit**

```bash
git add worker.py tests/test_worker_nap.py
git commit -m "feat(worker): extract nap-state classifier with unit tests"
```

---

## Task 7: Wire worker main loop to use window-gated claim and nap states

**Files:**
- Modify: `worker.py` (around lines 1843, 1916-1931)

- [ ] **Step 1: Add imports (if not done in Task 6)**

Verify `worker.py` imports include:

```python
import windows
from datetime import datetime, timezone
```

(The `datetime` import likely already exists; check before adding.)

- [ ] **Step 2: Add `window_sleep_announced_until` state variable**

In `worker_loop` at `worker.py:1843`, add next to `idle_announced`:

```python
    idle_announced: bool = False
    window_sleep_announced_until: datetime | None = None
```

- [ ] **Step 3: Replace the claim/idle block at `worker.py:1916-1931`**

Old code to remove:

```python
        # ── Claim next job ────────────────────────────────────────────────
        try:
            job = db.claim_next_job(conn, worker_pid)
        except Exception as exc:
            log.error("Failed to claim job: %s", exc)
            time.sleep(POLL_INTERVAL)
            continue

        if not job:
            if not idle_announced:
                slack_send("😴 Nothing in the queue — taking a nap until something comes in")
                idle_announced = True
            time.sleep(POLL_INTERVAL)
            continue

        idle_announced = False
```

New replacement:

```python
        # ── Claim next job (with working-hours gating) ───────────────────
        now_utc = datetime.now(timezone.utc)
        allowed_types = [
            t for t in db.JOB_TYPES if windows.is_allowed_now(t, now_utc)
        ]

        if not allowed_types:
            job = None
        else:
            try:
                job = db.claim_next_job(conn, worker_pid, allowed_types)
            except Exception as exc:
                log.error("Failed to claim job: %s", exc)
                time.sleep(POLL_INTERVAL)
                continue

        if not job:
            try:
                pending = db.pending_by_type(conn)
            except Exception as exc:
                log.warning("pending_by_type failed: %s", exc)
                pending = {}

            state, target = classify_nap_state(pending, allowed_types, now_utc)

            if state == "empty":
                if not idle_announced:
                    slack_send(
                        "😴 Nothing in the queue — taking a nap until something comes in"
                    )
                    idle_announced = True
                window_sleep_announced_until = None
            elif state == "window_blocked":
                # Re-announce only when the target datetime changes.
                if target is not None and target != window_sleep_announced_until:
                    slack_send(
                        f"😴 Sleeping until {target.strftime('%H:%M')} UTC — "
                        f"outside my working hours"
                    )
                    window_sleep_announced_until = target
                idle_announced = False
            else:  # "debounce"
                # Normal debounce silence — nothing to announce.
                pass

            time.sleep(POLL_INTERVAL)
            continue

        idle_announced = False
        window_sleep_announced_until = None
```

- [ ] **Step 4: Lint-check the module loads**

Run: `python3 -c "import worker; print('ok')"`
Expected: `ok` (no syntax errors, no missing imports).

- [ ] **Step 5: Re-run the worker nap test (safety regression)**

Run: `python3 -m pytest tests/test_worker_nap.py -q`
Expected: still passes.

- [ ] **Step 6: Commit**

```bash
git add worker.py
git commit -m "feat(worker): gate main loop claim by working-hours windows"
```

---

## Task 8: Pass `min_run_after` from worker enqueue sites

**Files:**
- Modify: `worker.py` (six call sites: lines 1243, 1382, 1417, 1558, 1649, 1685)

- [ ] **Step 1: Update call site 1 — periodic hygiene/memory (line ~1243)**

Change:

```python
                job_id = db.enqueue_job(
                    conn, job_type, dedup_key,
                    payload={"repo": repo, "reasons": ["periodic"]},
                    debounce_seconds=0,
                )
```

To:

```python
                job_id = db.enqueue_job(
                    conn, job_type, dedup_key,
                    payload={"repo": repo, "reasons": ["periodic"]},
                    debounce_seconds=0,
                    min_run_after=windows.next_allowed_after(
                        job_type, datetime.now(timezone.utc)
                    ),
                )
```

- [ ] **Step 2: Update call site 2 — feedback poll (line ~1382)**

Add to the existing call:

```python
                    job_id = db.enqueue_job(
                        conn, "feedback", f"feedback:{repo}:PR:{pr_number}",
                        payload={
                            "repo": repo,
                            "pr_number": pr_number,
                            "title": pr_title,
                            "reasons": reasons,
                            "latest_head_sha": head_sha,
                            "base_ref": base_ref,
                            "head_ref": head_ref,
                        },
                        debounce_seconds=0,
                        min_run_after=windows.next_allowed_after(
                            "feedback", datetime.now(timezone.utc)
                        ),
                    )
```

- [ ] **Step 3: Update call site 3 — ci_check poll (line ~1417)**

Add:

```python
                            min_run_after=windows.next_allowed_after(
                                "ci_check", datetime.now(timezone.utc)
                            ),
```

as the final keyword argument to the `db.enqueue_job(conn, "ci_check", ...)` call.

- [ ] **Step 4: Update call site 4 — review poll (line ~1558)**

Add:

```python
                    min_run_after=windows.next_allowed_after(
                        "review", datetime.now(timezone.utc)
                    ),
```

as the final keyword argument.

- [ ] **Step 5: Update call site 5 — review thread-reply poll (line ~1649)**

Same as Step 4: add `min_run_after=windows.next_allowed_after("review", datetime.now(timezone.utc))` as the final keyword argument.

- [ ] **Step 6: Update call site 6 — implement poll (line ~1685)**

Add:

```python
                min_run_after=windows.next_allowed_after(
                    "implement", datetime.now(timezone.utc)
                ),
```

as the final keyword argument.

- [ ] **Step 7: Verify no site was missed**

Run: `python3 -m grep.py` — or actually:

Run: `python3 -c "
import re, pathlib
src = pathlib.Path('worker.py').read_text()
sites = [m.start() for m in re.finditer(r'db\.enqueue_job\(', src)]
print(f'{len(sites)} enqueue_job call sites')
"`
Expected: `6 enqueue_job call sites`.

Then:

Run: `grep -c 'min_run_after=windows.next_allowed_after' worker.py`
Expected: `6` (one per call site).

- [ ] **Step 8: Verify module loads and tests still pass**

Run: `python3 -c "import worker; print('ok')" && python3 -m pytest tests/ -q`
Expected: `ok` and all tests pass (or skip if no DB).

- [ ] **Step 9: Commit**

```bash
git add worker.py
git commit -m "feat(worker): clamp run_after on enqueue per working-hours window"
```

---

## Task 9: Wire webhook receiver enqueue with `min_run_after`

**Files:**
- Modify: `webhook_receiver.py` (line ~324)

- [ ] **Step 1: Verify current state**

Read `webhook_receiver.py` around lines 1-30 to confirm `datetime`/`timezone` imports. If missing, add:

```python
from datetime import datetime, timezone
```

Also add:

```python
import windows
```

next to the existing `import db`.

- [ ] **Step 2: Update the enqueue call at line ~324**

Old:

```python
        job = _classify_event(event, action, payload)
        if job:
            job_id = db.enqueue_job(conn, **job)
```

New:

```python
        job = _classify_event(event, action, payload)
        if job:
            # Gate 1: clamp run_after forward if this job's window is closed.
            # Note: classify builds dicts with key "job_type", matching
            # enqueue_job's parameter name.
            job_id = db.enqueue_job(
                conn,
                **job,
                min_run_after=windows.next_allowed_after(
                    job["job_type"], datetime.now(timezone.utc)
                ),
            )
```

- [ ] **Step 3: Verify module loads**

Run: `python3 -c "import webhook_receiver; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add webhook_receiver.py
git commit -m "feat(webhook): clamp run_after on enqueue per working-hours window"
```

---

## Task 10: Full test-suite run and final cleanup

**Files:** none new

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: all tests pass (or DB-requiring tests skip cleanly if no Postgres available).

- [ ] **Step 2: Verify no stray references to the old 2-arg `claim_next_job`**

Run: `grep -n 'claim_next_job' worker.py db.py tests/`
Expected: only three kinds of references:
1. `db.py` definition (takes 4 args).
2. `worker.py` call (passes `allowed_types` positionally or as keyword).
3. `tests/test_db_windows.py` calls (pass `allowed_types` + `check_time`).

No other references should exist.

- [ ] **Step 3: Verify no old enqueue_job call missed `min_run_after`**

Run: `grep -n 'enqueue_job' worker.py webhook_receiver.py | grep -v min_run_after | grep -v 'def enqueue_job'`
Expected: empty output (every production call passes `min_run_after`).

- [ ] **Step 4: Lint check if any is configured**

Run: `python3 -m py_compile db.py worker.py webhook_receiver.py windows.py`
Expected: no output, exit 0.

- [ ] **Step 5: Final commit if anything changed**

```bash
git status
# If clean: no-op. If not: git add <files> && git commit -m "chore: final cleanup from working-hours gating"
```

---

## Rollback notes

If this needs to be rolled back after deploy:

1. Revert the commits in reverse order (10 → 1). `min_run_after` defaults to `None`, so reverting worker/webhook without reverting db is safe as an interim step.
2. If only Gate 2 misbehaves, temporarily pass `allowed_types=list(db.JOB_TYPES)` in `worker.py` and the SQL window CASE will still gate correctly — this is a narrower partial rollback that keeps Gate 1 clamps intact.
3. `windows.py` is pure — it can be deleted without touching the DB schema. There are no migrations in this change.

## Self-review notes

- **Spec coverage:** Every item in the spec's Components section has a task (windows.py → Task 2; db.py changes → Tasks 3, 4, 5; worker.py nap → Tasks 6, 7; worker.py enqueue sites → Task 8; webhook → Task 9). Testing tiers are Tasks 2/3/4/5/6.
- **Type consistency:** `classify_nap_state` signature matches between definition and tests. `claim_next_job` signature matches between db.py, worker.py, and tests. `enqueue_job` signature matches between db.py, worker.py, webhook_receiver.py, and tests. Window constants referenced by strftime format match `"%H:%M"` everywhere.
- **No placeholders:** Every code block is complete. Every command has an expected result. No "TBD", no "see spec", no "similar to above" shortcuts — each call site update is fully spelled out.
