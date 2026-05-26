# Slack review-request notifications — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claudia posts per-PR review requests and a daily digest into `#artemistest` whenever she lands real code on her own PRs.

**Architecture:** Short-transaction delivery-state machine (`claim → draft → validate → post → finalize/release`). Two new inline agents (`review-announcer`, `review-digest`) draft human-sounding messages; the worker validates and posts via a new structured `slack_post()` helper that classifies failures as `ok` / `definite_failure` / `ambiguous_failure`. Per-PR and digest delivery each have their own delivery-state table with a short-tx claim; external work (LLM, Slack) happens outside any DB transaction. Template fallback guarantees that agent/validator failure can never silently drop a notification.

**Tech Stack:** Python 3, PostgreSQL (psycopg2), Claude Code CLI (inline invocation), Slack Web API (`chat.postMessage`, `conversations.history`, `auth.test`), pytest.

**Spec:** `docs/superpowers/specs/2026-04-10-slack-review-requests-design.md` — read it before starting; this plan does not duplicate its rationale.

---

## File Structure

**New files:**

- `slack_api.py` — `slack_post(text, channel, *, timeout=10.0) -> dict` with error classification. One responsibility: post to Slack and classify the result.
- `inline_agents.py` — `run_inline_agent(agent_name, placeholders, *, expected_type, timeout_seconds=180) -> dict`. One responsibility: run a drafting agent outside the job queue, with strict success criteria.
- `review_requests.py` — classifier, sanitization, enumeration, validators, template fallbacks, and the two orchestrators `_maybe_announce_review` / `_maybe_fire_digest`. Everything review-channel-specific lives here so `worker.py` only needs two hook calls.
- `agents/review-announcer.md` — per-PR drafting agent (Sonnet, no `max_turns`, `cwd=CLAUDIA_DIR`, no overlay).
- `agents/review-digest.md` — digest drafting agent (same profile).
- `tests/test_review_session_day.py` — pure unit tests for `windows.current_own_session_day`.
- `tests/test_slack_post.py` — `slack_post` classification tests (mocked HTTP).
- `tests/test_review_requests.py` — pure unit tests: classifier, sanitizer, validators, template renderers.
- `tests/test_review_requests_db.py` — Postgres tests: claim, finalize, release, digest counterparts, concurrent claim race, retention.
- `tests/test_review_orchestration.py` — orchestrator tests with `run_inline_agent` and `slack_post` mocked at the module boundary.
- `tests/test_worker_digest.py` — fake-clock worker-loop transition test.

**Modified files:**

- `windows.py` — add `current_own_session_day`.
- `db.py` — schema extension + claim/finalize/release helpers + retention helper.
- `worker.py` — post-delta hook call; main-loop `True → False` digest hook; startup init of `was_in_own_window` and `CLAUDIA_BOT_USER_ID`.
- `.env.example`, `README.md` — document `SLACK_REVIEW_CHANNEL`.

**Explicitly NOT touched:**

- `slack.py`, `utils.slack_send()`, `utils.slack_alert()` — unchanged.
- `AGENT_MAP`, `build_agent_prompt` — unchanged; inline agents bypass both.
- Repo `agent-overlay.md` loading — unchanged.

---

## Task 1: `windows.current_own_session_day`

**Files:**
- Modify: `windows.py`
- Test: `tests/test_review_session_day.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_review_session_day.py`:

```python
"""Unit tests for windows.current_own_session_day."""
from datetime import date, datetime, timezone
import pytest
import windows

UTC = timezone.utc

def dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)

@pytest.mark.parametrize("now,expected", [
    (dt(2026, 4, 10, 18, 0),  date(2026, 4,  9)),  # before 19:01 → yesterday
    (dt(2026, 4, 10, 19, 0),  date(2026, 4,  9)),  # one minute before boundary
    (dt(2026, 4, 10, 19, 1),  date(2026, 4, 10)),  # exact boundary → today
    (dt(2026, 4, 10, 23, 59), date(2026, 4, 10)),
    (dt(2026, 4, 11,  0,  0), date(2026, 4, 10)),  # after midnight, still yesterday's session
    (dt(2026, 4, 11,  3, 15), date(2026, 4, 10)),
    (dt(2026, 4, 11,  6, 59), date(2026, 4, 10)),
    (dt(2026, 4, 11,  7,  0), date(2026, 4, 10)),  # window closed but session still yesterday
    (dt(2026, 4, 11,  7,  1), date(2026, 4, 10)),
    (dt(2026, 4, 11, 12,  0), date(2026, 4, 10)),
    (dt(2026, 4, 11, 18, 59), date(2026, 4, 10)),
    (dt(2026, 4, 11, 19,  1), date(2026, 4, 11)),  # next session opens
])
def test_current_own_session_day(now, expected):
    assert windows.current_own_session_day(now) == expected

def test_current_own_session_day_requires_utc():
    with pytest.raises(AssertionError):
        windows.current_own_session_day(datetime(2026, 4, 10, 19, 0))  # naive
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_review_session_day.py -v`
Expected: `AttributeError: module 'windows' has no attribute 'current_own_session_day'`.

- [ ] **Step 3: Implement `current_own_session_day`**

Add to `windows.py` below `next_allowed_for_types`:

```python
from datetime import date

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
```

(`timedelta` is already imported; `date` must be added to the existing `from datetime import ...` line.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_review_session_day.py -v`
Expected: all parametrized cases PASS; `test_current_own_session_day_requires_utc` PASS.

- [ ] **Step 5: Commit**

```bash
git add windows.py tests/test_review_session_day.py
git commit -m "feat(windows): add current_own_session_day helper"
```

---

## Task 2: DB schema — `pr_review_announcements` + `pr_review_digests`

**Files:**
- Modify: `db.py` (extend `SCHEMA_SQL`)
- Test: covered in Task 3

- [ ] **Step 1: Add schema to `SCHEMA_SQL`**

Append to the end of the `SCHEMA_SQL` constant in `db.py` (before the closing `"""`):

```sql

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
```

- [ ] **Step 2: Write a smoke test that actually executes `SCHEMA_SQL`**

Create `tests/test_review_schema_smoke.py` (this will be extended into the full DB test file in Task 3; for Task 2 it exists solely to fail-fast on any SCHEMA_SQL syntax error, since neither `test_windows.py` nor `test_db_windows.py` touches `pg_conn` in a way that executes the new DDL):

```python
"""Smoke test: new review-delivery tables must be creatable via SCHEMA_SQL.

The pg_conn fixture runs SCHEMA_SQL inside a throwaway schema on setup.
If the new DDL is broken, this test blows up immediately.
"""


def test_review_tables_exist_after_migrate(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s), to_regclass(%s)",
            ("pr_review_announcements", "pr_review_digests"),
        )
        ann, dig = cur.fetchone()
    assert ann is not None, "pr_review_announcements missing after SCHEMA_SQL"
    assert dig is not None, "pr_review_digests missing after SCHEMA_SQL"
```

Run: `pytest tests/test_review_schema_smoke.py -v`
Expected: PASS. Any syntax error in the new schema fails here because `SCHEMA_SQL` is executed in the `pg_conn` fixture's setup before the test body runs.

- [ ] **Step 3: Commit**

```bash
git add db.py
git commit -m "feat(db): add pr_review_announcements and pr_review_digests tables"
```

---

## Task 3: DB claim/finalize/release helpers (per-PR)

**Files:**
- Modify: `db.py` (new helpers)
- Test: `tests/test_review_requests_db.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_review_requests_db.py`:

```python
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
    """Race test must use the real `public` schema because psycopg2
    connections from separate threads can't share the per-test search_path
    from the `pg_conn` fixture.

    We:
      1) open a primary connection, run `migrate()` to ensure the tables
         exist in `public`,
      2) pick a (repo, pr_number, session_day) tuple that no other test
         uses and delete any leftover before and after,
      3) open two fresh connections in two threads and race the claim.
    """
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_review_requests_db.py -v`
Expected: `AttributeError: module 'db' has no attribute 'claim_pr_review_slot'`.

- [ ] **Step 3: Implement the helpers**

Add to `db.py` below `cleanup_old_deliveries`:

```python
# ── Slack review-request delivery state ──────────────────────────────────

import uuid as _uuid

def claim_pr_review_slot(
    conn: psycopg2.extensions.connection,
    repo: str,
    pr_number: int,
    session_day,
) -> _uuid.UUID | None:
    """Claim a per-PR review-announcement slot. Returns a claim token or None.

    The whole transaction is a single INSERT ON CONFLICT DO NOTHING so it
    commits immediately — no LLM or Slack work runs inside the tx.
    """
    token = _uuid.uuid4()
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
```

Note: the module-level `import uuid as _uuid` can live at the top with the other imports — the inline import above is just for clarity. Move it to the top-of-file imports during implementation.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_review_requests_db.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_review_requests_db.py
git commit -m "feat(db): add claim/finalize/release helpers for PR review announcements"
```

---

## Task 4: DB helpers for digest delivery

**Files:**
- Modify: `db.py`
- Test: `tests/test_review_requests_db.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_requests_db.py`:

```python
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
```

- [ ] **Step 2: Run tests, verify failing**

Run: `pytest tests/test_review_requests_db.py -v -k digest`
Expected: `AttributeError: module 'db' has no attribute 'claim_pr_review_digest'`.

- [ ] **Step 3: Implement digest helpers**

Add to `db.py`:

```python
def claim_pr_review_digest(conn, session_day) -> _uuid.UUID | None:
    token = _uuid.uuid4()
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
    token = _uuid.uuid4()
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
```

- [ ] **Step 4: Run, verify passing**

Run: `pytest tests/test_review_requests_db.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_review_requests_db.py
git commit -m "feat(db): add digest claim/finalize/release + empty-and-complete helper"
```

---

## Task 5: 60-day retention + wire into 6-hour cleanup path

**Files:**
- Modify: `db.py`, `worker.py`
- Test: `tests/test_review_requests_db.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_review_requests_db.py`:

```python
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
```

- [ ] **Step 2: Run, verify failing**

Run: `pytest tests/test_review_requests_db.py::test_retention_deletes_old_posted_rows_only -v`
Expected: `AttributeError: cleanup_old_review_rows`.

- [ ] **Step 3: Implement retention helper**

Add to `db.py`:

```python
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
```

- [ ] **Step 4: Wire into the 6-hour cleanup path in `worker.py`**

In `worker.py` around line 1944, inside the `if now - cleanup_at > 6 * 3600:` block, after `cleanup_old_deliveries(conn)`:

```python
            try:
                pruned = db.cleanup_old_review_rows(conn)
                if pruned:
                    log.info("Pruned %d old review rows", pruned)
            except Exception as exc:
                log.warning("Review-row cleanup failed: %s", exc)
```

(Do NOT add this to the 5-minute stale-recovery path — review-row cleanup runs on the slower 6h cadence only.)

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_review_requests_db.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add db.py worker.py tests/test_review_requests_db.py
git commit -m "feat(db): 60-day retention for review delivery state, wired into 6h cleanup"
```

---

## Task 6: `slack_api.slack_post` helper

**Files:**
- Create: `slack_api.py`
- Test: `tests/test_slack_post.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_slack_post.py`:

```python
"""Unit tests for slack_api.slack_post classification."""
from unittest.mock import patch, MagicMock
import io
import json
import socket
import urllib.error

import pytest

import slack_api


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")


def _mock_response(status: int, body: dict):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
    return resp


def test_ok_response():
    with patch("slack_api._urlopen", return_value=_mock_response(200, {"ok": True, "ts": "1.2"})):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r == {"result": "ok", "ts": "1.2"}


def test_slack_ok_false_is_definite_failure():
    with patch("slack_api._urlopen", return_value=_mock_response(200, {"ok": False, "error": "channel_not_found"})):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r == {"result": "definite_failure", "error": "channel_not_found"}


def test_http_401_is_definite_failure():
    """urllib.request.urlopen raises HTTPError for 4xx, not a status=401
    response — we must mock that raise path accurately."""
    err = urllib.error.HTTPError(
        url="https://slack.com/api/chat.postMessage",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(json.dumps({"ok": False, "error": "invalid_auth"}).encode()),
    )
    with patch("slack_api._urlopen", side_effect=err):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "definite_failure"
    assert r["error"] == "invalid_auth"


def test_dns_failure_is_definite_failure():
    with patch("slack_api._urlopen", side_effect=socket.gaierror("name resolution failed")):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "definite_failure"
    assert "name resolution" in r["error"].lower() or "gaierror" in r["error"].lower()


def test_connection_refused_is_definite_failure():
    with patch("slack_api._urlopen", side_effect=ConnectionRefusedError("refused")):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "definite_failure"


def test_socket_timeout_is_ambiguous_failure():
    with patch("slack_api._urlopen", side_effect=socket.timeout("read timed out")):
        r = slack_api.slack_post("hi", "C012NFRM76F", timeout=1.0)
    assert r["result"] == "ambiguous_failure"


def test_connection_reset_mid_request_is_ambiguous():
    with patch("slack_api._urlopen", side_effect=ConnectionResetError("reset by peer")):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "ambiguous_failure"


def test_unparseable_body_after_200_is_ambiguous():
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = b"not json"
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
    with patch("slack_api._urlopen", return_value=resp):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "ambiguous_failure"


def test_missing_token_raises():
    import os
    os.environ.pop("SLACK_BOT_TOKEN", None)
    with pytest.raises(RuntimeError):
        slack_api.slack_post("hi", "C012NFRM76F")
```

- [ ] **Step 2: Run, verify failing**

Run: `pytest tests/test_slack_post.py -v`
Expected: `ModuleNotFoundError: No module named 'slack_api'`.

- [ ] **Step 3: Implement `slack_api.py`**

Create `slack_api.py`:

```python
"""In-process Slack posting with structured error classification.

The returned dict has a `result` field:
    - "ok"                 → {"result":"ok","ts":"<slack ts>"}
    - "definite_failure"   → {"result":"definite_failure","error":"<reason>"}
    - "ambiguous_failure"  → {"result":"ambiguous_failure","error":"<reason>"}

Classification follows observable uncertainty: default to ambiguous_failure
whenever we cannot rule out that Slack accepted the message. Do NOT auto-
retry on ambiguous failure.
"""
import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

_SLACK_POST_URL = "https://slack.com/api/chat.postMessage"


def _urlopen(req, timeout):
    # Wrapped for monkeypatching in tests.
    return urllib.request.urlopen(req, timeout=timeout)


def slack_post(text: str, channel: str, *, timeout: float = 10.0) -> dict[str, Any]:
    if not isinstance(channel, str) or not channel:
        raise RuntimeError("slack_post: channel must be a non-empty string")
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("slack_post: SLACK_BOT_TOKEN not set")

    body = json.dumps({
        "channel": channel,
        "text": text,
        "unfurl_links": False,
    }).encode()
    req = urllib.request.Request(
        _SLACK_POST_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )

    # --- definite failures (transport could not have delivered) ---
    try:
        resp = _urlopen(req, timeout=timeout)
    except socket.timeout as e:
        return {"result": "ambiguous_failure", "error": f"socket timeout: {e}"}
    except ConnectionResetError as e:
        return {"result": "ambiguous_failure", "error": f"connection reset: {e}"}
    except ConnectionRefusedError as e:
        return {"result": "definite_failure", "error": f"connection refused: {e}"}
    except socket.gaierror as e:
        return {"result": "definite_failure", "error": f"dns failure: {e}"}
    except urllib.error.HTTPError as e:
        # Slack returns 4xx for invalid_auth etc. — still definite_failure.
        try:
            payload = json.loads(e.read().decode() or "{}")
            err = payload.get("error", f"http_{e.code}")
        except Exception:
            err = f"http_{e.code}"
        return {"result": "definite_failure", "error": err}
    except urllib.error.URLError as e:
        # Wrapped OS errors — best to treat as ambiguous unless we know better.
        reason = getattr(e, "reason", "")
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            return {"result": "definite_failure", "error": f"urlerror: {reason}"}
        if isinstance(reason, socket.timeout):
            return {"result": "ambiguous_failure", "error": f"urlerror: {reason}"}
        return {"result": "ambiguous_failure", "error": f"urlerror: {reason}"}

    # --- status returned; parse body ---
    with resp:
        try:
            raw = resp.read()
            payload = json.loads(raw.decode())
        except Exception as e:
            return {"result": "ambiguous_failure", "error": f"unparseable response: {e}"}

        if resp.status >= 400:
            return {"result": "definite_failure", "error": payload.get("error", f"http_{resp.status}")}

        if not payload.get("ok"):
            return {"result": "definite_failure", "error": payload.get("error", "ok_false")}

        ts = payload.get("ts", "")
        return {"result": "ok", "ts": ts}
```

- [ ] **Step 4: Run tests, verify passing**

Run: `pytest tests/test_slack_post.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add slack_api.py tests/test_slack_post.py
git commit -m "feat: add slack_api.slack_post with observable-uncertainty classification"
```

---

## Task 7: `inline_agents.run_inline_agent`

**Files:**
- Create: `inline_agents.py`
- Test: deferred to the orchestrator tests in Task 11 (the unit here is thin glue around `run_claude_with_heartbeat`; we test the boundary with a mock there).

- [ ] **Step 1: Implement `inline_agents.py`**

Create `inline_agents.py`:

```python
"""Run drafting agents outside the job queue with strict success criteria.

This bypasses AGENT_MAP, build_agent_prompt, and repo overlays entirely.
Used for `review-announcer` and `review-digest` which have no associated
job, no repo worktree, and no repo-specific context.
"""
import json
import os
import re
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Re-use the existing Claude runner and state-delta extractor from worker.py.
# We import lazily to avoid circular imports and to make the helper easy to
# mock in tests.
def _run_claude(prompt: str, cwd: str, timeout: int, output_file: str, model: str | None) -> int:
    from worker import run_claude_with_heartbeat
    return run_claude_with_heartbeat(
        job_id=-1,  # sentinel: not a real job
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        output_file=output_file,
        model=model,
        max_turns=None,
    )


def _extract_deltas(output_file: str) -> list[dict]:
    """Collect every parseable state_delta fenced block from Claude's stream."""
    deltas: list[dict] = []
    try:
        with open(output_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                for block in obj.get("message", {}).get("content", []):
                    if block.get("type") != "text":
                        continue
                    for match in re.findall(
                        r"```state_delta\s*\n(.*?)\n```",
                        block.get("text", ""),
                        re.DOTALL,
                    ):
                        try:
                            deltas.append(json.loads(match.strip()))
                        except json.JSONDecodeError:
                            deltas.append({"__malformed__": match[:200]})
    except OSError:
        pass
    return deltas


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_raw = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    fm: dict[str, str] = {}
    for line in fm_raw.split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, body


def run_inline_agent(
    agent_name: str,
    placeholders: dict[str, str],
    *,
    expected_type: str,
    timeout_seconds: int = 180,
) -> dict:
    """Run an agent file out-of-queue with strict success criteria.

    Returns one of:
        {"result": "ok", "delta": {...}}
        {"result": "agent_failure", "reason": "<tag>"}

    `expected_type` is the value we require in the delta's `type` field.
    A non-empty `message` string is also required.
    """
    agent_file = SCRIPT_DIR / "agents" / f"{agent_name}.md"
    if not agent_file.is_file():
        return {"result": "agent_failure", "reason": "agent_file_missing"}

    text = agent_file.read_text()
    fm, body = _parse_frontmatter(text)
    model = fm.get("model")

    prompt = body
    for key, value in placeholders.items():
        prompt = prompt.replace("{{" + key + "}}", value)

    unresolved = re.findall(r"\{\{[A-Z0-9_]+\}\}", prompt)
    if unresolved:
        return {"result": "agent_failure", "reason": f"unresolved:{','.join(sorted(set(unresolved)))[:80]}"}

    claudia_dir = placeholders.get("CLAUDIA_DIR", str(SCRIPT_DIR))
    output_file = tempfile.mktemp(suffix=".jsonl", prefix=f"inline-{agent_name}-")
    try:
        try:
            exit_code = _run_claude(prompt, claudia_dir, timeout_seconds, output_file, model)
        except Exception as e:
            return {"result": "agent_failure", "reason": f"exception:{type(e).__name__}"}

        if exit_code == -1:
            return {"result": "agent_failure", "reason": "timeout"}
        if exit_code != 0:
            return {"result": "agent_failure", "reason": f"exit_{exit_code}"}

        deltas = _extract_deltas(output_file)
        if not deltas:
            return {"result": "agent_failure", "reason": "no_delta"}
        # Strict: exactly one usable delta.
        if len(deltas) > 1:
            return {"result": "agent_failure", "reason": "multiple_deltas"}
        delta = deltas[0]
        if "__malformed__" in delta:
            return {"result": "agent_failure", "reason": "malformed_json"}
        if delta.get("type") != expected_type:
            return {"result": "agent_failure", "reason": f"type_mismatch:{delta.get('type')}"}
        message = delta.get("message")
        if not isinstance(message, str) or not message.strip():
            return {"result": "agent_failure", "reason": "empty_message"}
        return {"result": "ok", "delta": delta}
    finally:
        try:
            os.unlink(output_file)
        except OSError:
            pass
```

- [ ] **Step 2: Smoke-import it**

Run: `python3 -c "import inline_agents; print(inline_agents.run_inline_agent.__doc__[:60])"`
Expected: the first line of the docstring is printed without errors.

- [ ] **Step 3: Commit**

```bash
git add inline_agents.py
git commit -m "feat: add run_inline_agent helper for out-of-queue drafting agents"
```

---

## Task 8: `agents/review-announcer.md`

**Files:**
- Create: `agents/review-announcer.md`

- [ ] **Step 1: Write the agent file**

Create `agents/review-announcer.md`:

````markdown
---
name: review-announcer
tools: Bash, Read
model: sonnet
---

You draft a single Slack review-request message for one of your own pull
requests. You DO NOT post it. You output a state delta and exit.

## Inputs

- Repo: `{{REPO}}`
- PR number: `{{PR_NUMBER}}`
- PR URL: `{{PR_URL}}`
- Sanitized title: `{{SANITIZED_TITLE}}`
- Slack channel ID: `{{SLACK_REVIEW_CHANNEL}}`
- Bot user id (may be literal `null`): `{{CLAUDIA_BOT_USER_ID}}`
- Skip channel-style fetch: `{{SKIP_CHANNEL_STYLE_FETCH}}` (`true` or `false`)

## Phase 1 — Fetch PR context

```bash
gh pr view {{PR_NUMBER}} --repo {{REPO}} \
  --json number,title,body,url,additions,deletions,changedFiles,files
```

Read `title`, `body`, `files[].path` carefully. Your description must be
grounded strictly in what the PR actually does — no invented features.

## Phase 2 — Fetch channel style (optional)

If `{{SKIP_CHANNEL_STYLE_FETCH}}` is `true`, skip this phase entirely and
use the baked-in examples below as your tone reference.

Otherwise, call:

```bash
curl -s -G "https://slack.com/api/conversations.history" \
  --data-urlencode "channel={{SLACK_REVIEW_CHANNEL}}" \
  --data-urlencode "limit=30" \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN"
```

Filter the returned `messages` array:

- Keep only messages whose `text` contains `github.com/` and `/pull/`.
- Drop any message where `user == {{CLAUDIA_BOT_USER_ID}}` or whose
  `bot_id` looks like Claudia's.

Study the remaining messages for tone, typical length, and any emoji
conventions. If the filtered set is empty or off-topic, fall back to the
baked-in examples.

## Phase 3 — Draft the message

Write **1 or 2 sentences** (never three) that describe what the PR does.
Constraints:

- The exact literal `<{{PR_URL}}|PR #{{PR_NUMBER}} — {{SANITIZED_TITLE}}>`
  MUST appear somewhere in the message. No other GitHub PR links.
- No `@` mentions, no `<!here>`, `<!channel>`, `<!everyone>`, or
  `<!subteam^...>`.
- Match the tone of the filtered channel messages. If the channel is quiet
  or off-topic, default to neutral plain prose.
- Do not invent features not in the PR diff/body.
- No emojis unless the filtered channel history uses them.

## Phase 4 — Output

Output ONLY a single state delta fenced block — no prose before or after:

```state_delta
{"type":"review_announce","repo":"{{REPO}}","pr_number":{{PR_NUMBER}},"message":"<your drafted message>"}
```

Exit immediately after the state delta.

## Examples (baked-in tone reference)

```
<https://github.com/ls1intum/Artemis/pull/1234|PR #1234 — Communication: Fix notification ordering>
Reorders notification delivery so course-wide announcements always land before per-thread pings. Small change in NotificationService, touches two tests.
```

```
<https://github.com/ls1intum/Artemis/pull/1250|PR #1250 — Exercise: Cache participation lookups>
Adds a short-lived cache around participation fetches on the exercise dashboard to cut repeat DB hits. Behaviour unchanged for students; mainly a performance win.
```

```
<https://github.com/ls1intum/Artemis/pull/1261|PR #1261 — General: Bump Hibernate to 6.4>
Routine Hibernate minor bump. Touches a handful of entity mappings where the deprecated API was still in use; no schema changes.
```

```
<https://github.com/ls1intum/Artemis/pull/1272|PR #1272 — Iris: Retry transient LLM timeouts>
Wraps the Iris chat completion call in a short retry loop for 504s and connection resets. Logs are unchanged on success and noisier on retry.
```
````

- [ ] **Step 2: Commit**

```bash
git add agents/review-announcer.md
git commit -m "feat(agents): add review-announcer drafting agent"
```

---

## Task 9: `agents/review-digest.md`

**Files:**
- Create: `agents/review-digest.md`

- [ ] **Step 1: Write the agent file**

Create `agents/review-digest.md`:

````markdown
---
name: review-digest
tools: Bash, Read
model: sonnet
---

You draft the daily digest of open pull requests that need review. You DO
NOT post the message. You output a state delta and exit.

## Inputs

- Slack channel ID: `{{SLACK_REVIEW_CHANNEL}}`
- Bot user id (may be literal `null`): `{{CLAUDIA_BOT_USER_ID}}`
- Skip channel-style fetch: `{{SKIP_CHANNEL_STYLE_FETCH}}`
- Partial flag: `{{PARTIAL}}` (`true` or `false`)
- Failed repos (JSON list, may be `[]`): `{{FAILED_REPOS_JSON}}`
- PR list (JSON): `{{PR_LIST_JSON}}`
  Schema: `[{"repo":"...","pr_number":N,"url":"...","title":"...","body_excerpt":"...","sanitized_title":"..."}, ...]`

## Phase 1 — Parse inputs

Parse `{{PR_LIST_JSON}}` and `{{FAILED_REPOS_JSON}}`. Preserve the PR list
order exactly — the worker already sorted it.

## Phase 2 — Fetch channel style (optional)

If `{{SKIP_CHANNEL_STYLE_FETCH}}` is `true`, skip. Otherwise call:

```bash
curl -s -G "https://slack.com/api/conversations.history" \
  --data-urlencode "channel={{SLACK_REVIEW_CHANNEL}}" \
  --data-urlencode "limit=30" \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN"
```

Apply the same filtering as `review-announcer`: only `github.com/.../pull/`
messages, drop Claudia's own.

## Phase 3 — Draft

Structure:

1. If `{{PARTIAL}} == true`, the FIRST line must be an unmistakable partial
   label that names every repo in `{{FAILED_REPOS_JSON}}`. Example:
   `⚠️ Partial digest — could not enumerate ls1intum/Foo, ls1intum/Bar.`
   Follow it with a blank line.
2. Greeting line. Do NOT reference dates, times, or "yesterday" — these
   PRs may have been open for days.
3. One bullet per PR. Each bullet must contain the exact literal
   `<{url}|PR #{pr_number} — {sanitized_title}>` followed by a 1–2 sentence
   prose description grounded in the PR title and body_excerpt. Cap prose
   at ~260 characters per bullet.

Rules:
- No `@` mentions, no `<!here>` / `<!channel>` / `<!everyone>` / `<!subteam^...>`.
- No GitHub PR links other than the supplied ones.
- No `Thanks!` footer.
- No emojis except those seen in the sampled channel history (and the
  `⚠️` in the partial label if applicable).

## Phase 4 — Output

Output ONLY a single state delta fenced block:

```state_delta
{"type":"review_digest","count":<N>,"partial":<true_or_false_lower>,"message":"<drafted message>"}
```

## Example (non-partial)

```
Good morning! A few open PRs that could use a review when you have a moment:

• <https://.../pull/1234|PR #1234 — Communication: Fix notification ordering>
  Reorders notification delivery so course-wide announcements always arrive before per-thread pings. Small patch, two touched tests.
• <https://.../pull/1250|PR #1250 — Exercise: Cache participation lookups>
  Adds a short-lived cache on the exercise dashboard participation query to cut repeat DB reads. No user-visible behaviour change.
• <https://.../pull/1261|PR #1261 — General: Bump Hibernate to 6.4>
  Minor Hibernate bump with small mapping tweaks. No schema changes, nothing risky.
```

## Example (partial)

```
⚠️ Partial digest — could not enumerate ls1intum/Foo, ls1intum/Bar.

Good morning! Open PRs I could enumerate this session:

• <https://.../pull/1234|PR #1234 — Communication: Fix notification ordering>
  Reorders notification delivery so course-wide announcements always arrive before per-thread pings. Small patch, two touched tests.
```
````

- [ ] **Step 2: Commit**

```bash
git add agents/review-digest.md
git commit -m "feat(agents): add review-digest drafting agent"
```

---

## Task 10: `review_requests.py` — classifier, sanitizer, validators, fallbacks

**Files:**
- Create: `review_requests.py`
- Test: `tests/test_review_requests.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_review_requests.py`:

```python
"""Unit tests for review_requests.py (pure-function layer)."""
import pytest

import review_requests as rr


# ── Delta classifier ─────────────────────────────────────────────────────

def test_classifies_implement_with_pr_number():
    assert rr.delta_triggers_announce({"type": "implement", "pr_number": 42, "status": "implemented"}) is True

def test_classifies_implement_without_pr_number_false():
    assert rr.delta_triggers_announce({"type": "implement", "status": "implemented"}) is False

def test_classifies_feedback_with_pushed_sha_true():
    assert rr.delta_triggers_announce(
        {"type": "feedback", "status": "handled", "pushed_sha": "abc123", "pr_number": 9}
    ) is True

def test_classifies_feedback_without_pushed_sha_false():
    assert rr.delta_triggers_announce(
        {"type": "feedback", "status": "handled", "pushed_sha": None, "pr_number": 9}
    ) is False

def test_classifies_feedback_with_empty_pushed_sha_false():
    assert rr.delta_triggers_announce(
        {"type": "feedback", "status": "handled", "pushed_sha": "", "pr_number": 9}
    ) is False

def test_classifies_hygiene_false():
    assert rr.delta_triggers_announce({"type": "hygiene", "pr_number": 9}) is False

def test_classifies_review_false():
    assert rr.delta_triggers_announce({"type": "review", "pr_number": 9}) is False


# ── Title sanitization ───────────────────────────────────────────────────

def test_sanitize_strips_control_chars():
    assert rr.sanitize_title("foo\x00bar\x1fbaz") == "foobarbaz"

def test_sanitize_escapes_mrkdwn():
    assert rr.sanitize_title("a & b < c > d") == "a &amp; b &lt; c &gt; d"

def test_sanitize_strips_shell_metachars():
    assert rr.sanitize_title("foo`bar`$baz'qux") == "foobarbazqux"

def test_sanitize_truncates_over_140():
    title = "x" * 200
    out = rr.sanitize_title(title)
    assert len(out) == 140
    assert out.endswith("…")


# ── Per-PR validator ─────────────────────────────────────────────────────

LINK = "<https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix thing>"

def _msg(prefix="Quick review please — ", suffix=""):
    return f"{prefix}{LINK}{suffix}"

def test_announce_validator_good():
    v = rr.validate_announce_message(
        _msg() + "\nTouches one file.",
        pr_url="https://github.com/ls1intum/Artemis/pull/42",
        pr_number=42,
        sanitized_title="General: Fix thing",
    )
    assert v.status == "ok"

def test_announce_validator_missing_exact_link_is_hard_reject():
    v = rr.validate_announce_message(
        "Please review https://github.com/ls1intum/Artemis/pull/42",
        pr_url="https://github.com/ls1intum/Artemis/pull/42",
        pr_number=42,
        sanitized_title="General: Fix thing",
    )
    assert v.status == "hard_reject"
    assert "missing_link" in v.reason

def test_announce_validator_extra_pr_link_is_hard_reject():
    extra = _msg() + "\nSee also https://github.com/foo/bar/pull/1"
    v = rr.validate_announce_message(
        extra,
        pr_url="https://github.com/ls1intum/Artemis/pull/42",
        pr_number=42,
        sanitized_title="General: Fix thing",
    )
    assert v.status == "hard_reject"

@pytest.mark.parametrize("mention", [
    "<@U12345>", "<!here>", "<!channel>", "<!everyone>", "<!subteam^S123|team>",
])
def test_announce_validator_mentions_hard_reject(mention):
    v = rr.validate_announce_message(
        _msg() + f"\n{mention}",
        pr_url="https://github.com/ls1intum/Artemis/pull/42",
        pr_number=42,
        sanitized_title="General: Fix thing",
    )
    assert v.status == "hard_reject"

def test_announce_validator_over_length_is_warn_only():
    prose = "x " * 200  # much > 280 chars
    v = rr.validate_announce_message(
        _msg() + "\n" + prose,
        pr_url="https://github.com/ls1intum/Artemis/pull/42",
        pr_number=42,
        sanitized_title="General: Fix thing",
    )
    assert v.status == "warn"


# ── Digest validator ─────────────────────────────────────────────────────

DIGEST_GOOD = (
    "Good morning! A few open PRs:\n\n"
    "• <https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix thing>\n"
    "  Fixes the thing. Small patch.\n"
    "• <https://github.com/ls1intum/Artemis/pull/43|PR #43 — Exercise: Cache>\n"
    "  Adds caching.\n"
)

PR_LIST = [
    {"repo": "ls1intum/Artemis", "pr_number": 42,
     "url": "https://github.com/ls1intum/Artemis/pull/42",
     "sanitized_title": "General: Fix thing"},
    {"repo": "ls1intum/Artemis", "pr_number": 43,
     "url": "https://github.com/ls1intum/Artemis/pull/43",
     "sanitized_title": "Exercise: Cache"},
]

def test_digest_validator_good():
    v = rr.validate_digest_message(DIGEST_GOOD, pr_list=PR_LIST, failed_repos=[])
    assert v.status == "ok"

def test_digest_validator_missing_pr_link_hard_reject():
    missing = (
        "Good morning!\n\n"
        "• <https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix thing>\n"
        "  Fixes the thing.\n"
    )
    v = rr.validate_digest_message(missing, pr_list=PR_LIST, failed_repos=[])
    assert v.status == "hard_reject"

def test_digest_validator_partial_missing_label_hard_reject():
    v = rr.validate_digest_message(
        DIGEST_GOOD, pr_list=PR_LIST, failed_repos=["ls1intum/Foo"]
    )
    assert v.status == "hard_reject"

def test_digest_validator_partial_label_missing_repo_name_hard_reject():
    msg = "⚠️ Partial digest — could not enumerate ls1intum/Bar.\n\n" + DIGEST_GOOD
    v = rr.validate_digest_message(msg, pr_list=PR_LIST, failed_repos=["ls1intum/Foo"])
    assert v.status == "hard_reject"

def test_digest_validator_partial_good():
    msg = "⚠️ Partial digest — could not enumerate ls1intum/Foo.\n\n" + DIGEST_GOOD
    v = rr.validate_digest_message(msg, pr_list=PR_LIST, failed_repos=["ls1intum/Foo"])
    assert v.status == "ok"

def test_digest_validator_bullet_over_length_is_warn_only():
    long_prose = "x" * 300  # >260
    msg = (
        "Good morning!\n\n"
        "• <https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix thing>\n"
        f"  {long_prose}\n"
        "• <https://github.com/ls1intum/Artemis/pull/43|PR #43 — Exercise: Cache>\n"
        "  Short ok prose.\n"
    )
    v = rr.validate_digest_message(msg, pr_list=PR_LIST, failed_repos=[])
    assert v.status == "warn"
    assert "bullet_over_length" in v.reason

def test_digest_validator_bullet_over_sentences_is_warn_only():
    msg = (
        "Good morning!\n\n"
        "• <https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix thing>\n"
        "  First sentence. Second sentence. Third sentence. Fourth sentence.\n"
        "• <https://github.com/ls1intum/Artemis/pull/43|PR #43 — Exercise: Cache>\n"
        "  Fine.\n"
    )
    v = rr.validate_digest_message(msg, pr_list=PR_LIST, failed_repos=[])
    assert v.status == "warn"
    assert "bullet_over_sentences" in v.reason


# ── Template fallback renderers ──────────────────────────────────────────

def test_announce_fallback():
    out = rr.render_announce_fallback(
        pr_url="https://github.com/ls1intum/Artemis/pull/42",
        pr_number=42,
        sanitized_title="General: Fix thing",
    )
    assert ":mag:" in out
    assert "<https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix thing>" in out

def test_digest_fallback_non_partial():
    out = rr.render_digest_fallback(pr_list=PR_LIST, failed_repos=[])
    assert ":sunrise:" in out
    assert "PR #42" in out
    assert "PR #43" in out
    assert ":warning:" not in out

def test_digest_fallback_partial_labels_failed_repos():
    out = rr.render_digest_fallback(
        pr_list=PR_LIST, failed_repos=["ls1intum/Foo", "ls1intum/Bar"]
    )
    assert ":warning:" in out
    assert "ls1intum/Foo" in out
    assert "ls1intum/Bar" in out
    assert "PR #42" in out
```

- [ ] **Step 2: Run, verify failing**

Run: `pytest tests/test_review_requests.py -v`
Expected: `ModuleNotFoundError: No module named 'review_requests'`.

- [ ] **Step 3: Implement `review_requests.py` (pure-function layer)**

Create `review_requests.py`. This task only implements the pure-function layer (classifier, sanitizer, validators, fallback renderers). The orchestration functions are added in Tasks 11 and 12.

```python
"""Slack review-request notifications: pure helpers.

Non-pure orchestration (`_maybe_announce_review`, `_maybe_fire_digest`)
is defined later in this module alongside these helpers. This section is
pure: no DB, no subprocess, no network.
"""
import json
import re
from dataclasses import dataclass
from typing import Any

# ── Delta classifier ────────────────────────────────────────────────────

def delta_triggers_announce(delta: dict) -> bool:
    """True iff this state delta should trigger a per-PR announcement."""
    if not isinstance(delta, dict):
        return False
    t = delta.get("type")
    if t == "implement":
        return bool(delta.get("pr_number"))
    if t == "feedback":
        return (
            delta.get("status") == "handled"
            and bool(delta.get("pushed_sha"))
            and bool(delta.get("pr_number"))
        )
    return False


# ── Title sanitization ──────────────────────────────────────────────────

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_SHELL_METACHARS = re.compile(r"[`'$]")

def sanitize_title(title: str, *, max_len: int = 140) -> str:
    """Shell-safe, mrkdwn-escaped, length-capped title."""
    if not isinstance(title, str):
        title = str(title or "")
    t = _CONTROL_CHARS.sub("", title)
    t = _SHELL_METACHARS.sub("", t)
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t


# ── Validators ──────────────────────────────────────────────────────────

@dataclass
class Validation:
    status: str  # "ok" | "warn" | "hard_reject"
    reason: str = ""


_MENTION_RES = [
    re.compile(r"<@[UW][A-Z0-9]+>"),
    re.compile(r"<!here>"),
    re.compile(r"<!channel>"),
    re.compile(r"<!everyone>"),
    re.compile(r"<!subteam\^"),
]

_PR_LINK_RE = re.compile(r"github\.com/[^/\s]+/[^/\s]+/pull/\d+")


def _has_mention(msg: str) -> bool:
    return any(r.search(msg) for r in _MENTION_RES)


def validate_announce_message(
    msg: str, *, pr_url: str, pr_number: int, sanitized_title: str
) -> Validation:
    expected_link = f"<{pr_url}|PR #{pr_number} — {sanitized_title}>"
    if expected_link not in msg:
        return Validation("hard_reject", "missing_link")
    # Unexpected github.com/*/pull/ references?
    expected_host_path = pr_url.split("://", 1)[-1]  # github.com/.../pull/42
    for hit in _PR_LINK_RE.findall(msg):
        if hit != expected_host_path:
            return Validation("hard_reject", f"unexpected_link:{hit}")
    if _has_mention(msg):
        return Validation("hard_reject", "mention_present")

    prose = msg.replace(expected_link, "").strip()
    if len(prose) > 280:
        return Validation("warn", "over_length")
    # Heuristic sentence count.
    sentences = re.findall(r"[.!?](?:\s|$)", prose)
    if len(sentences) > 2:
        return Validation("warn", "over_sentences")
    return Validation("ok")


def validate_digest_message(
    msg: str, *, pr_list: list[dict], failed_repos: list[str]
) -> Validation:
    # Each PR's expected literal link must appear.
    expected_links = [
        (pr, f"<{pr['url']}|PR #{pr['pr_number']} — {pr['sanitized_title']}>")
        for pr in pr_list
    ]
    for pr, expected in expected_links:
        if expected not in msg:
            return Validation("hard_reject", f"missing:{pr['pr_number']}")
    # No extra PR links.
    expected_paths = {pr["url"].split("://", 1)[-1] for pr in pr_list}
    for hit in _PR_LINK_RE.findall(msg):
        if hit not in expected_paths:
            return Validation("hard_reject", f"unexpected_link:{hit}")
    if _has_mention(msg):
        return Validation("hard_reject", "mention_present")
    if failed_repos:
        if not re.search(r"(?i)partial\s+digest", msg):
            return Validation("hard_reject", "partial_label_missing")
        for repo in failed_repos:
            if repo not in msg:
                return Validation("hard_reject", f"failed_repo_unnamed:{repo}")

    # ── Warn-only per-bullet checks ────────────────────────────────────
    # Slice the message between each expected link and the next link (or
    # end-of-message) to isolate the prose for each bullet.
    link_positions = [(pr, msg.find(lit)) for pr, lit in expected_links]
    link_positions.sort(key=lambda x: x[1])
    for i, (pr, start) in enumerate(link_positions):
        link_text = f"<{pr['url']}|PR #{pr['pr_number']} — {pr['sanitized_title']}>"
        prose_start = start + len(link_text)
        prose_end = (
            link_positions[i + 1][1] if i + 1 < len(link_positions) else len(msg)
        )
        prose = msg[prose_start:prose_end].strip()
        if len(prose) > 260:
            return Validation("warn", f"bullet_over_length:{pr['pr_number']}")
        sentences = re.findall(r"[.!?](?:\s|$)", prose)
        if len(sentences) > 2:
            return Validation("warn", f"bullet_over_sentences:{pr['pr_number']}")
    return Validation("ok")


# ── Template fallback renderers ─────────────────────────────────────────

def render_announce_fallback(
    *, pr_url: str, pr_number: int, sanitized_title: str
) -> str:
    return (
        ":mag: Review please — "
        f"<{pr_url}|PR #{pr_number} — {sanitized_title}>"
    )


def render_digest_fallback(*, pr_list: list[dict], failed_repos: list[str]) -> str:
    lines: list[str] = []
    if failed_repos:
        lines.append(
            ":warning: Partial digest — could not enumerate "
            + ", ".join(failed_repos)
            + "."
        )
        lines.append("")
        lines.append(":sunrise: Open PRs I could enumerate this session:")
    else:
        lines.append(":sunrise: Open PRs that could use a review:")
    for pr in pr_list:
        lines.append(
            f"• <{pr['url']}|PR #{pr['pr_number']} — {pr['sanitized_title']}>"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run, verify passing**

Run: `pytest tests/test_review_requests.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add review_requests.py tests/test_review_requests.py
git commit -m "feat: add review_requests pure layer (classifier, validators, fallbacks)"
```

---

## Task 11: `_maybe_announce_review` orchestration

**Files:**
- Modify: `review_requests.py`
- Test: `tests/test_review_orchestration.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_review_orchestration.py`:

```python
"""Tests for _maybe_announce_review orchestrator, with inline agent +
slack_post mocked at the module boundary."""
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import uuid

import pytest

import review_requests as rr


REPO = "ls1intum/Artemis"
NOW = datetime(2026, 4, 10, 22, 0, tzinfo=timezone.utc)


@pytest.fixture
def mock_conn():
    return MagicMock(name="pg_conn")


def _mock_claim(return_token):
    return patch("review_requests.db.claim_pr_review_slot", return_value=return_token)


def test_announce_suppressed_on_wrong_delta_type(mock_conn):
    rr._maybe_announce_review(
        mock_conn, REPO,
        {"type": "hygiene", "pr_number": 9},
        NOW,
    )
    # nothing happened — no claim call.

def test_announce_suppressed_when_already_claimed(mock_conn):
    with _mock_claim(None), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack:
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented",
             "pr_url": "https://github.com/ls1intum/Artemis/pull/42",
             "pr_title": "General: Fix x"},
            NOW,
        )
    agent.assert_not_called()
    slack.assert_not_called()

def test_announce_ok_path_finalizes(mock_conn):
    tok = uuid.uuid4()
    with _mock_claim(tok), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.finalize_pr_review_slot", return_value=True) as fin:
        agent.return_value = {
            "result": "ok",
            "delta": {
                "type": "review_announce",
                "message": (
                    "Review please: "
                    "<https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix x>. "
                    "Touches one file."
                ),
            },
        }
        slack.return_value = {"result": "ok", "ts": "1712000000.1"}
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented",
             "pr_url": "https://github.com/ls1intum/Artemis/pull/42",
             "pr_title": "General: Fix x"},
            NOW,
        )
    fin.assert_called_once()
    assert fin.call_args.kwargs["slack_ts"] == "1712000000.1"

def test_announce_hard_reject_uses_template_fallback(mock_conn):
    tok = uuid.uuid4()
    with _mock_claim(tok), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.finalize_pr_review_slot", return_value=True) as fin, \
         patch("review_requests.slack_alert") as alert:
        agent.return_value = {
            "result": "ok",
            "delta": {"type": "review_announce", "message": "Bad: no link"},
        }
        slack.return_value = {"result": "ok", "ts": "1.2"}
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented",
             "pr_url": "https://github.com/ls1intum/Artemis/pull/42",
             "pr_title": "General: Fix x"},
            NOW,
        )
    # Posted (fallback), finalized, alert fired.
    posted_text = slack.call_args.args[0]
    assert ":mag:" in posted_text
    fin.assert_called_once()
    alert.assert_called_once()

def test_announce_agent_failure_uses_template_fallback(mock_conn):
    tok = uuid.uuid4()
    with _mock_claim(tok), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.finalize_pr_review_slot", return_value=True), \
         patch("review_requests.slack_alert"):
        agent.return_value = {"result": "agent_failure", "reason": "timeout"}
        slack.return_value = {"result": "ok", "ts": "1.2"}
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented",
             "pr_url": "https://github.com/ls1intum/Artemis/pull/42",
             "pr_title": "General: Fix x"},
            NOW,
        )
    text = slack.call_args.args[0]
    assert ":mag:" in text

def test_announce_slack_definite_failure_releases_slot(mock_conn):
    tok = uuid.uuid4()
    with _mock_claim(tok), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.release_pr_review_slot", return_value=True) as rel, \
         patch("review_requests.db.finalize_pr_review_slot") as fin, \
         patch("review_requests.slack_alert") as alert:
        agent.return_value = {"result": "ok", "delta": {
            "type": "review_announce",
            "message": "Review please: <https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix x>."
        }}
        slack.return_value = {"result": "definite_failure", "error": "channel_not_found"}
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented",
             "pr_url": "https://github.com/ls1intum/Artemis/pull/42",
             "pr_title": "General: Fix x"},
            NOW,
        )
    rel.assert_called_once()
    fin.assert_not_called()
    alert.assert_called_once()

def test_announce_release_and_alert_when_resolution_fails(mock_conn):
    """When neither the delta nor `gh pr view` yield url+title, we must
    release the slot and alert — never post a broken link."""
    tok = uuid.uuid4()
    with _mock_claim(tok), \
         patch("review_requests._resolve_pr_url_and_title", return_value=None), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.release_pr_review_slot", return_value=True) as rel, \
         patch("review_requests.slack_alert") as alert:
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented"},
            NOW,
        )
    agent.assert_not_called()
    slack.assert_not_called()
    rel.assert_called_once()
    alert.assert_called_once()


def test_announce_slack_ambiguous_failure_leaves_posting(mock_conn):
    tok = uuid.uuid4()
    with _mock_claim(tok), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.release_pr_review_slot") as rel, \
         patch("review_requests.db.finalize_pr_review_slot") as fin, \
         patch("review_requests.slack_alert") as alert:
        agent.return_value = {"result": "ok", "delta": {
            "type": "review_announce",
            "message": "Review please: <https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix x>."
        }}
        slack.return_value = {"result": "ambiguous_failure", "error": "socket timeout"}
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented",
             "pr_url": "https://github.com/ls1intum/Artemis/pull/42",
             "pr_title": "General: Fix x"},
            NOW,
        )
    rel.assert_not_called()
    fin.assert_not_called()
    alert.assert_called_once()
```

- [ ] **Step 2: Run, verify failing**

Run: `pytest tests/test_review_orchestration.py -v`
Expected: `AttributeError: module 'review_requests' has no attribute '_maybe_announce_review'`.

- [ ] **Step 3: Implement orchestrator**

Append to `review_requests.py`:

```python
# ── Orchestration ──────────────────────────────────────────────────────

import logging
import os
import subprocess

import db
import windows
from inline_agents import run_inline_agent
from slack_api import slack_post
from utils import slack_alert

log = logging.getLogger("claudia.review_requests")


# Module-level state populated by worker.main().
WORKER_STATE = {
    "claudia_bot_user_id": None,  # may stay None
}


def _skip_channel_style() -> str:
    return "true" if WORKER_STATE.get("claudia_bot_user_id") is None else "false"


def _bot_user_literal() -> str:
    v = WORKER_STATE.get("claudia_bot_user_id")
    return v if v else "null"


def _review_channel() -> str:
    return os.environ.get("SLACK_REVIEW_CHANNEL", "C012NFRM76F")


def _resolve_pr_url_and_title(
    delta: dict, repo: str, pr_number: int
) -> tuple[str, str] | None:
    """Return (url, title) or None if we cannot produce BOTH values.

    Upstream state deltas (from `issue-implementer`, `pr-feedback-handler`)
    do not currently emit `pr_url` or `pr_title`, so this almost always
    falls through to `gh pr view`. If that call fails or returns empty
    fields, we return None — the caller MUST release the slot and alert
    rather than post a broken message like `<|PR #42 — >`.
    """
    url = delta.get("pr_url") or ""
    title = delta.get("pr_title") or ""
    if url and title:
        return url, title
    try:
        out = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo,
             "--json", "url,title"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            import json as _json
            data = _json.loads(out.stdout)
            url = url or data.get("url", "")
            title = title or data.get("title", "")
    except Exception as exc:
        log.warning("gh pr view failed for %s #%d: %s", repo, pr_number, exc)
    if url and title:
        return url, title
    return None


def _maybe_announce_review(conn, repo: str, delta: dict, now) -> None:
    """Post a per-PR review request if this delta qualifies. Never raises.

    Called from worker.py after a successful state delta has been recorded.
    """
    try:
        if not delta_triggers_announce(delta):
            return
        pr_number = int(delta["pr_number"])
        session_day = windows.current_own_session_day(now)

        claim_token = db.claim_pr_review_slot(conn, repo, pr_number, session_day)
        if claim_token is None:
            log.debug("Review slot already claimed: %s #%d %s", repo, pr_number, session_day)
            return

        resolved = _resolve_pr_url_and_title(delta, repo, pr_number)
        if resolved is None:
            # Release the slot so the next qualifying delta in the same
            # session can retry. Never post a message with empty URL/title.
            db.release_pr_review_slot(conn, repo, pr_number, session_day, claim_token)
            slack_alert(
                f":rotating_light: Could not resolve URL/title for "
                f"{repo} #{pr_number} (delta + `gh pr view` both empty) — "
                f"skipping announce, slot released"
            )
            return
        pr_url, raw_title = resolved
        sanitized_title = sanitize_title(raw_title)

        placeholders = {
            "REPO": repo,
            "PR_NUMBER": str(pr_number),
            "PR_URL": pr_url,
            "SANITIZED_TITLE": sanitized_title,
            "SLACK_REVIEW_CHANNEL": _review_channel(),
            "CLAUDIA_BOT_USER_ID": _bot_user_literal(),
            "SKIP_CHANNEL_STYLE_FETCH": _skip_channel_style(),
            "CLAUDIA_DIR": str(SCRIPT_DIR),
        }

        agent_result = run_inline_agent(
            "review-announcer",
            placeholders,
            expected_type="review_announce",
            timeout_seconds=180,
        )

        message: str
        use_fallback_reason: str | None = None

        if agent_result["result"] == "ok":
            draft = agent_result["delta"]["message"]
            v = validate_announce_message(
                draft,
                pr_url=pr_url,
                pr_number=pr_number,
                sanitized_title=sanitized_title,
            )
            if v.status == "hard_reject":
                use_fallback_reason = f"validator:{v.reason}"
                message = render_announce_fallback(
                    pr_url=pr_url, pr_number=pr_number, sanitized_title=sanitized_title
                )
            else:
                if v.status == "warn":
                    log.warning("Announce validator warn: %s", v.reason)
                message = draft
        else:
            use_fallback_reason = f"agent:{agent_result.get('reason')}"
            message = render_announce_fallback(
                pr_url=pr_url, pr_number=pr_number, sanitized_title=sanitized_title
            )

        if use_fallback_reason:
            slack_alert(
                f":construction: Review announce fell back to template "
                f"({use_fallback_reason}) for {repo} #{pr_number}"
            )

        slack_result = slack_post(message, _review_channel())

        if slack_result["result"] == "ok":
            ok = db.finalize_pr_review_slot(
                conn, repo, pr_number, session_day, claim_token,
                slack_ts=slack_result["ts"],
            )
            if not ok:
                slack_alert(
                    f":rotating_light: Review slot finalize UPDATE matched 0 rows "
                    f"for {repo} #{pr_number} — row stays `posting`"
                )
        elif slack_result["result"] == "definite_failure":
            db.release_pr_review_slot(conn, repo, pr_number, session_day, claim_token)
            slack_alert(
                f":rotating_light: slack_post definite_failure "
                f"({slack_result.get('error')}) for {repo} #{pr_number}"
            )
        else:  # ambiguous_failure
            slack_alert(
                f":rotating_light: slack_post AMBIGUOUS for {repo} #{pr_number} "
                f"({slack_result.get('error')}) — row stays `posting`, no auto-retry"
            )
    except Exception as exc:
        # Never let a notification glitch take down the worker.
        log.exception("_maybe_announce_review crashed: %s", exc)
```

Also add at the top of `review_requests.py` (below the existing imports):

```python
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
```

- [ ] **Step 4: Run, verify passing**

Run: `pytest tests/test_review_orchestration.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add review_requests.py tests/test_review_orchestration.py
git commit -m "feat: _maybe_announce_review orchestration with fallback + failure matrix"
```

---

## Task 12: `_maybe_fire_digest` orchestration with enumeration

**Files:**
- Modify: `review_requests.py`
- Test: `tests/test_review_orchestration.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_review_orchestration.py`:

```python
# ── Digest orchestrator tests ───────────────────────────────────────────

from unittest.mock import ANY


def _gh_list_ok(pr_entries):
    """Build a fake gh-pr-list subprocess result."""
    import json as _json
    result = MagicMock()
    result.returncode = 0
    result.stdout = _json.dumps(pr_entries)
    return result


def _gh_list_fail():
    result = MagicMock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = "boom"
    return result


@pytest.fixture
def two_repos(monkeypatch):
    monkeypatch.setattr(
        "review_requests.REPO_LIST_PROVIDER",
        lambda: ["ls1intum/Artemis", "ls1intum/Athena"],
    )


def test_digest_empty_and_complete_does_not_call_agent(mock_conn, two_repos):
    with patch("review_requests.db.mark_digest_posted_empty", return_value=True) as empty, \
         patch("review_requests._gh_list_prs") as gh, \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack:
        gh.return_value = ("ok", [])  # both repos return empty lists
        rr._maybe_fire_digest(mock_conn, NOW, github_user="claudia-bot")
    empty.assert_called_once()
    agent.assert_not_called()
    slack.assert_not_called()


def test_digest_excludes_drafts_and_approved(mock_conn, two_repos):
    with patch("review_requests.db.claim_pr_review_digest", return_value=uuid.uuid4()), \
         patch("review_requests.db.mark_digest_posted_empty", return_value=False), \
         patch("review_requests._gh_list_prs") as gh, \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.finalize_pr_review_digest", return_value=True):
        gh.side_effect = [
            ("ok", [
                {"number": 42, "title": "keep", "url": "u1",
                 "body": "b", "isDraft": False, "reviewDecision": ""},
                {"number": 43, "title": "draft", "url": "u2",
                 "body": "b", "isDraft": True, "reviewDecision": ""},
                {"number": 44, "title": "approved", "url": "u3",
                 "body": "b", "isDraft": False, "reviewDecision": "APPROVED"},
            ]),
            ("ok", []),
        ]
        agent.return_value = {
            "result": "ok",
            "delta": {"type": "review_digest", "message":
                "Good morning!\n\n• <u1|PR #42 — keep>\n  Keeps the thing.\n"}
        }
        slack.return_value = {"result": "ok", "ts": "1.2"}
        rr._maybe_fire_digest(mock_conn, NOW, github_user="claudia-bot")
    # Check the agent saw exactly one PR.
    sent_placeholders = agent.call_args.args[1]
    import json as _json
    pr_list = _json.loads(sent_placeholders["PR_LIST_JSON"])
    assert len(pr_list) == 1
    assert pr_list[0]["pr_number"] == 42


def test_digest_partial_label_enforced_on_agent_output(mock_conn, two_repos):
    tok = uuid.uuid4()
    with patch("review_requests.db.claim_pr_review_digest", return_value=tok), \
         patch("review_requests.db.mark_digest_posted_empty", return_value=False), \
         patch("review_requests._gh_list_prs") as gh, \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.finalize_pr_review_digest", return_value=True), \
         patch("review_requests.slack_alert"):
        gh.side_effect = [
            ("ok", [{"number": 42, "title": "keep", "url": "u1",
                     "body": "b", "isDraft": False, "reviewDecision": ""}]),
            ("fail", None),  # second repo enumeration fails
        ]
        # Agent forgets the partial label — validator rejects → fallback.
        agent.return_value = {
            "result": "ok",
            "delta": {"type": "review_digest", "message":
                "Good morning!\n\n• <u1|PR #42 — keep>\n  prose\n"}
        }
        slack.return_value = {"result": "ok", "ts": "1.2"}
        rr._maybe_fire_digest(mock_conn, NOW, github_user="claudia-bot")
    posted = slack.call_args.args[0]
    assert ":warning:" in posted
    assert "ls1intum/Athena" in posted


def test_digest_pagination_boundary_200_marks_partial(mock_conn, two_repos):
    tok = uuid.uuid4()
    with patch("review_requests.db.claim_pr_review_digest", return_value=tok), \
         patch("review_requests.db.mark_digest_posted_empty", return_value=False), \
         patch("review_requests._gh_list_prs") as gh, \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.finalize_pr_review_digest", return_value=True) as fin, \
         patch("review_requests.slack_alert"):
        # One repo returns exactly 200 items — treated as truncated.
        big = [
            {"number": n, "title": f"t{n}", "url": f"u{n}",
             "body": "", "isDraft": False, "reviewDecision": ""}
            for n in range(1, 201)
        ]
        gh.side_effect = [("ok", big), ("ok", [])]
        agent.return_value = {
            "result": "ok",
            "delta": {"type": "review_digest", "message": "fallback-will-be-used"},
        }
        slack.return_value = {"result": "ok", "ts": "1.2"}
        rr._maybe_fire_digest(mock_conn, NOW, github_user="claudia-bot")
    # Validator hard-rejects → fallback posted → still finalized with partial=True.
    assert fin.call_args.kwargs["partial"] is True
    assert fin.call_args.kwargs["pr_count"] == 200
```

- [ ] **Step 2: Run, verify failing**

Run: `pytest tests/test_review_orchestration.py -v -k digest`
Expected: `AttributeError: _maybe_fire_digest` / `REPO_LIST_PROVIDER`.

- [ ] **Step 3: Implement digest orchestrator**

Append to `review_requests.py`:

```python
# Populated by worker.main() with a function returning the list of repo slugs
# Claudia manages. Tests may monkeypatch this directly.
REPO_LIST_PROVIDER: "callable | None" = None


def _gh_list_prs(repo: str, github_user: str) -> tuple[str, list[dict] | None]:
    """Enumerate one repo's open PRs. Returns (status, entries).

    status: "ok" → entries is the JSON list; "fail" → entries is None.
    """
    try:
        r = subprocess.run(
            [
                "gh", "pr", "list", "--repo", repo,
                "--author", github_user, "--state", "open",
                "--limit", "200",
                "--json", "number,title,url,body,isDraft,reviewDecision",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            log.warning("gh pr list failed for %s: %s", repo, r.stderr.strip()[:200])
            return "fail", None
        import json as _json
        return "ok", _json.loads(r.stdout)
    except Exception as exc:
        log.warning("gh pr list exception for %s: %s", repo, exc)
        return "fail", None


def _filter_and_flatten(
    per_repo_results: dict[str, list[dict]]
) -> tuple[list[dict], list[str]]:
    """Apply draft/APPROVED exclusion + pagination-boundary detection.

    Returns (flat_pr_list, extra_failed_repos) where extra_failed_repos
    contains repos whose enumeration hit the 200-item boundary.
    """
    truncated: list[str] = []
    out: list[dict] = []
    for repo, entries in per_repo_results.items():
        if len(entries) == 200:
            truncated.append(repo)
        for e in entries:
            if e.get("isDraft"):
                continue
            if e.get("reviewDecision") == "APPROVED":
                continue
            out.append({
                "repo": repo,
                "pr_number": int(e["number"]),
                "url": e["url"],
                "title": e.get("title", ""),
                "body_excerpt": (e.get("body") or "")[:400],
                "sanitized_title": sanitize_title(e.get("title", "")),
            })
    out.sort(key=lambda p: (p["repo"], p["pr_number"]))
    return out, truncated


def _maybe_fire_digest(conn, now, *, github_user: str) -> None:
    try:
        session_day = windows.current_own_session_day(now)

        # Enumerate FIRST so the empty-and-complete path can short-circuit
        # without claiming the slot with `posting` state we'd immediately
        # have to finalize.
        repos = REPO_LIST_PROVIDER() if REPO_LIST_PROVIDER else []
        per_repo: dict[str, list[dict]] = {}
        failed_repos: list[str] = []
        for repo in repos:
            status, entries = _gh_list_prs(repo, github_user)
            if status == "ok":
                per_repo[repo] = entries
            else:
                failed_repos.append(repo)

        pr_list, truncated = _filter_and_flatten(per_repo)
        failed_repos.extend(truncated)

        if not pr_list and not failed_repos:
            inserted = db.mark_digest_posted_empty(conn, session_day)
            log.info("Empty-and-complete digest for %s (inserted=%s)", session_day, inserted)
            return

        claim_token = db.claim_pr_review_digest(conn, session_day)
        if claim_token is None:
            log.debug("Digest slot already claimed for %s", session_day)
            return

        import json as _json
        placeholders = {
            "SLACK_REVIEW_CHANNEL": _review_channel(),
            "CLAUDIA_BOT_USER_ID": _bot_user_literal(),
            "SKIP_CHANNEL_STYLE_FETCH": _skip_channel_style(),
            "CLAUDIA_DIR": str(SCRIPT_DIR),
            "PARTIAL": "true" if failed_repos else "false",
            "FAILED_REPOS_JSON": _json.dumps(failed_repos),
            "PR_LIST_JSON": _json.dumps(pr_list),
        }

        message: str
        use_fallback_reason: str | None = None

        if pr_list:
            agent_result = run_inline_agent(
                "review-digest",
                placeholders,
                expected_type="review_digest",
                timeout_seconds=180,
            )
            if agent_result["result"] == "ok":
                draft = agent_result["delta"]["message"]
                v = validate_digest_message(draft, pr_list=pr_list, failed_repos=failed_repos)
                if v.status == "hard_reject":
                    use_fallback_reason = f"validator:{v.reason}"
                    message = render_digest_fallback(
                        pr_list=pr_list, failed_repos=failed_repos
                    )
                else:
                    if v.status == "warn":
                        log.warning("Digest validator warn: %s", v.reason)
                    message = draft
            else:
                use_fallback_reason = f"agent:{agent_result.get('reason')}"
                message = render_digest_fallback(
                    pr_list=pr_list, failed_repos=failed_repos
                )
        else:
            # pr_list is empty but failed_repos is not — pure partial digest.
            message = render_digest_fallback(pr_list=[], failed_repos=failed_repos)

        if use_fallback_reason:
            slack_alert(
                f":construction: Digest fell back to template "
                f"({use_fallback_reason}) for session {session_day}"
            )

        slack_result = slack_post(message, _review_channel())

        if slack_result["result"] == "ok":
            ok = db.finalize_pr_review_digest(
                conn, session_day, claim_token,
                slack_ts=slack_result["ts"],
                pr_count=len(pr_list),
                partial=bool(failed_repos),
            )
            if not ok:
                slack_alert(
                    f":rotating_light: Digest finalize matched 0 rows "
                    f"for session {session_day} — row stays `posting`"
                )
        elif slack_result["result"] == "definite_failure":
            db.release_pr_review_digest(conn, session_day, claim_token)
            slack_alert(
                f":rotating_light: Digest slack_post definite_failure "
                f"({slack_result.get('error')}) for session {session_day}"
            )
        else:  # ambiguous
            slack_alert(
                f":rotating_light: Digest slack_post AMBIGUOUS for {session_day} "
                f"({slack_result.get('error')}) — row stays `posting`, no auto-retry"
            )
    except Exception as exc:
        log.exception("_maybe_fire_digest crashed: %s", exc)
```

- [ ] **Step 4: Run, verify passing**

Run: `pytest tests/test_review_orchestration.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add review_requests.py tests/test_review_orchestration.py
git commit -m "feat: _maybe_fire_digest orchestration with partial handling"
```

---

## Task 13: Worker integration — post-delta hook, main-loop digest hook, startup init

**Files:**
- Modify: `worker.py`
- Test: `tests/test_worker_digest.py` (new)

- [ ] **Step 1: Write the failing tests for a new pure helper `should_fire_digest` + a real driver test for `worker._run_digest_tick`**

The TDD discipline requires a real failing test. We extract the transition detection into a pure helper `review_requests.should_fire_digest(prev_in_own, now) -> bool` AND a worker-level shim `worker._run_digest_tick(conn, now, prev_in_own, github_user) -> bool` that the main loop calls. Both are absent initially, so the tests fail.

Create `tests/test_worker_digest.py`:

```python
"""TDD tests for the digest transition detection helper and worker shim."""
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

import review_requests as rr


UTC = timezone.utc


# ── should_fire_digest (pure helper) ────────────────────────────────────

@pytest.mark.parametrize("prev,is_now_in,expected", [
    (True,  False, True),   # crossed out of the window → fire
    (True,  True,  False),  # still inside → no fire
    (False, False, False),  # still outside → no fire
    (False, True,  False),  # just entered → no fire
    (None,  False, False),  # cold start outside window → no fire
    (None,  True,  False),  # cold start inside window → no fire
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
        now = datetime(2026, 4, 11, 7, 0, tzinfo=UTC)  # just closed
        new_state = worker._run_digest_tick(
            conn, now, prev_in_own=True, github_user="claudia-bot"
        )
    fire.assert_called_once_with(conn, now, github_user="claudia-bot")
    assert new_state is False  # 07:00 is outside the own window


def test_run_digest_tick_no_call_when_still_inside_window():
    import worker
    conn = MagicMock()
    with patch.object(rr, "_maybe_fire_digest") as fire:
        now = datetime(2026, 4, 11, 6, 0, tzinfo=UTC)  # inside window
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
        # Must not raise — worker loop cannot crash on notification glitches.
        now = datetime(2026, 4, 11, 7, 0, tzinfo=UTC)
        new_state = worker._run_digest_tick(
            conn, now, prev_in_own=True, github_user="claudia-bot"
        )
    assert new_state is False
```

- [ ] **Step 2: Run, verify failing**

Run: `pytest tests/test_worker_digest.py -v`
Expected: both `should_fire_digest` and `worker._run_digest_tick` are absent → failures like `AttributeError: module 'review_requests' has no attribute 'should_fire_digest'` and `AttributeError: module 'worker' has no attribute '_run_digest_tick'`.

- [ ] **Step 2a: Implement `should_fire_digest`**

Append to `review_requests.py` (near the top of the orchestration section):

```python
def should_fire_digest(prev_in_own: bool | None, is_now_in_own: bool) -> bool:
    """True iff we just crossed the own-window's closing edge.

    `prev_in_own=None` means cold start — we never fire retroactively.
    """
    return prev_in_own is True and is_now_in_own is False
```

- [ ] **Step 3: Implement `worker._run_digest_tick` shim + post-delta hook**

Add a new top-level helper in `worker.py` (near the other `worker_loop` helpers, e.g., below `_log_success`):

```python
def _run_digest_tick(
    conn, now, *, prev_in_own: bool | None, github_user: str
) -> bool:
    """Fire the digest exactly once on the own-window closing transition.

    Returns the new `is_in_own` state for the caller to remember. Never
    raises — notification glitches must not take down the worker loop.
    """
    import review_requests
    is_in_own = windows.is_allowed_now("implement", now)
    if review_requests.should_fire_digest(prev_in_own, is_in_own):
        try:
            review_requests._maybe_fire_digest(conn, now, github_user=github_user)
        except Exception as exc:
            log.warning("_maybe_fire_digest failed: %s", exc)
    return is_in_own
```

Post-delta hook: in `worker.py`, locate the `if outcome == "success":` block around line 2279. After the `_log_success(...)` call, add:

```python
            if state_delta:
                try:
                    import review_requests
                    review_requests._maybe_announce_review(conn, repo, state_delta, datetime.now(timezone.utc))
                except Exception as exc:
                    log.warning("_maybe_announce_review failed: %s", exc)
```

- [ ] **Step 4: Wire `_run_digest_tick` into the main loop**

At the top of `worker_loop` around line 1890, add:

```python
    # Review-digest transition detection (None = cold start — no retroactive fire).
    was_in_own_window: bool | None = None
```

Then inside the loop, right after `now_utc = datetime.now(timezone.utc)` (around line 1964), BEFORE the `allowed_types = ...` computation, add:

```python
        was_in_own_window = _run_digest_tick(
            conn, now_utc,
            prev_in_own=was_in_own_window,
            github_user=github_user,
        )
```

- [ ] **Step 5: Wire startup init — bot user id + repo list provider**

In `worker.main()`, after `REPO_CONTEXTS` is populated (right after the `for repo, settings in repos_config.items():` loop around line 2527), add:

```python
    # Review-request helper state.
    import review_requests
    try:
        import json as _json
        import subprocess as _sp
        r = _sp.run(
            ["curl", "-s", "-X", "POST", "https://slack.com/api/auth.test",
             "-H", f"Authorization: Bearer {os.environ.get('SLACK_BOT_TOKEN','')}"],
            capture_output=True, text=True, timeout=10,
        )
        data = _json.loads(r.stdout or "{}")
        if data.get("ok"):
            review_requests.WORKER_STATE["claudia_bot_user_id"] = data.get("user_id")
            log.info("Resolved Slack bot user id: %s", data.get("user_id"))
        else:
            log.warning("Slack auth.test failed: %s", data.get("error"))
    except Exception as exc:
        log.warning("Slack auth.test exception: %s", exc)

    review_requests.REPO_LIST_PROVIDER = lambda: list(REPO_CONTEXTS.keys())
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/ -v`
Expected: all PASS. Pay special attention to `tests/test_worker_digest.py`.

- [ ] **Step 7: Commit**

```bash
git add worker.py tests/test_worker_digest.py
git commit -m "feat(worker): wire review-request hooks + startup bot-id resolution"
```

---

## Task 14: Env var + docs

**Files:**
- Modify: `.env.example`, `README.md`

- [ ] **Step 1: Check current state**

Run: `grep -n "SLACK" .env.example README.md 2>/dev/null` and read the current env table format.

- [ ] **Step 2: Add `SLACK_REVIEW_CHANNEL` to `.env.example`**

Add below the existing `SLACK_CHANNEL=...` line:

```
# Slack channel ID for review-request notifications (per-PR + daily digest).
# Defaults to #artemistest if unset.
SLACK_REVIEW_CHANNEL=C012NFRM76F
```

- [ ] **Step 3: Document in `README.md`**

Add a row to the env-var table (matching the surrounding format):

```
| `SLACK_REVIEW_CHANNEL` | Slack channel ID for review-request notifications (per-PR + daily digest). Default: `C012NFRM76F` (`#artemistest`). |
```

- [ ] **Step 4: Commit**

```bash
git add .env.example README.md
git commit -m "docs: document SLACK_REVIEW_CHANNEL env var"
```

---

## Task 15: Full test pass + lint

**Files:** none modified here — final sanity check.

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v 2>&1 | tee /tmp/claudia_review_tests.txt | tail -50`
Expected: all PASS.

- [ ] **Step 2: Check Python import graph (no circular imports)**

Run: `python3 -c "import worker, review_requests, inline_agents, slack_api, windows, db"`
Expected: no ImportError, no circular-import errors.

- [ ] **Step 3: Lint if configured**

Run: `ls requirements-dev.txt 2>/dev/null && grep -E "ruff|flake8|pylint" requirements-dev.txt`, then run whichever linter is listed on the new/modified files. If none is listed, skip.

Example (ruff):
```bash
ruff check slack_api.py inline_agents.py review_requests.py windows.py db.py worker.py tests/
```

- [ ] **Step 4: Cleanup**

```bash
rm -f /tmp/claudia_review_tests.txt
```

---

## Self-Review (completed by plan author before handoff)

- **Spec §2.1 triggers** — Task 10 classifier + Task 11 tests cover both trigger shapes and all explicit non-triggers.
- **Spec §2.2 debounce** — Task 3 claim helper enforces one-per-session via PK; Task 11 tests second-delta suppression.
- **Spec §2.3 digest firing** — Task 13 wires the `True→False` transition; Task 13 test covers cold-start no-fire.
- **Spec §4.1/4.2 tables** — Task 2 schema; Tasks 3+4 helpers with Python-generated UUID per codex's final note.
- **Spec §4.3 in-memory state** — Task 13 step 4 initializes `was_in_own_window`; step 5 initializes `CLAUDIA_BOT_USER_ID`.
- **Spec §4.4 retention** — Task 5 implements with `posting` rows left alone; wired into 6h cleanup (not 5min).
- **Spec §5 session-day helper** — Task 1.
- **Spec §6.2 `slack_post`** — Task 6 with all 8 classification paths tested.
- **Spec §6.3 `SLACK_REVIEW_CHANNEL`** — Task 14 docs + `_review_channel()` reads it at runtime.
- **Spec §7 agents** — Tasks 8+9. Both Sonnet, no max_turns, cwd=CLAUDIA_DIR, baked-in examples.
- **Spec §8.1 run_inline_agent** — Task 7 with strict success criteria.
- **Spec §8.2 announce path** — Task 11 implements all 9 steps incl. sanitization and failure matrix handling.
- **Spec §8.3 digest path** — Task 12 incl. draft/APPROVED exclusion, pagination boundary, empty-and-complete short-circuit.
- **Spec §8.4 startup** — Task 13 step 5.
- **Spec §9 validators + fallback** — Task 10 for the pure layer; Task 11/12 wire it into the orchestrators with the exact §9.4 failure matrix.
- **Spec §10 tests** — Tasks 1, 3, 4, 5, 6, 10, 11, 12, 13 cover every test category in §10.1–§10.6.
- **Spec §11 files touched** — matches exactly.
- **Spec §12 non-goals** — no task implements retroactive digest, auto-resume, per-reviewer routing, CI gating, age filtering, or a max_turns cap. Good.

**Type consistency check:** `claim_token` is `uuid.UUID` throughout; helper return types are consistent. `Validation` dataclass used uniformly. `slack_post` return shape `{result, ts|error}` used consistently by both orchestrators. `run_inline_agent` return shape `{result, delta}` or `{result, reason}` handled in both orchestrators the same way.

**Placeholder scan:** no TBD/TODO/"implement later" strings; every code block is concrete; every test has actual assertions.

---

Plan complete and saved to `docs/superpowers/plans/2026-04-10-slack-review-requests.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
