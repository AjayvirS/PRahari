#!/usr/bin/env python3
"""PostgreSQL database layer for Claudia's event-driven job queue.

Manages the jobs, job_attempts, and webhook_deliveries tables.
Provides atomic job claiming via SELECT ... FOR UPDATE SKIP LOCKED.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

log = logging.getLogger("claudia.db")

DATABASE = "claudia"

# ── Priority mapping ─────────────────────────────────────────────────────────

PRIORITY = {
    "feedback": 10,
    "ci_check": 20,
    "review": 30,
    "hygiene": 40,
    "memory": 50,
    "implement": 60,
}

JOB_TYPES = tuple(PRIORITY.keys())
JOB_STATUSES = ("pending", "processing", "completed", "skipped", "dead_letter", "archived")
ATTEMPT_OUTCOMES = ("success", "transient_failure", "ambiguous", "permanent_skip", "quota_blocked")

# ── Working-hours window SQL literals ─────────────────────────────────────
# SQL window literals — must mirror windows.py's OWN/REVIEW boundaries.
# `tests/test_windows.py` asserts equality so any boundary change keeps
# Python and SQL aligned.
_SQL_OWN_WINDOW_START = "19:01"
_SQL_OWN_WINDOW_END = "07:00"
_SQL_REVIEW_WINDOW_START = "19:01"
_SQL_REVIEW_WINDOW_END = "12:30"

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- Enums (idempotent via DO blocks)
DO $$ BEGIN
    CREATE TYPE job_type AS ENUM ('feedback', 'ci_check', 'review', 'implement', 'hygiene', 'memory');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE job_status AS ENUM ('pending', 'processing', 'completed', 'skipped', 'dead_letter', 'archived');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE attempt_outcome AS ENUM ('success', 'transient_failure', 'ambiguous', 'permanent_skip', 'quota_blocked');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS jobs (
    id BIGSERIAL PRIMARY KEY,
    type job_type NOT NULL,
    dedup_key TEXT NOT NULL,
    priority SMALLINT NOT NULL DEFAULT 50,
    status job_status NOT NULL DEFAULT 'pending',
    payload JSONB NOT NULL DEFAULT '{}',
    retry_count SMALLINT NOT NULL DEFAULT 0,
    max_retries SMALLINT NOT NULL DEFAULT 3,
    ambiguous_count SMALLINT NOT NULL DEFAULT 0,
    error_message TEXT,
    run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    worker_pid INTEGER
);

CREATE TABLE IF NOT EXISTS job_attempts (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id),
    attempt_number SMALLINT NOT NULL,
    outcome attempt_outcome NOT NULL,
    error_message TEXT,
    result_metadata JSONB,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    claude_exit_code SMALLINT,
    cost_usd NUMERIC(8,4),
    tokens_in INTEGER,
    tokens_out INTEGER,
    backend TEXT NOT NULL DEFAULT 'claude',
    UNIQUE(job_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type TEXT NOT NULL,
    action TEXT
);

-- Indexes (idempotent)
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedup
    ON jobs(dedup_key) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_jobs_pickup
    ON jobs(priority, created_at) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_jobs_lease
    ON jobs(lease_expires_at) WHERE status = 'processing';

CREATE INDEX IF NOT EXISTS idx_attempts_job
    ON job_attempts(job_id);

-- ── Slack review-request delivery state ───────────────────────────────
CREATE TABLE IF NOT EXISTS pr_review_announcements (
    repo         TEXT        NOT NULL,
    pr_number    INTEGER     NOT NULL,
    session_day  DATE        NOT NULL,
    status       TEXT        NOT NULL CHECK (status IN ('posting','posted')),
    claim_token  UUID        NOT NULL,
    claimed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_at    TIMESTAMPTZ,
    slack_ts     TEXT,
    last_error   TEXT,
    PRIMARY KEY (repo, pr_number, session_day)
);

CREATE INDEX IF NOT EXISTS idx_pr_review_announcements_session
    ON pr_review_announcements(session_day);

CREATE TABLE IF NOT EXISTS pr_review_digests (
    session_day  DATE        PRIMARY KEY,
    status       TEXT        NOT NULL CHECK (status IN ('posting','posted')),
    claim_token  UUID        NOT NULL,
    claimed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_at    TIMESTAMPTZ,
    slack_ts     TEXT,
    pr_count     INTEGER,
    partial      BOOLEAN     NOT NULL DEFAULT FALSE,
    last_error   TEXT
);
"""

# ── Connection ────────────────────────────────────────────────────────────────


def _parse_pgpass() -> dict[str, str]:
    """Parse ~/.pgpass for claudia_agent credentials."""
    pgpass = Path.home() / ".pgpass"
    if not pgpass.is_file():
        return {}
    for line in pgpass.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) >= 5:
            host, port, db, user, password = parts[0], parts[1], parts[2], parts[3], ":".join(parts[4:])
            if user == "claudia_agent" and (db in (DATABASE, "*")):
                return {
                    "host": host if host != "*" else "localhost",
                    "port": port if port != "*" else "5432",
                    "dbname": DATABASE,
                    "user": user,
                    "password": password,
                }
    return {}


def connect() -> psycopg2.extensions.connection:
    """Connect to the claudia database using ~/.pgpass credentials."""
    params = _parse_pgpass()
    if not params:
        # Fall back to env var or defaults
        dsn = os.environ.get("DATABASE_URL")
        if dsn:
            conn = psycopg2.connect(dsn)
        else:
            conn = psycopg2.connect(
                dbname=DATABASE, user="claudia_agent", host="localhost"
            )
    else:
        conn = psycopg2.connect(**params)
    conn.autocommit = False
    return conn


def ensure_database() -> None:
    """Create the claudia database if it doesn't exist."""
    params = _parse_pgpass()
    connect_params = {
        "dbname": "postgres",
        "user": params.get("user", "claudia_agent"),
        "host": params.get("host", "localhost"),
        "port": params.get("port", "5432"),
    }
    if "password" in params:
        connect_params["password"] = params["password"]

    conn = psycopg2.connect(**connect_params)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,))
    if not cur.fetchone():
        cur.execute(f'CREATE DATABASE {DATABASE} OWNER claudia_agent')
        log.info("Created database: %s", DATABASE)
    cur.close()
    conn.close()


def migrate(conn: psycopg2.extensions.connection) -> None:
    """Run schema migrations (idempotent)."""
    cur = conn.cursor()
    cur.execute(SCHEMA_SQL)
    cur.execute("""
        ALTER TABLE job_attempts
            ADD COLUMN IF NOT EXISTS backend TEXT NOT NULL DEFAULT 'claude'
    """)
    conn.commit()
    cur.close()
    log.info("Schema migration complete")


# ── Job operations ────────────────────────────────────────────────────────────


def enqueue_job(
    conn: psycopg2.extensions.connection,
    job_type: str,
    dedup_key: str,
    payload: dict[str, Any],
    debounce_seconds: int = 60,
    priority: int | None = None,
    min_run_after: datetime | None = None,
    bypass_window: bool = False,
) -> int | None:
    """Insert a job with ON CONFLICT debounce. Returns job ID or None if coalesced.

    `min_run_after` is an advisory lower bound from Gate 1 (the working-hours
    clamp). It can only push `run_after` forward — never earlier — and never
    regresses an existing backoff-deferred row in the ON CONFLICT path.

    `bypass_window` marks the job as an on-demand override that skips
    working-hours gating. The flag is stored inside the JSONB payload and is
    sticky. Coalesce semantics:
      - fresh INSERT with bypass=True: run_after ≈ now() (debounce forced 0).
      - coalesce onto a NON-bypass existing row: run_after pulled back via
        LEAST(jobs.run_after, now()) so a prior window clamp is undone.
      - coalesce onto a row that ALREADY has bypass=True: run_after is
        preserved unchanged so retry backoff set by `retry_job` cannot be
        collapsed by a later coalescing event.
    """
    if priority is None:
        priority = PRIORITY.get(job_type, 50)
    # Bypass means "run now" — debounce consolidation must not delay it.
    if bypass_window:
        debounce_seconds = 0
        min_run_after = None
    payload = dict(payload)
    payload["bypass_window"] = bool(bypass_window)
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
            run_after = CASE
                WHEN COALESCE((jobs.payload->>'bypass_window')::bool, false)
                    -- Existing row is already bypass: preserve run_after
                    -- unchanged so retry backoff (set by retry_job) is
                    -- respected. A later event must not collapse a
                    -- future backoff target back to now().
                    THEN jobs.run_after
                WHEN COALESCE((EXCLUDED.payload->>'bypass_window')::bool, false)
                    -- Fresh bypass onto a non-bypass existing row: pull
                    -- run_after back to now() so the window clamp the
                    -- prior event applied is undone.
                    THEN LEAST(jobs.run_after, now())
                ELSE GREATEST(
                    jobs.run_after,
                    now() + make_interval(secs := %(debounce)s),
                    COALESCE(%(min_run_after)s, now())
                )
            END,
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
                'conclusion', COALESCE(EXCLUDED.payload->>'conclusion', jobs.payload->>'conclusion'),
                'bypass_window', to_jsonb(
                    COALESCE((EXCLUDED.payload->>'bypass_window')::bool, false)
                    OR COALESCE((jobs.payload->>'bypass_window')::bool, false)
                )
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


def claim_next_job(
    conn: psycopg2.extensions.connection,
    worker_pid: int,
    allowed_types: list[str],
    check_time: datetime | None = None,
    backend: str | None = None,
) -> dict | None:
    """Atomically claim the highest-priority ready job inside its window.

    Two filters apply, in order:

      1. `allowed_types` — a Python-side prefilter (the worker derives
         this from `windows.is_allowed_now(t, now)`). An empty list
         claims nothing.

      2. The SQL CASE below — the authoritative window gate. Lives here
         so the time-of-day check shares a single `check_ts` clock
         snapshot with the `run_after` comparison (no Python↔SQL clock
         race at window boundaries) and so bypass_window reviews can
         claim out-of-window atomically without a separate query.

    `backend` selects the gate policy:
      - "claude" (or None, conservatively): peak-hour gate applies to
        feedback/ci_check/hygiene/memory/review; implement is always
        night-only; review honors bypass_window.
      - "codex":   peak-hour gate disabled; implement still night-only;
        review honors bypass_window (mostly unused since review is 24/7
        already, but kept for parity).

    Keeping the SQL gate plus the Python gate is intentional defense in
    depth: the Python check produces a clean Slack/log story for
    "sleeping until 19:01"; the SQL check makes it safe at the boundary.
    """
    if not allowed_types:
        return None
    if backend is None:
        backend = "claude"  # conservative default — full gating
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
            SELECT jobs.id FROM jobs, check_ts, tod
            WHERE status = 'pending'
              AND run_after <= check_ts.ts
              AND type = ANY(%(allowed_types)s::job_type[])
              AND CASE
                    -- bypass_window reviews skip every gate
                    WHEN type = 'review'::job_type
                         AND COALESCE((jobs.payload->>'bypass_window')::bool, false)
                         THEN TRUE
                    -- implement is workflow-gated regardless of backend
                    WHEN type = 'implement'::job_type
                         THEN (tod.t >= TIME '19:01' OR tod.t < TIME '07:00')
                    -- peak-hour gate skipped under codex (no OpenAI peak hours)
                    WHEN %(backend)s = 'codex'
                         THEN TRUE
                    -- claude: peak-hour gate by job type
                    WHEN type IN ('feedback'::job_type, 'ci_check'::job_type,
                                  'hygiene'::job_type, 'memory'::job_type)
                         THEN (tod.t >= TIME '19:01' OR tod.t < TIME '07:00')
                    WHEN type = 'review'::job_type
                         THEN (tod.t >= TIME '19:01' OR tod.t < TIME '12:30')
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
            "backend": backend,
            "check_time": check_time,
        },
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    if row:
        return dict(row)
    return None


def complete_job(conn: psycopg2.extensions.connection, job_id: int) -> None:
    """Mark a job as completed."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE jobs SET
            status = 'completed',
            completed_at = now(),
            updated_at = now()
        WHERE id = %s
        """,
        (job_id,),
    )
    conn.commit()
    cur.close()


def skip_job(conn: psycopg2.extensions.connection, job_id: int, reason: str) -> None:
    """Mark a job as permanently skipped."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE jobs SET
            status = 'skipped',
            error_message = %s,
            completed_at = now(),
            updated_at = now()
        WHERE id = %s
        """,
        (reason, job_id),
    )
    conn.commit()
    cur.close()


def retry_job(
    conn: psycopg2.extensions.connection,
    job_id: int,
    error: str,
    backoff_seconds: int = 60,
) -> str:
    """Release a job back to pending with backoff, or dead_letter if exhausted.

    Returns the new status: 'pending' or 'dead_letter'.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT retry_count, max_retries FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
    row = cur.fetchone()
    if not row:
        conn.commit()
        cur.close()
        return "dead_letter"

    new_retry = row["retry_count"] + 1
    if new_retry >= row["max_retries"]:
        cur.execute(
            """
            UPDATE jobs SET
                status = 'dead_letter',
                retry_count = %s,
                error_message = %s,
                completed_at = now(),
                updated_at = now()
            WHERE id = %s
            """,
            (new_retry, error, job_id),
        )
        conn.commit()
        cur.close()
        return "dead_letter"

    cur.execute(
        """
        UPDATE jobs SET
            status = 'pending',
            retry_count = %s,
            error_message = %s,
            run_after = now() + make_interval(secs := %s),
            claimed_at = NULL,
            started_at = NULL,
            heartbeat_at = NULL,
            lease_expires_at = NULL,
            worker_pid = NULL,
            updated_at = now()
        WHERE id = %s
        """,
        (new_retry, error, backoff_seconds, job_id),
    )
    conn.commit()
    cur.close()
    return "pending"


def ambiguous_job(
    conn: psycopg2.extensions.connection,
    job_id: int,
    error: str,
    backoff_seconds: int = 120,
) -> str:
    """Handle an ambiguous outcome (work done but unclear success).

    Returns the new status: 'pending' or 'dead_letter'.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT ambiguous_count FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
    row = cur.fetchone()
    if not row:
        conn.commit()
        cur.close()
        return "dead_letter"

    new_count = row["ambiguous_count"] + 1
    if new_count >= 2:
        cur.execute(
            """
            UPDATE jobs SET
                status = 'dead_letter',
                ambiguous_count = %s,
                error_message = %s,
                completed_at = now(),
                updated_at = now()
            WHERE id = %s
            """,
            (new_count, error, job_id),
        )
        conn.commit()
        cur.close()
        return "dead_letter"

    cur.execute(
        """
        UPDATE jobs SET
            status = 'pending',
            ambiguous_count = %s,
            error_message = %s,
            run_after = now() + make_interval(secs := %s),
            claimed_at = NULL,
            started_at = NULL,
            heartbeat_at = NULL,
            lease_expires_at = NULL,
            worker_pid = NULL,
            updated_at = now()
        WHERE id = %s
        """,
        (new_count, error, backoff_seconds, job_id),
    )
    conn.commit()
    cur.close()
    return "pending"


def dead_letter_job(conn: psycopg2.extensions.connection, job_id: int, error: str) -> None:
    """Move a job directly to dead_letter."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE jobs SET
            status = 'dead_letter',
            error_message = %s,
            completed_at = now(),
            updated_at = now()
        WHERE id = %s
        """,
        (error, job_id),
    )
    conn.commit()
    cur.close()


def release_job(conn: psycopg2.extensions.connection, job_id: int, run_after_seconds: int = 0) -> None:
    """Release a processing job back to pending (e.g., quota backpressure)."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE jobs SET
            status = 'pending',
            claimed_at = NULL,
            started_at = NULL,
            heartbeat_at = NULL,
            lease_expires_at = NULL,
            worker_pid = NULL,
            run_after = now() + make_interval(secs := %s),
            updated_at = now()
        WHERE id = %s
        """,
        (run_after_seconds, job_id),
    )
    conn.commit()
    cur.close()


def get_job_status(conn: psycopg2.extensions.connection, job_id: int) -> str | None:
    """Return current jobs.status string, or None if job not found."""
    cur = conn.cursor()
    cur.execute("SELECT status::text FROM jobs WHERE id = %s", (job_id,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def requeue_job(conn: psycopg2.extensions.connection, job_id: int) -> bool:
    """Requeue a dead_letter job back to pending. Returns True if successful."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE jobs SET
            status = 'pending',
            retry_count = 0,
            ambiguous_count = 0,
            error_message = NULL,
            run_after = now(),
            claimed_at = NULL,
            started_at = NULL,
            heartbeat_at = NULL,
            lease_expires_at = NULL,
            worker_pid = NULL,
            updated_at = now()
        WHERE id = %s AND status = 'dead_letter'
        """,
        (job_id,),
    )
    affected = cur.rowcount
    conn.commit()
    cur.close()
    return affected > 0


def update_heartbeat(conn: psycopg2.extensions.connection, job_id: int) -> None:
    """Extend the lease for a processing job."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE jobs SET
            heartbeat_at = now(),
            lease_expires_at = now() + interval '90 minutes',
            updated_at = now()
        WHERE id = %s AND status = 'processing'
        """,
        (job_id,),
    )
    conn.commit()
    cur.close()


def recover_stale_jobs(conn: psycopg2.extensions.connection) -> int:
    """Move processing jobs with expired leases back to pending. Returns count.

    If a pending job already exists for the same dedup_key, the stale job
    is sent to dead_letter instead (the pending one will handle the work).
    """
    cur = conn.cursor()
    # First, dead_letter stale jobs that would conflict with existing pending jobs
    cur.execute(
        """
        UPDATE jobs SET
            status = 'dead_letter',
            error_message = 'Lease expired — superseded by pending job',
            updated_at = now()
        WHERE status = 'processing' AND lease_expires_at < now()
            AND dedup_key IN (
                SELECT dedup_key FROM jobs WHERE status = 'pending'
            )
        """
    )
    dead = cur.rowcount
    # Then recover the rest back to pending
    cur.execute(
        """
        UPDATE jobs SET
            status = 'pending',
            error_message = 'Lease expired — recovered',
            claimed_at = NULL,
            started_at = NULL,
            heartbeat_at = NULL,
            lease_expires_at = NULL,
            worker_pid = NULL,
            updated_at = now()
        WHERE status = 'processing' AND lease_expires_at < now()
        """
    )
    count = cur.rowcount
    conn.commit()
    cur.close()
    return count + dead


# ── Job attempts ──────────────────────────────────────────────────────────────


def record_attempt(
    conn: psycopg2.extensions.connection,
    job_id: int,
    outcome: str,
    started_at: datetime,
    finished_at: datetime,
    error_message: str | None = None,
    result_metadata: dict | None = None,
    claude_exit_code: int | None = None,
    cost_usd: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    backend: str = "claude",
) -> int:
    """Record a job attempt in the audit trail. Returns attempt ID.

    attempt_number is auto-derived from MAX(attempt_number)+1 for the job,
    which is safe across retries, ambiguous outcomes, and requeue operations.
    """
    cur = conn.cursor()
    # Lock the job row to prevent concurrent attempt number races
    cur.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
    cur.execute(
        """
        INSERT INTO job_attempts
            (job_id, attempt_number, outcome, error_message, result_metadata,
             started_at, finished_at, claude_exit_code, cost_usd,
             tokens_in, tokens_out, backend)
        VALUES (
            %s,
            COALESCE((SELECT MAX(attempt_number) FROM job_attempts WHERE job_id = %s), 0) + 1,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        RETURNING id
        """,
        (
            job_id, job_id, outcome, error_message,
            json.dumps(result_metadata) if result_metadata else None,
            started_at, finished_at, claude_exit_code, cost_usd,
            tokens_in, tokens_out, backend,
        ),
    )
    attempt_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return attempt_id


# ── Webhook deliveries ────────────────────────────────────────────────────────


def check_delivery(conn: psycopg2.extensions.connection, delivery_id: str) -> bool:
    """Check if a webhook delivery has already been processed. Returns True if duplicate."""
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM webhook_deliveries WHERE delivery_id = %s", (delivery_id,))
    exists = cur.fetchone() is not None
    cur.close()
    return exists


def record_delivery(
    conn: psycopg2.extensions.connection,
    delivery_id: str,
    event_type: str,
    action: str | None = None,
) -> bool:
    """Record a webhook delivery (idempotent). Returns True if this
    call actually inserted the row, False if a concurrent/prior row
    already existed. The caller MUST use this return value to decide
    whether to run user-visible side effects (posting comments,
    sending Slack messages) so that a concurrent retry of the same
    delivery_id does not cause duplicate external writes.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO webhook_deliveries (delivery_id, event_type, action)
        VALUES (%s, %s, %s)
        ON CONFLICT (delivery_id) DO NOTHING
        RETURNING 1
        """,
        (delivery_id, event_type, action),
    )
    inserted = cur.fetchone() is not None
    # Note: caller manages commit (part of a transaction)
    cur.close()
    return inserted


# ── Cleanup & stats ──────────────────────────────────────────────────────────


def cleanup_old_jobs(conn: psycopg2.extensions.connection, days: int = 90) -> int:
    """Archive old completed/skipped/dead_letter jobs. Returns count."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE jobs SET
            status = 'archived',
            updated_at = now()
        WHERE status IN ('completed', 'skipped', 'dead_letter')
          AND completed_at < now() - make_interval(days := %s)
        """,
        (days,),
    )
    count = cur.rowcount
    conn.commit()
    cur.close()
    return count


def cleanup_old_deliveries(conn: psycopg2.extensions.connection, days: int = 30) -> int:
    """Remove old webhook delivery records. Returns count."""
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM webhook_deliveries
        WHERE received_at < now() - make_interval(days := %s)
        """,
        (days,),
    )
    count = cur.rowcount
    conn.commit()
    cur.close()
    return count


# ── Slack review-request delivery state ──────────────────────────────────

def claim_pr_review_slot(
    conn: psycopg2.extensions.connection,
    repo: str,
    pr_number: int,
    session_day,
) -> uuid.UUID | None:
    """Claim a per-PR review-announcement slot. Returns a claim token or None.

    Short transaction: a single INSERT ON CONFLICT DO NOTHING that commits
    immediately — no LLM or Slack work runs inside the tx.
    """
    token = uuid.uuid4()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO pr_review_announcements
            (repo, pr_number, session_day, status, claim_token)
        VALUES (%s, %s, %s, 'posting', %s)
        ON CONFLICT DO NOTHING
        RETURNING claim_token
        """,
        (repo, pr_number, session_day, str(token)),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    return token if row else None


def finalize_pr_review_slot(
    conn, repo, pr_number, session_day, claim_token, *, slack_ts: str
) -> bool:
    """Mark the slot as posted. Returns True if the row matched the token."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE pr_review_announcements
        SET status = 'posted', posted_at = now(), slack_ts = %s
        WHERE repo = %s AND pr_number = %s AND session_day = %s
          AND claim_token = %s AND status = 'posting'
        """,
        (slack_ts, repo, pr_number, session_day, str(claim_token)),
    )
    ok = cur.rowcount == 1
    conn.commit()
    cur.close()
    return ok


def release_pr_review_slot(
    conn, repo, pr_number, session_day, claim_token
) -> bool:
    """Delete the claimed-but-unposted row. Returns True if deleted."""
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM pr_review_announcements
        WHERE repo = %s AND pr_number = %s AND session_day = %s
          AND claim_token = %s AND status = 'posting'
        """,
        (repo, pr_number, session_day, str(claim_token)),
    )
    deleted = cur.rowcount == 1
    conn.commit()
    cur.close()
    return deleted


def claim_pr_review_digest(conn, session_day) -> uuid.UUID | None:
    """Claim the digest slot for a session_day. Returns token or None."""
    token = uuid.uuid4()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO pr_review_digests (session_day, status, claim_token)
        VALUES (%s, 'posting', %s)
        ON CONFLICT DO NOTHING
        RETURNING claim_token
        """,
        (session_day, str(token)),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    return token if row else None


def finalize_pr_review_digest(
    conn, session_day, claim_token, *, slack_ts: str, pr_count: int, partial: bool
) -> bool:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE pr_review_digests
        SET status='posted', posted_at=now(), slack_ts=%s,
            pr_count=%s, partial=%s
        WHERE session_day=%s AND claim_token=%s AND status='posting'
        """,
        (slack_ts, pr_count, partial, session_day, str(claim_token)),
    )
    ok = cur.rowcount == 1
    conn.commit()
    cur.close()
    return ok


def release_pr_review_digest(conn, session_day, claim_token) -> bool:
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM pr_review_digests
        WHERE session_day=%s AND claim_token=%s AND status='posting'
        """,
        (session_day, str(claim_token)),
    )
    deleted = cur.rowcount == 1
    conn.commit()
    cur.close()
    return deleted


def mark_digest_posted_empty(conn, session_day) -> bool:
    """Atomic empty-and-complete path: insert a posted row with pr_count=0.

    Returns True if the row was inserted, False if a row already existed.
    """
    token = uuid.uuid4()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO pr_review_digests
            (session_day, status, claim_token, posted_at, pr_count, partial)
        VALUES (%s, 'posted', %s, now(), 0, FALSE)
        ON CONFLICT DO NOTHING
        RETURNING session_day
        """,
        (session_day, str(token)),
    )
    inserted = cur.fetchone() is not None
    conn.commit()
    cur.close()
    return inserted


def cleanup_old_review_rows(conn, days: int = 60) -> int:
    """Delete posted review-announcement and digest rows older than `days`.

    `posting` rows are intentionally left alone so stuck cases remain visible.
    Returns the total number of rows deleted across both tables.
    """
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM pr_review_announcements
        WHERE status = 'posted'
          AND posted_at < now() - make_interval(days := %s)
        """,
        (days,),
    )
    total = cur.rowcount
    cur.execute(
        """
        DELETE FROM pr_review_digests
        WHERE status = 'posted'
          AND posted_at < now() - make_interval(days := %s)
        """,
        (days,),
    )
    total += cur.rowcount
    conn.commit()
    cur.close()
    return total


def drain_all(conn: psycopg2.extensions.connection) -> int:
    """Emergency: move all pending jobs to dead_letter. Returns count."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE jobs SET
            status = 'dead_letter',
            error_message = 'Drained by operator',
            completed_at = now(),
            updated_at = now()
        WHERE status = 'pending'
        """
    )
    count = cur.rowcount
    conn.commit()
    cur.close()
    return count


def queue_status(conn: psycopg2.extensions.connection) -> dict[str, int]:
    """Get job counts by status."""
    cur = conn.cursor()
    cur.execute("SELECT status::text, count(*) FROM jobs GROUP BY status")
    result = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    return result


def pending_by_type(conn: psycopg2.extensions.connection) -> dict[str, int]:
    """Get pending job counts by type."""
    cur = conn.cursor()
    cur.execute(
        "SELECT type::text, count(*) FROM jobs WHERE status = 'pending' GROUP BY type"
    )
    result = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    return result


def get_job(conn: psycopg2.extensions.connection, job_id: int) -> dict | None:
    """Fetch a single job by ID."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def has_pending_job(conn: psycopg2.extensions.connection, dedup_key: str) -> bool:
    """Check if a pending job exists for the given dedup_key."""
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM jobs WHERE dedup_key = %s AND status = 'pending'",
        (dedup_key,),
    )
    exists = cur.fetchone() is not None
    cur.close()
    return exists


def has_finished_job(conn: psycopg2.extensions.connection, dedup_key: str) -> bool:
    """Check if a completed or skipped job exists for the given dedup_key.

    Used by poll_github to avoid re-enqueuing work that was already handled
    (e.g., CI failures that Claude decided to skip as flaky).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM jobs WHERE dedup_key = %s AND status IN ('completed', 'skipped')",
        (dedup_key,),
    )
    exists = cur.fetchone() is not None
    cur.close()
    return exists


def pending_ready_bypass_review_exists(
    conn: psycopg2.extensions.connection,
    check_time: datetime | None = None,
) -> bool:
    """True if a pending review row with `bypass_window=true` is claimable now.

    "Claimable" here means status='pending' AND run_after <= now() AND
    the bypass flag is set. The worker uses this to decide whether to add
    `review` to its `claim_allowed` set outside the review window.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1 FROM jobs
        WHERE status = 'pending'
          AND type = 'review'::job_type
          AND run_after <= COALESCE(%s, now())
          AND COALESCE((payload->>'bypass_window')::bool, false)
        LIMIT 1
        """,
        (check_time,),
    )
    exists = cur.fetchone() is not None
    cur.close()
    return exists


def pending_any_bypass_review_exists(
    conn: psycopg2.extensions.connection,
) -> bool:
    """True if any pending review row has `bypass_window=true`, ready or not.

    Used by the worker to silence the "sleeping until 19:01 UTC" nap
    announce when a bypass review is still in debounce/backoff: the row
    exists but `run_after` is in the future, so `pending_ready_bypass_*`
    is false. We still want to nap silently (debounce) rather than
    window-blocked with a Slack announce.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1 FROM jobs
        WHERE status = 'pending'
          AND type = 'review'::job_type
          AND COALESCE((payload->>'bypass_window')::bool, false)
        LIMIT 1
        """
    )
    exists = cur.fetchone() is not None
    cur.close()
    return exists
