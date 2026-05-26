"""Tests for the job_attempts.backend column + record_attempt threading it."""
from datetime import datetime, timezone

import db


def _seed_job(conn) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO jobs (type, dedup_key, payload, status) "
        "VALUES (%s, %s, %s::jsonb, 'pending') RETURNING id",
        ("review", "test:dedup:1", '{"pr_number": 1}'),
    )
    job_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return job_id


def test_schema_has_backend_column(pg_conn):
    cur = pg_conn.cursor()
    cur.execute(
        "SELECT column_name, data_type, column_default, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_name = 'job_attempts' "
        "AND column_name = 'backend' "
        "AND table_schema = current_schema()"
    )
    row = cur.fetchone()
    cur.close()
    assert row is not None
    name, dtype, default, is_nullable = row
    assert dtype == "text"
    assert is_nullable == "NO"
    # default is something like "'claude'::text"
    assert default and "claude" in default


def test_record_attempt_writes_backend_column(pg_conn):
    job_id = _seed_job(pg_conn)
    now = datetime.now(timezone.utc)
    db.record_attempt(
        pg_conn, job_id,
        outcome="success",
        started_at=now, finished_at=now,
        backend="codex",
    )
    cur = pg_conn.cursor()
    cur.execute("SELECT backend FROM job_attempts WHERE job_id = %s", (job_id,))
    rows = cur.fetchall()
    cur.close()
    assert rows == [("codex",)]


def test_migrate_is_idempotent(pg_conn):
    db.migrate(pg_conn)
    db.migrate(pg_conn)
    cur = pg_conn.cursor()
    cur.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'job_attempts' "
        "AND column_name = 'backend' "
        "AND table_schema = current_schema()"
    )
    (count,) = cur.fetchone()
    cur.close()
    assert count == 1
