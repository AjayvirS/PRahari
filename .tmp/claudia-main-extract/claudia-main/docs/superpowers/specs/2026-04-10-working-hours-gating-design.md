# Working-hours gating for Claudia

**Date:** 2026-04-10
**Status:** Approved design

## Problem

Claudia currently processes every job the moment it becomes eligible, regardless of wall-clock time. We want her active only during defined UTC windows, with different windows per category of work:

- **Own-PR work** (feedback, CI failures, hygiene, issue implementation, memory housekeeping): allowed during the interval `[19:01, 07:00)` UTC (night window, wraps midnight, half-open — 07:00:00 is already *outside* the window).
- **Reviewing other people's PRs** (`review` jobs): allowed during the interval `[19:01, 12:30)` UTC (wider window, wraps midnight, same half-open semantics).
- Outside those windows the corresponding job types must not run — incoming events are still accepted but their execution is deferred to the next allowed window.

"GMT" in the requirements is treated as UTC. No DST handling.

## Job-type mapping

| Job type    | Window        | Rationale                           |
|-------------|---------------|-------------------------------------|
| `feedback`  | Own (A)       | Reacting to reviews on own PRs      |
| `ci_check`  | Own (A)       | CI failures on own PRs              |
| `hygiene`   | Own (A)       | Housekeeping on own PRs             |
| `implement` | Own (A)       | Creating new PRs from issues        |
| `memory`    | Own (A)       | Knowledge-file maintenance          |
| `review`    | Review (B)    | Reviewing other contributors' PRs   |

## Architecture

Two gates enforce the windows. The claim-time gate is the authoritative one; the enqueue-time gate is advisory for visibility and Slack messaging.

### Gate 1 — Enqueue-time `run_after` clamp (advisory)

At enqueue, compute `next_allowed_after(job_type, now_utc())`. If the window is currently closed for that type, clamp the job's `run_after` forward to the next window start. Purpose:

- Sensible DB state for humans inspecting pending jobs (shows a realistic ETA).
- Dedup-merge behaviour: the `ON CONFLICT … DO UPDATE` path must not regress an already-later `run_after` set by backoff/backpressure.

**Not authoritative.** Retry/release/requeue/recover code paths in `db.py` do not clamp, and window transitions can invalidate a stale `run_after`. Only Gate 2 is trusted for correctness.

### Gate 2 — Claim-time SQL-inlined window predicate (authoritative)

`claim_next_job` embeds the window check directly in the SQL `WHERE` clause, so the window decision and the claim happen in one atomic statement under the database clock. This eliminates the race where Python computes `allowed_types` at `12:29:59.9` and the SQL claim runs at `12:30:00.1`.

The SQL additionally accepts a Python-computed `allowed_types: list[str]` parameter as a fast-path pre-filter (skips the SQL entirely when *no* type is currently allowed — the all-blocked nap case), but the SQL window predicate is independent and authoritative.

**Time injection for deterministic tests.** `claim_next_job` also takes `check_time: datetime | None = None`. The SQL uses `COALESCE(%(check_time)s, now())` exactly once as the authoritative timestamp, and both the `run_after <= ...` comparison and the time-of-day window predicate are derived from it. In production `check_time` is always `None` and `now()` is used. Tests pass a fixed UTC datetime to exercise specific window transitions without needing wall-clock or a time-freeze extension.

Exact claim inner `SELECT`:

```sql
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
```

Every known job type is listed explicitly. A new future job type falls to `ELSE FALSE` and is blocked until its window is added — safer than defaulting to the own-window as a fallback.

Note the explicit `::job_type[]` cast on `allowed_types` — `jobs.type` is a Postgres enum and `text[] = ANY` is not a safe implicit comparison.

The window boundary values are defined as Python `time` constants in `windows.py` and also as SQL literals in the claim query. A unit test asserts the two stay in sync (imports the constants from `windows.py` and checks that they match strings exported from `db.py`).

### In-flight jobs

A job that is already claimed when its window closes runs to completion. The 12:30 UTC review cutoff gives roughly 30 minutes of drain time before 13:00 UTC — this is an advisory buffer, not an enforced hard boundary. The design does not abort or preempt running jobs. If a long-running job bleeds well past 13:00, that is accepted behaviour.

## Components

### New: `windows.py`

Pure module, no DB or IO. Public surface:

```python
OWN_WINDOW_START   = time(19, 1)   # inclusive
OWN_WINDOW_END     = time(7, 0)    # exclusive
REVIEW_WINDOW_START = time(19, 1)  # inclusive
REVIEW_WINDOW_END   = time(12, 30) # exclusive

JOB_TYPE_WINDOWS: dict[str, tuple[time, time]] = {
    "feedback": (OWN_WINDOW_START, OWN_WINDOW_END),
    "ci_check": (OWN_WINDOW_START, OWN_WINDOW_END),
    "hygiene":  (OWN_WINDOW_START, OWN_WINDOW_END),
    "implement":(OWN_WINDOW_START, OWN_WINDOW_END),
    "memory":   (OWN_WINDOW_START, OWN_WINDOW_END),
    "review":   (REVIEW_WINDOW_START, REVIEW_WINDOW_END),
}

def next_allowed_after(job_type: str, now: datetime) -> datetime | None
def next_allowed_for_types(types: Iterable[str], now: datetime) -> datetime | None
def is_allowed_now(job_type: str, now: datetime) -> bool
```

- `next_allowed_after`: returns `None` if the job type is allowed at `now` (half-open semantics), else the UTC datetime at which the next window opens for that type.
- `next_allowed_for_types`: returns the *earliest* next-allowed datetime across a set of types (used for the nap message target). Returns `None` if any of the types are already allowed.
- All inputs must be timezone-aware UTC datetimes; function asserts this.

### Changed: `db.py`

- `enqueue_job` gains `min_run_after: datetime | None = None`. SQL uses the exact same expression in both paths so behaviour is identical whether the row is fresh or merged:
  - INSERT path:
    ```sql
    run_after = GREATEST(
        now() + make_interval(secs := %(debounce)s),
        COALESCE(%(min_run_after)s, now())
    )
    ```
  - `ON CONFLICT DO UPDATE` path:
    ```sql
    run_after = GREATEST(
        jobs.run_after,
        now() + make_interval(secs := %(debounce)s),
        COALESCE(%(min_run_after)s, now())
    )
    ```
  Including `jobs.run_after` in the UPDATE branch prevents pulling a backoff-deferred job earlier. `COALESCE(min_run_after, now())` handles `NULL` correctly — it degenerates to `now()`, which is always ≤ the other `GREATEST` operands so it has no effect.
- `claim_next_job` gains `allowed_types: list[str]` and `check_time: datetime | None = None`. Callers in production pass `check_time=None`; tests pass a fixed UTC datetime. The SQL embeds the window predicate as shown above with the explicit `::job_type[]` cast.
- Retry/release/requeue/recover helpers (`retry_job`, `release_job`, `requeue_job`, `recover_stale_jobs`) are **not** modified to clamp. Gate 2 covers correctness. A short comment in each notes that clamping is Gate 2's job.

### Changed: `webhook_receiver.py`

The `db.enqueue_job(conn, **job)` call at line ~324 passes `min_run_after=windows.next_allowed_after(job["job_type"], datetime.now(timezone.utc))`.

Note: the webhook's classify functions build dicts with key `job_type` (not `type`) — matching `enqueue_job`'s parameter name. Use the correct key.

### Changed: `worker.py`

**Enqueue call sites** (six sites per `grep enqueue_job`) each pass `min_run_after` computed from the job type just before the call.

**Main loop nap state machine:**

1. Compute `allowed_types = [t for t in db.JOB_TYPES if windows.is_allowed_now(t, now_utc())]`.
2. If `allowed_types` is empty → skip the claim call and go directly to the nap branch with state = `window_blocked`.
3. Else call `db.claim_next_job(conn, worker_pid, allowed_types)`. If it returns a job → process normally.
4. Nap branch classifies into one of three states and acts:
   - **Empty queue** (`db.pending_by_type(conn)` returns empty dict) → existing idle Slack message, unchanged.
   - **Window-blocked** (pending dict non-empty, but none of its keys intersect `allowed_types`) → new window-nap Slack message. Target datetime = `windows.next_allowed_for_types(pending_types, now_utc())` where `pending_types` is the set of keys in `pending_by_type`'s result. Message: `"😴 Sleeping until HH:MM UTC — outside my working hours"`. Track announced target in `window_sleep_announced_until: datetime | None`; re-announce only when the target changes.
   - **Debounce-only** (pending dict intersects `allowed_types`, but claim returned nothing because all those jobs have `run_after > now()`) → silent sleep for `POLL_INTERVAL`. This is normal debounce behaviour and was already silent before this change.
5. On successful claim, reset `window_sleep_announced_until = None` and `idle_announced = False`. Implicit wake via the existing `⚙️ …` job-start message — no explicit "good morning" (matches the quota-nap pattern).

**Use existing `pending_by_type` helper.** No new `pending_types` helper — the codebase already has `db.pending_by_type(conn)` at `db.py:705`. Take `set(result.keys())` at the call site.

## Testing

Three tiers:

### Unit tests — `tests/test_windows.py`

Pure tests on `windows.py`:

- Boundary behaviour for each window (half-open): at `07:00:00`, `07:00:00.001`, `06:59:59.999`, `19:00:59.999`, `19:01:00`, `12:29:59.999`, `12:30:00` — assert expected allowed/blocked and expected `next_allowed_after` result.
- Midnight wrap: `00:00`, `03:00`, `23:59` for both windows.
- `next_allowed_after` returns `None` inside the window and a correctly-wrapped datetime outside.
- `next_allowed_for_types` across a mixed set returns the minimum.
- Every entry in `db.JOB_TYPES` is covered by `JOB_TYPE_WINDOWS` (mapping completeness test).
- A "constants sync" test that asserts the SQL literal strings exported from `db.py` (for documentation purposes) match `windows.py`'s constants, so a change in one flags the other.

### DB integration tests — `tests/test_db_windows.py`

Run against a real Postgres (reuse whatever fixture the repo already has; if none, spin up via `testing.postgresql` or skip-if-unavailable):

- `enqueue_job` with `min_run_after`: INSERT path clamps correctly.
- `enqueue_job` `ON CONFLICT` path: pre-existing row with later `run_after` is not regressed; pre-existing row with earlier `run_after` is clamped forward; `NULL min_run_after` behaves as before.
- `claim_next_job` at simulated window-closed and window-open times. Tests pass `check_time=datetime(2026, 4, 10, 12, 30, tzinfo=timezone.utc)` etc. to exercise each boundary deterministically. Because `check_time` flows through a single `COALESCE` into both the `run_after` comparison and the time-of-day predicate, a fixed injected datetime gives fully reproducible results — no wall-clock dependency, no Postgres time-freeze extension. Cover: exactly-at cutoff (blocked), one microsecond before cutoff (allowed), midnight wrap for both windows, `review` allowed while `feedback` blocked at 08:00 UTC, both allowed at 20:00 UTC.
- Enum cast: passing `allowed_types` as a `list[str]` actually works with the `::job_type[]` cast.
- Order correctness: highest-priority allowed-type job is picked over a higher-priority disallowed-type job.

### Worker loop test — `tests/test_worker_nap.py`

One small test (not full end-to-end) of the nap-state classification logic, extracted to a pure helper so it can be unit-tested without spinning up a worker. The helper takes `(pending_by_type_result, allowed_types, now_utc)` and returns one of `("empty", None)`, `("window_blocked", target_datetime)`, `("debounce", None)`. Test the three branches.

Extracting this helper is a small refactor but it makes the hardest-to-get-right branch trivially testable.

## Non-goals

- No new DB columns or migrations.
- No config table or per-repo overrides.
- No deferral Slack message per individual event (only on sleep-state transitions).
- No explicit "good morning" wake message.
- No aborting in-flight jobs at window close.
- No clamping inside retry/release/requeue/recover — Gate 2 covers correctness.
- No DST handling.
