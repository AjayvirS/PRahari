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


# ── claim_next_job with window predicate ─────────────────────────────────

def _enqueue_runnable(conn, check_time=None, **kwargs):
    """Enqueue a job and force `run_after` to be ≤ the claim check time.

    `enqueue_job` always sets `run_after = GREATEST(now() + debounce, ...)`,
    which is a wall-clock value. Pairing that with a fixed-date `check_time`
    in tests is a time bomb: once the real clock moves past `check_time`,
    the `run_after <= check_ts.ts` predicate in the claim SQL starts blocking
    the row for the wrong reason. To stay deterministic we overwrite
    `run_after` to `check_time - 1 day` immediately after enqueue, so the
    run_after comparison always passes regardless of the actual date.
    """
    kwargs.setdefault("debounce_seconds", 0)
    job_id = db.enqueue_job(conn, **kwargs)
    if check_time is not None and job_id is not None:
        cur = conn.cursor()
        cur.execute(
            "UPDATE jobs SET run_after = %s WHERE id = %s",
            (check_time - timedelta(days=1), job_id),
        )
        conn.commit()
        cur.close()
    return job_id


def test_claim_allowed_when_inside_own_window(pg_conn):
    check = datetime(2026, 4, 11, 3, 0, 0, tzinfo=UTC)  # inside own window
    _enqueue_runnable(
        pg_conn,
        check_time=check,
        job_type="feedback",
        dedup_key="claim:own:inside",
        payload={"repo": "x/y"},
    )
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback"],
        check_time=check,
    )
    assert job is not None
    assert job["type"] == "feedback"


# Below: SQL window predicate ("Gate 2") tests. Gate 2 is the
# authoritative time-of-day check, sharing a single `check_ts` snapshot
# with `run_after` so Python↔SQL clock skew can't race at window
# boundaries. All tests pin backend="claude" because under codex the
# peak-hour gate is dropped (separate codex test block below).


def test_claim_blocked_outside_own_window(pg_conn):
    check = datetime(2026, 4, 11, 8, 0, 0, tzinfo=UTC)  # outside own window
    _enqueue_runnable(
        pg_conn,
        check_time=check,
        job_type="feedback",
        dedup_key="claim:own:outside",
        payload={"repo": "x/y"},
    )
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback"],
        check_time=check, backend="claude",
    )
    assert job is None


def test_claim_exactly_at_own_window_close_is_blocked(pg_conn):
    check = datetime(2026, 4, 11, 7, 0, 0, tzinfo=UTC)  # exact own close
    _enqueue_runnable(pg_conn, check_time=check, job_type="feedback",
                      dedup_key="c:own:close", payload={"repo": "x/y"})
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback"],
        check_time=check, backend="claude",
    )
    assert job is None


def test_claim_one_microsecond_before_own_window_close_is_allowed(pg_conn):
    check = datetime(2026, 4, 11, 6, 59, 59, 999_000, tzinfo=UTC)
    _enqueue_runnable(pg_conn, check_time=check, job_type="feedback",
                      dedup_key="c:own:before", payload={"repo": "x/y"})
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback"],
        check_time=check, backend="claude",
    )
    assert job is not None


def test_claim_exactly_at_own_window_open_is_allowed(pg_conn):
    check = datetime(2026, 4, 11, 19, 1, 0, tzinfo=UTC)  # exact own open
    _enqueue_runnable(pg_conn, check_time=check, job_type="feedback",
                      dedup_key="c:own:open", payload={"repo": "x/y"})
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback"],
        check_time=check, backend="claude",
    )
    assert job is not None


def test_claim_review_allowed_while_own_blocked(pg_conn):
    check = datetime(2026, 4, 11, 8, 0, 0, tzinfo=UTC)  # review OK, own blocked
    _enqueue_runnable(pg_conn, check_time=check, job_type="feedback",
                      dedup_key="c:f", payload={"repo": "x/y"})
    _enqueue_runnable(pg_conn, check_time=check, job_type="review",
                      dedup_key="c:r", payload={"repo": "x/y"})
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback", "review"],
        check_time=check, backend="claude",
    )
    assert job is not None
    assert job["type"] == "review"


def test_claim_exactly_at_review_close_is_blocked(pg_conn):
    check = datetime(2026, 4, 11, 12, 30, 0, tzinfo=UTC)
    _enqueue_runnable(pg_conn, check_time=check, job_type="review",
                      dedup_key="c:close", payload={"repo": "x/y"})
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["review"],
        check_time=check, backend="claude",
    )
    assert job is None


def test_claim_one_microsecond_before_review_close_is_allowed(pg_conn):
    check = datetime(2026, 4, 11, 12, 29, 59, 999_000, tzinfo=UTC)
    _enqueue_runnable(pg_conn, check_time=check, job_type="review",
                      dedup_key="c:before", payload={"repo": "x/y"})
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["review"],
        check_time=check, backend="claude",
    )
    assert job is not None


# ── Codex backend: peak-hour gate dropped except for `implement` ──────────


def test_codex_claims_feedback_outside_claude_own_window(pg_conn):
    """Under codex, feedback is 24/7 — gate 2 must allow at 13:00 UTC."""
    check = datetime(2026, 4, 11, 13, 0, 0, tzinfo=UTC)
    _enqueue_runnable(pg_conn, check_time=check, job_type="feedback",
                      dedup_key="c:codex:fb", payload={"repo": "x/y"})
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback"],
        check_time=check, backend="codex",
    )
    assert job is not None
    assert job["type"] == "feedback"


def test_codex_claims_review_outside_claude_review_window(pg_conn):
    """Under codex, review is 24/7 (no bypass needed)."""
    check = datetime(2026, 4, 11, 14, 0, 0, tzinfo=UTC)
    _enqueue_runnable(pg_conn, check_time=check, job_type="review",
                      dedup_key="c:codex:rv", payload={"repo": "x/y"})
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["review"],
        check_time=check, backend="codex",
    )
    assert job is not None


def test_codex_still_gates_implement_to_night(pg_conn):
    """Under codex, `implement` keeps the overnight workflow gate."""
    check_midday = datetime(2026, 4, 11, 13, 0, 0, tzinfo=UTC)
    _enqueue_runnable(pg_conn, check_time=check_midday, job_type="implement",
                      dedup_key="c:codex:impl", payload={"repo": "x/y"})
    blocked = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["implement"],
        check_time=check_midday, backend="codex",
    )
    assert blocked is None
    # At 20:00 UTC the same job is claimable.
    night = datetime(2026, 4, 11, 20, 0, 0, tzinfo=UTC)
    claimed = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["implement"],
        check_time=night, backend="codex",
    )
    assert claimed is not None


def test_claim_both_allowed_at_20_utc(pg_conn):
    check = datetime(2026, 4, 11, 20, 0, 0, tzinfo=UTC)
    _enqueue_runnable(pg_conn, check_time=check, job_type="feedback",
                      dedup_key="c:20:f", payload={"repo": "x/y"}, priority=10)
    _enqueue_runnable(pg_conn, check_time=check, job_type="review",
                      dedup_key="c:20:r", payload={"repo": "x/y"}, priority=30)
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
    check = datetime(2026, 4, 11, 20, 0, 0, tzinfo=UTC)  # both windows open
    _enqueue_runnable(pg_conn, check_time=check, job_type="feedback",
                      dedup_key="c:pf:f", payload={"repo": "x/y"}, priority=10)
    _enqueue_runnable(pg_conn, check_time=check, job_type="review",
                      dedup_key="c:pf:r", payload={"repo": "x/y"}, priority=30)
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["review"],  # only allow review
        check_time=check,
    )
    assert job is not None
    assert job["type"] == "review"


def test_claim_empty_allowed_types_returns_nothing(pg_conn):
    check = datetime(2026, 4, 11, 20, 0, 0, tzinfo=UTC)
    _enqueue_runnable(pg_conn, check_time=check, job_type="feedback",
                      dedup_key="c:empty", payload={"repo": "x/y"})
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


def test_released_job_remains_window_gated(pg_conn):
    """Regression: release_job does NOT clamp run_after to the next window,
    but Gate 2 (the SQL window predicate) still blocks the release from
    being claimed out-of-window. This is the "retry/release/requeue paths
    rely on Gate 2 for correctness" invariant from the spec.
    """
    inside = datetime(2026, 4, 11, 20, 0, 0, tzinfo=UTC)  # own window open
    outside = datetime(2026, 4, 11, 10, 0, 0, tzinfo=UTC)  # own window closed
    _enqueue_runnable(pg_conn, check_time=inside, job_type="feedback",
                      dedup_key="c:released", payload={"repo": "x/y"})
    job = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback"],
        check_time=inside, backend="claude",
    )
    assert job is not None
    db.release_job(pg_conn, job["id"], run_after_seconds=0)
    cur = pg_conn.cursor()
    cur.execute(
        "UPDATE jobs SET run_after = %s WHERE id = %s",
        (outside - timedelta(days=1), job["id"]),
    )
    pg_conn.commit()
    cur.close()
    reclaimed = db.claim_next_job(
        pg_conn, worker_pid=1234,
        allowed_types=["feedback"],
        check_time=outside, backend="claude",
    )
    assert reclaimed is None
