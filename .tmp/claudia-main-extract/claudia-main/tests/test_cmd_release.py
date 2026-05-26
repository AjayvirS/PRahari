"""Tests for the `release` subcommand."""
from datetime import datetime, timezone

import db
import worker


def _seed_job(conn, status="processing") -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO jobs (type, dedup_key, payload, status) "
        "VALUES (%s, %s, %s::jsonb, %s::job_status) RETURNING id",
        ("review", f"release:{status}", '{"pr_number": 1}', status),
    )
    job_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return job_id


def test_get_job_status_returns_str(pg_conn):
    job_id = _seed_job(pg_conn, status="processing")
    assert db.get_job_status(pg_conn, job_id) == "processing"


def test_get_job_status_missing(pg_conn):
    assert db.get_job_status(pg_conn, 9999999) is None


def test_release_processing_job(pg_conn, capsys):
    job_id = _seed_job(pg_conn, status="processing")
    rc = worker.cmd_release(pg_conn, job_id, force=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Released job" in out
    assert db.get_job_status(pg_conn, job_id) == "pending"


def test_release_pending_job_without_force_refuses(pg_conn, capsys):
    job_id = _seed_job(pg_conn, status="pending")
    rc = worker.cmd_release(pg_conn, job_id, force=False)
    assert rc == 1
    assert db.get_job_status(pg_conn, job_id) == "pending"
    assert "not 'processing'" in capsys.readouterr().out


def test_release_pending_job_with_force_succeeds(pg_conn):
    job_id = _seed_job(pg_conn, status="pending")
    rc = worker.cmd_release(pg_conn, job_id, force=True)
    assert rc == 0
    assert db.get_job_status(pg_conn, job_id) == "pending"


def test_release_missing_job_returns_1(pg_conn, capsys):
    rc = worker.cmd_release(pg_conn, 999999, force=False)
    assert rc == 1
    assert "not found" in capsys.readouterr().out
