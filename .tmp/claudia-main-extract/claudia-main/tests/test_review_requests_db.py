"""Postgres tests for pr_review_announcements / pr_review_digests helpers."""
import uuid
from datetime import date, timedelta

import pytest

import db as claudia_db


REPO = "ls1intum/Artemis"
SDAY = date(2026, 4, 10)


def test_claim_first_caller_wins(pg_conn):
    token = claudia_db.claim_pr_review_slot(pg_conn, REPO, 1234, SDAY)
    assert isinstance(token, uuid.UUID)
    second = claudia_db.claim_pr_review_slot(pg_conn, REPO, 1234, SDAY)
    assert second is None

def test_claim_different_pr_succeeds(pg_conn):
    claudia_db.claim_pr_review_slot(pg_conn, REPO, 1234, SDAY)
    assert claudia_db.claim_pr_review_slot(pg_conn, REPO, 1235, SDAY) is not None

def test_claim_different_session_day_succeeds(pg_conn):
    claudia_db.claim_pr_review_slot(pg_conn, REPO, 1234, SDAY)
    assert claudia_db.claim_pr_review_slot(
        pg_conn, REPO, 1234, SDAY + timedelta(days=1)
    ) is not None

def test_finalize_matching_token_marks_posted(pg_conn):
    token = claudia_db.claim_pr_review_slot(pg_conn, REPO, 1, SDAY)
    ok = claudia_db.finalize_pr_review_slot(
        pg_conn, REPO, 1, SDAY, token, slack_ts="1712000000.000100"
    )
    assert ok is True
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT status, slack_ts FROM pr_review_announcements "
            "WHERE repo=%s AND pr_number=%s AND session_day=%s",
            (REPO, 1, SDAY),
        )
        row = cur.fetchone()
    assert row == ("posted", "1712000000.000100")

def test_finalize_stale_token_is_noop(pg_conn):
    claudia_db.claim_pr_review_slot(pg_conn, REPO, 1, SDAY)
    ok = claudia_db.finalize_pr_review_slot(
        pg_conn, REPO, 1, SDAY, uuid.uuid4(), slack_ts="x"
    )
    assert ok is False

def test_release_deletes_posting_row(pg_conn):
    token = claudia_db.claim_pr_review_slot(pg_conn, REPO, 1, SDAY)
    deleted = claudia_db.release_pr_review_slot(pg_conn, REPO, 1, SDAY, token)
    assert deleted is True
    assert claudia_db.claim_pr_review_slot(pg_conn, REPO, 1, SDAY) is not None

def test_release_leaves_posted_row_alone(pg_conn):
    token = claudia_db.claim_pr_review_slot(pg_conn, REPO, 1, SDAY)
    claudia_db.finalize_pr_review_slot(pg_conn, REPO, 1, SDAY, token, slack_ts="t")
    deleted = claudia_db.release_pr_review_slot(pg_conn, REPO, 1, SDAY, token)
    assert deleted is False

def test_concurrent_claim_race():
    """Race test uses the real `public` schema because psycopg2 connections
    from separate threads can't share the per-test search_path."""
    import threading

    RACE_REPO = "race-test/Foo"
    RACE_PR = 999_001
    RACE_DAY = date(2026, 1, 1)

    try:
        primary = claudia_db.connect()
    except Exception as exc:
        pytest.skip(f"Postgres not reachable: {exc}")

    try:
        claudia_db.migrate(primary)
        with primary.cursor() as cur:
            cur.execute(
                "DELETE FROM pr_review_announcements "
                "WHERE repo=%s AND pr_number=%s AND session_day=%s",
                (RACE_REPO, RACE_PR, RACE_DAY),
            )
        primary.commit()

        results: list = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def worker():
            c = claudia_db.connect()
            try:
                barrier.wait()
                token = claudia_db.claim_pr_review_slot(
                    c, RACE_REPO, RACE_PR, RACE_DAY
                )
                with lock:
                    results.append(token)
            finally:
                c.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(); t2.join()
        assert sum(r is not None for r in results) == 1
    finally:
        try:
            with primary.cursor() as cur:
                cur.execute(
                    "DELETE FROM pr_review_announcements "
                    "WHERE repo=%s AND pr_number=%s AND session_day=%s",
                    (RACE_REPO, RACE_PR, RACE_DAY),
                )
            primary.commit()
        finally:
            primary.close()


def test_digest_claim_first_caller_wins(pg_conn):
    assert claudia_db.claim_pr_review_digest(pg_conn, SDAY) is not None
    assert claudia_db.claim_pr_review_digest(pg_conn, SDAY) is None

def test_digest_claim_different_day_succeeds(pg_conn):
    claudia_db.claim_pr_review_digest(pg_conn, SDAY)
    assert claudia_db.claim_pr_review_digest(pg_conn, SDAY + timedelta(days=1)) is not None

def test_digest_finalize_marks_posted_with_counts(pg_conn):
    token = claudia_db.claim_pr_review_digest(pg_conn, SDAY)
    ok = claudia_db.finalize_pr_review_digest(
        pg_conn, SDAY, token, slack_ts="t1", pr_count=3, partial=True
    )
    assert ok
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT status, pr_count, partial FROM pr_review_digests WHERE session_day=%s",
            (SDAY,),
        )
        assert cur.fetchone() == ("posted", 3, True)

def test_digest_release_deletes_posting_row(pg_conn):
    token = claudia_db.claim_pr_review_digest(pg_conn, SDAY)
    assert claudia_db.release_pr_review_digest(pg_conn, SDAY, token) is True

def test_digest_empty_and_complete_short_circuit(pg_conn):
    """The empty-and-complete path does its own claim + finalize in one call."""
    ok = claudia_db.mark_digest_posted_empty(pg_conn, SDAY)
    assert ok is True
    # Second call must be a no-op because the row now exists.
    assert claudia_db.mark_digest_posted_empty(pg_conn, SDAY) is False

def test_retention_deletes_old_posted_rows_only(pg_conn):
    with pg_conn.cursor() as cur:
        # Old posted row → should be deleted.
        cur.execute(
            """
            INSERT INTO pr_review_announcements
              (repo, pr_number, session_day, status, claim_token,
               claimed_at, posted_at)
            VALUES (%s, %s, %s, 'posted', %s,
                    now() - interval '61 days',
                    now() - interval '61 days')
            """,
            (REPO, 900, date(2026, 2, 1), str(uuid.uuid4())),
        )
        # Old posting row → must be kept (stuck cases stay visible).
        cur.execute(
            """
            INSERT INTO pr_review_announcements
              (repo, pr_number, session_day, status, claim_token,
               claimed_at)
            VALUES (%s, %s, %s, 'posting', %s, now() - interval '61 days')
            """,
            (REPO, 901, date(2026, 2, 1), str(uuid.uuid4())),
        )
        # Recent posted row → must be kept.
        cur.execute(
            """
            INSERT INTO pr_review_announcements
              (repo, pr_number, session_day, status, claim_token,
               claimed_at, posted_at)
            VALUES (%s, %s, %s, 'posted', %s, now(), now())
            """,
            (REPO, 902, SDAY, str(uuid.uuid4())),
        )
        # Old digest rows.
        cur.execute(
            """
            INSERT INTO pr_review_digests
              (session_day, status, claim_token, claimed_at, posted_at,
               pr_count, partial)
            VALUES (%s, 'posted', %s, now() - interval '61 days',
                    now() - interval '61 days', 0, FALSE)
            """,
            (date(2026, 2, 1), str(uuid.uuid4())),
        )
    pg_conn.commit()

    deleted = claudia_db.cleanup_old_review_rows(pg_conn, days=60)
    assert deleted == 2  # old posted announcement + old posted digest

    with pg_conn.cursor() as cur:
        cur.execute("SELECT pr_number FROM pr_review_announcements ORDER BY pr_number")
        remaining = [r[0] for r in cur.fetchall()]
    assert 900 not in remaining
    assert 901 in remaining  # posting row kept
    assert 902 in remaining  # recent row kept
