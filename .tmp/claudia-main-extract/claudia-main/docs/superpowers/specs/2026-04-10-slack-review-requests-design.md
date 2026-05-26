# Slack review-request notifications for Claudia's own PRs

**Status:** Design · **Date:** 2026-04-10 · **Channel:** `#artemistest` (`C012NFRM76F`)

## 1. Problem and goals

When Claudia lands new code on one of her own PRs, the team currently has no
dedicated signal that a review is wanted. Hygiene/CI/feedback noise goes to
the existing `#claudia` channel, but it's noisy and not framed as a review
request. We want Claudia to post in a dedicated review channel
(`#artemistest`) in two situations:

1. **Per-PR push:** once per own-window session, when she lands real code
   changes on one of her own PRs.
2. **Daily digest:** once per own-window session, at the end of the session,
   a single message listing every open own-PR and asking for reviews.

Both messages must read as if a real human wrote them: they must mimic the
style of recent review requests in the channel and include a 1–2 sentence
(never more) description per PR. Every PR reference is a Slack link shaped
like `<url|PR #1234 — General: Do sth xyz>`.

## 2. Scope and triggers

### 2.1 What counts as "real code changes"

Per-PR announcements fire on exactly two worker state deltas:

- `issue-implementer` success with `status == "implemented"` and a non-null
  `pr_number`. This is the initial PR-open.
- `pr-feedback-handler` success with `status == "handled"` and a non-null
  `pushed_sha`. This is fix commits landed in response to review feedback.

Explicit non-triggers: `pr-hygiene-checker`, `ci-check-handler`,
`pr-reviewer` (that's Claudia reviewing *others*' PRs), `memory-processor`,
feedback-handler calls where `pushed_sha` is null (comment-only /
dismiss-only), and any merge-develop path. Rule: *if it didn't go through
one of the two delta shapes above, it's silent*.

### 2.2 Per-session debounce (intentional)

At most one per-PR announcement per **own-window session**, where a session
is the half-open interval `[19:01 UTC, 07:00 UTC next day)`. A session is
identified by the UTC date of its `19:01` start. If a feedback push lands at
`03:00 UTC`, its session day is the *previous* calendar date.

If the same PR receives a second qualifying push later in the same session,
the second announcement is silently suppressed. The digest at window close
covers it anyway. This is deliberate — the team does not want per-push spam.

### 2.3 Daily digest firing

The digest fires on the main-loop tick where the in-memory
`was_in_own_window` flag flips from `True → False` — i.e. we just crossed
`07:00 UTC`. It is guarded by a row in the `pr_review_digests` delivery
table (see §4.2) keyed by session_day, so it can never fire twice for the
same session across restarts or across multiple ticks.

A worker started cold at e.g. `08:00 UTC` initialises `was_in_own_window`
from the first observed tick. The `True → False` transition for the session
that ended at `07:00` is never observed, so no retroactive digest fires.
This is deliberate.

## 3. Architecture overview

```
┌──────────────────┐   state delta   ┌──────────────────────────────┐
│ issue-implementer│ ──────────────▶ │ worker: _maybe_announce_review│
│ pr-feedback-handler                │   1. classify delta           │
└──────────────────┘                 │   2. INSERT ON CONFLICT       │
                                     │      (claim slot 'posting')   │
                                     │   3. run_inline_agent         │
                                     │   4. validate draft           │
                                     │   5. slack_post(...)          │
                                     │   6. finalize or release      │
                                     └──────┬───────────────────────┘
                                            │ (3) draft via state delta
                                            ▼
                                     ┌──────────────────────────────┐
                                     │ agents/review-announcer.md   │
                                     │  Sonnet, no max_turns cap    │
                                     │  180s wall-clock timeout     │
                                     │  cwd=CLAUDIA_DIR, no overlay │
                                     │  gh pr view + channel style  │
                                     │  → drafts, does NOT post     │
                                     └──────────────────────────────┘

  ┌──────────────────────────────┐
  │ worker main loop tick        │
  │  was_in_own: True → False?   │
  │  → _maybe_fire_digest        │
  │    1. INSERT pr_review_digest│
  │       (claim session)        │
  │    2. enumerate gh pr list   │
  │    3. run_inline_agent       │
  │    4. validate draft         │
  │    5. slack_post(...)        │
  │    6. finalize or release    │
  └──────┬───────────────────────┘
         │ (3) draft via state delta
         ▼
  ┌──────────────────────────────┐
  │ agents/review-digest.md      │
  │  Sonnet, no max_turns cap    │
  │  180s wall-clock timeout     │
  │  cwd=CLAUDIA_DIR, no overlay │
  │  channel style + per-PR prose│
  │  → drafts, does NOT post     │
  └──────────────────────────────┘
```

Delivery is a short-transaction state machine: **claim → external work →
finalize or release**. Transactions never span LLM or Slack calls.

## 4. State model

### 4.1 Per-PR delivery-state table

```sql
CREATE TABLE pr_review_announcements (
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
```

**Claim** is a short tx. The `claim_token` is generated in Python
(`uuid.uuid4()`) and passed as a bound parameter, so no `pgcrypto` /
`gen_random_uuid()` dependency is introduced:
```sql
INSERT INTO pr_review_announcements
    (repo, pr_number, session_day, status, claim_token)
VALUES (%s, %s, %s, 'posting', %s)
ON CONFLICT DO NOTHING
RETURNING claim_token;
```
A returned `claim_token` means we just claimed the slot; absence means
somebody already claimed it and we silently return. The tx commits
immediately — no LLM or Slack work happens inside it.

**Finalize** (on Slack `result == "ok"`):
```sql
UPDATE pr_review_announcements
SET status = 'posted', posted_at = now(), slack_ts = %s
WHERE repo = %s AND pr_number = %s AND session_day = %s
  AND claim_token = %s;
```

**Release** (on Slack `result == "definite_failure"`, agent failure path
that still triggers template fallback — see §9 — this path only releases
when even the fallback post hits Slack definite_failure):
```sql
DELETE FROM pr_review_announcements
WHERE repo = %s AND pr_number = %s AND session_day = %s
  AND claim_token = %s AND status = 'posting';
```

**Ambiguous Slack failure** and **DB finalize failure** leave the row in
`status = 'posting'`. `slack_alert` fires loudly. There is no automatic
resume — manual intervention via logs is the expected path for those rare
cases, because auto-retry on ambiguous failure is how double-posts happen.

### 4.2 Digest delivery-state table

```sql
CREATE TABLE pr_review_digests (
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

Same lifecycle as §4.1. `pr_count` records how many PRs the digest
mentioned. `partial = TRUE` marks sessions where one or more repo
enumerations failed and the digest was posted with a clearly-labeled
partial list (see §8.2).

Empty-but-complete case: if every repo enumerated cleanly and the merged
list is empty, we INSERT a row at `status = 'posted'` with `pr_count = 0`
in a single tx and do not invoke the agent. This claims the session
without blocking future session detection.

### 4.3 In-memory worker state

- `was_in_own_window: bool` — initialised on the first main-loop tick to
  `is_allowed_now("implement", now_utc())`. Updated every tick. Used to
  detect the `True → False` transition that fires the digest.
- `CLAUDIA_BOT_USER_ID: str | None` — set once at worker startup by calling
  Slack `auth.test`. If the call fails, stays `None`; agents interpret
  `None` as "skip channel-style fetch entirely" (see §7).

No other cross-tick state. A worker restart re-initialises from the
current window state and the persistent delivery tables.

### 4.4 Retention

Rows older than 60 days are deleted from both tables by the existing 6-hour
maintenance path (same call site as `recover_stale_jobs` but in the slower
cleanup loop — **not** the 5-minute stale-job recovery path). `posting`
rows are left alone by retention so stuck cases remain visible in the DB
for the full 60-day window.

## 5. Session-day computation

New pure helper in `windows.py`:

```python
def current_own_session_day(now: datetime) -> date:
    """UTC date of the 19:01 own-window start for the session `now` belongs to.

    - Now in [19:01, 24:00) → today.
    - Now in [00:00, 07:00) → yesterday.
    - Now in [07:00, 19:01) → date of the most recent 19:01 start,
      i.e. yesterday (since today's 19:01 hasn't happened yet).

    Equivalent rule: if now.time() >= 19:01 → today, else → yesterday.
    """
```

Pure function, no DB, no IO, trivially unit-testable. Asserts UTC-aware
input like every other helper in `windows.py`.

## 6. Slack plumbing

### 6.1 Existing (unchanged)

`slack.py` / `utils.slack_send()` remain exactly as they are — fire-and-
forget CLI used by every current agent via `python3 slack.py 'msg'`. Agents
keep using this for their existing `#claudia` summary posts. No `--channel`
flag is added; no existing call sites are touched.

### 6.2 New structured Slack helper

New in-process helper in a new module `slack_api.py`:

```python
def slack_post(text: str, channel: str, *, timeout: float = 10.0) -> dict:
    """Post a message to Slack. Returns one of:
        {"result": "ok", "ts": "<slack ts>"}
        {"result": "definite_failure", "error": "<reason>"}
        {"result": "ambiguous_failure", "error": "<reason>"}

    - Always sets `unfurl_links=False`.
    - Reads SLACK_BOT_TOKEN from the environment.
    - `definite_failure` = Slack API responded with `ok:false`, or the
      request failed before any bytes could plausibly have reached Slack
      (DNS failure, connection refused, invalid_auth, channel_not_found).
    - `ambiguous_failure` = request may have been delivered but we didn't
      see a conclusive response (socket timeout, connection reset mid-
      request, unparseable response body after a 2xx header).
    - Classification is "observable uncertainty", not a literal
      headers-sent check — the transport layer may not expose that
      cleanly. Default to `ambiguous_failure` whenever we cannot rule out
      that Slack accepted the message.
    - Raises only on programming errors (missing token / invalid channel
      type).
    """
```

The review-announcer/digest paths call `slack_post()` directly from the
worker process — no subprocess hop. Fire-and-forget `slack_send()` is
**not** used for review channel posts because we need the ts and error
classification.

### 6.3 `SLACK_REVIEW_CHANNEL` env var

`SLACK_REVIEW_CHANNEL`, defaulting to `C012NFRM76F`. Documented in
`.env.example` and the `README.md` env table. Read once at module import
in the worker; passed to the agents as `{{SLACK_REVIEW_CHANNEL}}` and to
`slack_post(channel=...)` for actual posting.

### 6.4 Slack scopes

`conversations.history` requires `channels:history` / `groups:history`
scopes; `auth.test` requires no extra scopes. Both are already granted to
Claudia's Slack bot — no manual config change needed.

## 7. Agents

Two new files under `agents/`. Both have frontmatter:

```
name: review-announcer    # or review-digest
tools: Bash, Read
model: sonnet
# NOTE: no max_turns field → no cap (per user instruction; safety via 180s
# wall-clock timeout enforced by run_inline_agent in §8)
```

Both run `cwd=CLAUDIA_DIR` (not inside a repo worktree). Neither loads any
repo `agent-overlay.md` or repo-specific instructions — they are pure
drafting agents, and isolation from repo-specific coding guidance is
deliberate. Neither agent posts to Slack itself — they only draft.

### 7.1 `agents/review-announcer.md`

**Placeholders supplied by worker:** `{{REPO}}`, `{{PR_NUMBER}}`,
`{{PR_URL}}`, `{{SANITIZED_TITLE}}`, `{{SLACK_REVIEW_CHANNEL}}`,
`{{CLAUDIA_BOT_USER_ID}}` (may be literal `null`),
`{{SKIP_CHANNEL_STYLE_FETCH}}` (`true` or `false`), `{{CLAUDIA_DIR}}`.

**Phases:**

1. **Fetch PR context.**
   `gh pr view {{PR_NUMBER}} --repo {{REPO}} --json number,title,body,url,additions,deletions,changedFiles,files`

2. **Fetch channel style** (only if `{{SKIP_CHANNEL_STYLE_FETCH}} == false`).
   Call
   `https://slack.com/api/conversations.history?channel={{SLACK_REVIEW_CHANNEL}}&limit=30`
   with `Authorization: Bearer $SLACK_BOT_TOKEN`. Filter to messages whose
   `text` contains a `github.com/*/pull/` link; drop any where
   `user == {{CLAUDIA_BOT_USER_ID}}` or `bot_id` matches Claudia's.
   If `{{SKIP_CHANNEL_STYLE_FETCH}} == true`, skip this phase entirely and
   rely on the baked-in examples + neutral tone.

3. **Draft 1–2 sentences** describing what the PR does, grounded strictly
   in the PR title, body, and file list. Hard cap: two sentences, no more.
   Match the tone of the filtered channel messages. If the channel is
   quiet or off-topic, default to a neutral plain tone. No emojis beyond
   what the team uses. No `@` mentions, no `<!here>` / `<!channel>` /
   `<!everyone>`. Do not invent features not in the PR diff.

4. **Output the drafted message** to the state delta. The agent does
   **not** call `slack.py` or `slack_post()`. The message MUST contain
   the exact literal
   `<{{PR_URL}}|PR #{{PR_NUMBER}} — {{SANITIZED_TITLE}}>`.

5. **State delta** (parsed by worker):
   ````
   ```state_delta
   {"type":"review_announce","repo":"{{REPO}}","pr_number":{{PR_NUMBER}},"message":"<drafted message>"}
   ```
   ````

**In-prompt examples** (baked into the agent prompt so it has a baseline
when channel history is empty / filtered out; adjust once real
`#artemistest` traffic exists):

```
<https://github.com/.../pull/1234|PR #1234 — Communication: Fix notification ordering>
Reorders notification delivery so course-wide announcements always land before per-thread pings. Small change in NotificationService, touches two tests.

<https://github.com/.../pull/1250|PR #1250 — Exercise: Cache participation lookups>
Adds a short-lived cache around participation fetches on the exercise dashboard to cut repeat DB hits. Behaviour unchanged for students; mainly a performance win.

<https://github.com/.../pull/1261|PR #1261 — General: Bump Hibernate to 6.4>
Routine Hibernate minor bump. Touches a handful of entity mappings where the deprecated API was still in use; no schema changes.

<https://github.com/.../pull/1272|PR #1272 — Iris: Retry transient LLM timeouts>
Wraps the Iris chat completion call in a short retry loop for 504s and connection resets. Logs are unchanged on success and noisier on retry.
```

### 7.2 `agents/review-digest.md`

**Placeholders supplied by worker:** `{{SLACK_REVIEW_CHANNEL}}`,
`{{CLAUDIA_BOT_USER_ID}}` (may be `null`), `{{SKIP_CHANNEL_STYLE_FETCH}}`,
`{{CLAUDIA_DIR}}`, `{{PARTIAL}}` (`true` or `false`),
`{{FAILED_REPOS_JSON}}` (JSON list, may be `[]`), and a JSON-encoded
`{{PR_LIST_JSON}}` of the form
`[{repo, pr_number, url, title, body_excerpt, sanitized_title}, ...]`.

The worker does the `gh pr list` + filtering + sanitization so the agent
is deterministic in input shape.

**Phases:**

1. **Parse inputs.**
2. **Fetch channel style** (gated on `{{SKIP_CHANNEL_STYLE_FETCH}}`, same
   as §7.1).
3. **Draft** a short greeting line followed by one bullet per PR with a
   1–2 sentence description. Same tone/constraint rules as §7.1. Each
   bullet's prose is capped at ~260 characters. Bullets are in the order
   the worker supplied them. No `Thanks!` footer (see §9).
4. **If `{{PARTIAL}} == true`,** include an unmistakable partial-label
   line at the top, naming the failed repos from `{{FAILED_REPOS_JSON}}`.
   Example: `⚠️ Partial digest — could not enumerate ls1intum/Foo, ls1intum/Bar.`
   The label must be present or the validator rejects the draft.
5. **Output the drafted message** to the state delta. The agent does
   **not** post.
6. **State delta:**
   ````
   ```state_delta
   {"type":"review_digest","count":<N>,"partial":<true|false>,"message":"<drafted message>"}
   ```
   ````

**In-prompt example (non-partial):**

```
Good morning! A few open PRs that could use a review when you have a moment:

• <url|PR #1234 — Communication: Fix notification ordering>
  Reorders notification delivery so course-wide announcements always arrive before per-thread pings. Small patch, two touched tests.
• <url|PR #1250 — Exercise: Cache participation lookups>
  Adds a short-lived cache on the exercise dashboard participation query to cut repeat DB reads. No user-visible behaviour change.
• <url|PR #1261 — General: Bump Hibernate to 6.4>
  Minor Hibernate bump with small mapping tweaks. No schema changes, nothing risky.
```

**In-prompt example (partial):**

```
⚠️ Partial digest — could not enumerate ls1intum/Foo, ls1intum/Bar.

Good morning! Open PRs I could enumerate this session:

• <url|PR #1234 — Communication: Fix notification ordering>
  Reorders notification delivery so course-wide announcements always arrive before per-thread pings. Small patch, two touched tests.
```

The greeting never references dates — PRs may have been open for a long
time.

## 8. Worker integration

### 8.1 `run_inline_agent` helper

New helper (new module `inline_agents.py`, or placed in `worker.py`
alongside `run_claude_with_heartbeat` — implementation choice deferred to
the plan, but the function is a separate unit):

```python
def run_inline_agent(
    agent_name: str,
    placeholders: dict[str, str],
    *,
    timeout_seconds: int = 180,
) -> dict:
    """Run an agent inline (not via the job queue).

    Reads agents/<agent_name>.md, substitutes placeholders, invokes
    run_claude_with_heartbeat with cwd=CLAUDIA_DIR, no repo overlay, and
    the given wall-clock timeout.

    Returns one of:
        {"result": "ok", "delta": <parsed state_delta dict>}
        {"result": "agent_failure", "reason": "<exit/timeout/no_delta/malformed/type_mismatch/empty_message>"}

    Strict success criteria (stricter than the job runner):
      - exit_code == 0
      - exactly one parseable state_delta fenced block in stdout
      - delta has the expected `type` field (caller supplies expected)
      - delta has a non-empty `message` string field
    Anything else → agent_failure with a specific reason tag.

    Does NOT use AGENT_MAP, build_agent_prompt, the job queue, or any
    repo-specific overlays. Does NOT assume a repo worktree exists.
    """
```

The 180s wall-clock timeout is the safety valve that replaces a
`max_turns` cap (per user instruction).

### 8.2 Per-PR announcement path

Inside the existing post-delta handler, after a successful state-delta
parse and persist, call:

```python
_maybe_announce_review(conn, repo, state_delta, now_utc())
```

Lives in a new `review_requests.py` module for isolation from the
2600-line `worker.py`.

**Steps:**

1. Return unless delta is `issue-implementer` with `pr_number`, or
   `pr-feedback-handler` with `status == "handled"` and non-null
   `pushed_sha`.
2. `session_day = windows.current_own_session_day(now)`.
3. `claim_token = db.claim_pr_review_slot(conn, repo, pr_number, session_day)`.
   If `None`, silent return.
4. Resolve `pr_url` (from delta if present; otherwise
   `gh pr view --json url`).
5. Sanitize `sanitized_title` — strip ASCII control chars, escape Slack
   mrkdwn metacharacters (`&` → `&amp;`, `<` → `&lt;`, `>` → `&gt;`),
   truncate to 140 chars with `…`, strip single quotes / backticks /
   dollar signs for shell safety.
6. `agent_result = run_inline_agent("review-announcer", {...placeholders
   with CLAUDIA_BOT_USER_ID and SKIP_CHANNEL_STYLE_FETCH filled in from
   worker state...}, timeout_seconds=180)`.
7. If `agent_result["result"] == "ok"`, **validate** the drafted message
   via `_validate_announce_message` (§9). If validation is hard-reject or
   `agent_result["result"] == "agent_failure"`, build the **plain
   template fallback** for this PR (§9.3). Otherwise use the drafted
   message as-is.
8. `slack_result = slack_post(message, SLACK_REVIEW_CHANNEL)`.
9. Handle per the failure matrix in §9.4. Finalize, release, or leave
   `posting` accordingly.

### 8.3 Main-loop digest hook

In the existing main-loop body, alongside the window-nap logic:

```python
now = now_utc()
is_in_own = is_allowed_now("implement", now)
if was_in_own_window is True and is_in_own is False:
    _maybe_fire_digest(conn, now)
was_in_own_window = is_in_own
```

`_maybe_fire_digest` (also in `review_requests.py`):

1. `session_day = windows.current_own_session_day(now)`.
2. `claim_token = db.claim_pr_review_digest(conn, session_day)`.
   If `None` (already claimed), silent return.
3. Enumerate open own-PRs per repo:
   ```
   gh pr list --repo <r> --author <GITHUB_USER> --state open
              --limit 200
              --json number,title,url,body,isDraft,reviewDecision
   ```
   Filter out: `isDraft == true`, `reviewDecision == "APPROVED"`.
   Track per-repo outcomes: `ok_list`, `failed_repos`. If any repo's `gh`
   call fails (nonzero exit, parse error, timeout), add it to
   `failed_repos` and continue.
   If any repo returned exactly 200 items, add a warning and treat the
   enumeration for that repo as "truncated" — still use the 200 items we
   got, but add the repo to `failed_repos` so the digest is marked
   partial.
4. Flatten `ok_list` into a single PR list sorted by `(repo, pr_number)`.
   Truncate each `body` to a ~400-char excerpt for the agent prompt.
   Sanitize each `title` into `sanitized_title` as in §8.2 step 5.
5. **Empty and complete:** `ok_list` empty and `failed_repos` empty →
   short tx: `UPDATE pr_review_digests SET status='posted',
   posted_at=now(), pr_count=0 WHERE session_day=... AND
   claim_token=...`. Done, no agent call.
6. Otherwise: call
   `run_inline_agent("review-digest", {..., PARTIAL: bool(failed_repos),
   FAILED_REPOS_JSON: json.dumps(failed_repos), PR_LIST_JSON: ...}, ...)`.
7. On agent ok → validate (§9). On validation hard-reject or agent
   failure, build the plain-template digest fallback (§9.3). Otherwise
   use the drafted message. In both the fallback and the drafted cases,
   if `failed_repos` is non-empty, the partial label is added (in the
   fallback, the worker adds it; in the drafted case, the validator
   requires the agent to include it).
8. `slack_result = slack_post(message, SLACK_REVIEW_CHANNEL)`.
9. Handle per §9.4. On finalize, set `pr_count = len(ok_list)`, `partial
   = bool(failed_repos)`.

### 8.4 Worker startup additions

- Initialize `was_in_own_window = is_allowed_now("implement", now_utc())`.
- Call Slack `auth.test` once; set module global
  `CLAUDIA_BOT_USER_ID` from the response `user_id`. On failure, set
  `None` and log a warning.
- Both agents then receive `CLAUDIA_BOT_USER_ID` and
  `SKIP_CHANNEL_STYLE_FETCH = (CLAUDIA_BOT_USER_ID is None)` on every
  invocation.

## 9. Validation and fallback

### 9.1 Per-PR validator `_validate_announce_message`

**Hard reject** (returns `invalid`, triggers template fallback):

- Message does not contain the exact literal
  `<{PR_URL}|PR #{PR_NUMBER} — {SANITIZED_TITLE}>`.
- Message contains any `github.com/*/pull/` link other than the expected
  one.
- Message contains any Slack mention: `<@U[A-Z0-9]+>`, `<!here>`,
  `<!channel>`, `<!everyone>`, `<!subteam^...>`.

**Warn-only** (message is still posted as-is):

- Prose > 280 chars outside the link.
- More than two sentences of prose (counted by `.`, `!`, `?` outside
  backticked spans and URLs; heuristic, false positives acceptable).
- Contains emojis not seen in the sampled channel history (if history
  was fetched).

### 9.2 Digest validator `_validate_digest_message`

**Hard reject:**

- For each expected PR, the exact Slack-link literal
  `<{url}|PR #{num} — {sanitized_title}>` must appear somewhere in the
  message.
- No unexpected `github.com/*/pull/` links.
- No Slack mentions (same list as §9.1).
- If the digest is partial (`PARTIAL == true`), the message must contain
  a line explicitly labeled partial — matched by a regex like
  `(?i)partial\s+digest` — AND every repo in `FAILED_REPOS_JSON` must
  be named somewhere in the message.

**Warn-only:**

- Per-bullet prose > 260 chars (measured as the text between one bullet's
  PR link and the next bullet or end-of-message).
- More than two sentences per bullet.
- Greeting line and any leading partial label are **not** subject to
  sentence-count or length checks.

### 9.3 Plain template fallback

**Per-PR fallback:**
```
:mag: Review please — <{pr_url}|PR #{pr_number} — {sanitized_title}>
```

**Digest fallback (non-partial):**
```
:sunrise: Open PRs that could use a review:
• <{url1}|PR #{num1} — {title1}>
• <{url2}|PR #{num2} — {title2}>
...
```

**Digest fallback (partial):**
```
:warning: Partial digest — could not enumerate {failed_repo_1}, {failed_repo_2}, ...

:sunrise: Open PRs I could enumerate this session:
• <{url1}|PR #{num1} — {title1}>
...
```

Fallbacks are posted via `slack_post(channel=SLACK_REVIEW_CHANNEL)`, same
delivery path as the drafted message.

### 9.4 Failure matrix

| Scenario | Action |
|---|---|
| Agent ok, validator ok | Post drafted message via `slack_post`. |
| Agent ok, validator warn-only | Post drafted message via `slack_post`. Log warning. |
| Agent ok, validator hard-reject | Post template fallback via `slack_post`. `slack_alert` with the raw draft + specific reason. |
| Agent `agent_failure` | Post template fallback via `slack_post`. `slack_alert` with the failure reason. |
| Slack `result == "ok"` | Short tx: finalize row to `posted` with `slack_ts`. On UPDATE failure → `slack_alert`, leave row in `posting`. |
| Slack `result == "definite_failure"` | Short tx: DELETE row. `slack_alert` with the error. Next trigger (or next session for digest) will retry. |
| Slack `result == "ambiguous_failure"` | Leave row in `posting`. `slack_alert` loudly. No auto-retry. |
| Post of fallback itself fails with `definite_failure` | DELETE row. `slack_alert`. |
| Post of fallback itself fails with `ambiguous_failure` | Leave in `posting`. `slack_alert`. |

The fallback path guarantees that if Slack is available at all, a review
request or digest **does** land — agent glitches cannot silently drop a
notification. Only Slack failure can.

## 10. Testing

### 10.1 Pure unit tests (no DB, no IO)

- `windows.current_own_session_day` across boundaries: `18:00`, `19:00`,
  `19:01`, `23:59`, `00:00`, `06:59`, `07:00`, `07:01`, `12:00`, `18:59`.
- `_validate_announce_message`: table-driven — good message, missing
  exact PR link, extra unexpected PR link, `<@U...>` mention, `<!here>`,
  `<!channel>`, over-length prose (warn-only), > 2 sentences (warn-only).
- `_validate_digest_message`: good non-partial, good partial with correct
  label + failed repos named, partial missing label (hard-reject),
  partial with failed repo unnamed (hard-reject), per-bullet over-length
  (warn-only).
- Delta classifier predicate: every state-delta shape in the codebase
  maps to expected `True`/`False`, including `pr-feedback-handler` with
  `pushed_sha == null`.
- Title sanitization: control chars stripped, mrkdwn escaped, truncation,
  shell-metachar stripped.
- Plain template renderers for per-PR and digest (non-partial + partial).
- `slack_post` classification (mocked HTTP layer): Slack 200 ok → `ok`,
  Slack 200 `ok:false` → `definite_failure`, HTTP 401/403 →
  `definite_failure`, `channel_not_found` → `definite_failure`, socket
  timeout mid-request → `ambiguous_failure`, connection reset after
  sending body → `ambiguous_failure`, DNS failure →
  `definite_failure`.

### 10.2 DB tests (real Postgres, existing integration-test pattern)

- `claim_pr_review_slot` first call returns a UUID; second identical call
  returns `None`; different session_day returns a UUID; different
  pr_number returns a UUID.
- Finalize on claim_token matches row; finalize with stale claim_token
  leaves row alone (defensive).
- Release on claim_token deletes row only if still in `posting`.
- `claim_pr_review_digest` same three shapes.
- 60-day retention deletes rows with `session_day < now - 60 days` and
  leaves `posting` rows alone.
- Concurrent claim race: two connections each try `claim_pr_review_slot`
  for the same `(repo, pr, session_day)` — exactly one gets a UUID.
  Same for digest.

### 10.3 Worker-loop tests with fake clock

- Digest fires exactly once on the `07:00` `True → False` transition.
- Digest does not fire again on subsequent ticks the same day.
- Digest does not fire when worker starts cold at `08:00` (no transition
  observed).
- Per-PR announce fires on the first qualifying `issue-implementer`
  delta.
- Second identical delta in the same session is suppressed.
- Per-PR announce is suppressed when `pr-feedback-handler` `pushed_sha`
  is null.
- Empty-and-complete digest path writes a `posted` row with `pr_count=0`
  and does **not** invoke the agent.
- Partial-enumeration digest path sets `partial=TRUE` and the message
  contains the partial label + failed repo names.
- Pagination boundary: a repo returning exactly 200 items is treated as
  truncated and marked partial.

### 10.4 Agent-invocation boundary

Mocked at the `run_inline_agent` boundary:
- Agent ok with valid delta → drafted message is posted.
- Agent ok with `type` mismatch → template fallback posted,
  `slack_alert` fired.
- Agent ok with empty `message` → template fallback.
- Agent ok with malformed JSON → template fallback.
- Agent nonzero exit → template fallback.
- Agent timeout → template fallback.
- Validator hard-reject → template fallback (not drafted message).
- Validator warn-only → drafted message is posted, warning logged.
- Draft-exclusion: a repo with one draft + one non-draft → digest
  contains only the non-draft.
- APPROVED exclusion: a repo with one approved + one open → digest
  contains only the open.

### 10.5 Slack-delivery boundary

Mocked at the `slack_post` boundary:
- `result: ok` → row is finalized with `slack_ts`.
- `result: definite_failure` → row is DELETED; alert fired.
- `result: ambiguous_failure` → row stays `posting`; alert fired.
- Post succeeded but subsequent DB finalize UPDATE raises → row stays
  `posting`; alert fired; no duplicate post.

### 10.6 Shell-safety

- Title sanitization strips single quotes, backticks, dollar signs, and
  control characters.
- Fuzz a handful of adversarial PR titles through the sanitizer + shell
  interpolation path used in any `gh` calls we still build as strings.

No live Slack calls or live `gh` calls in any test — both mocked.

## 11. Files touched

**New:**

- `review_requests.py` — classifier, session-day wrapper, claim/finalize/
  release wrappers over `db.py`, enumeration + filtering, validators,
  fallback renderers, orchestration of `_maybe_announce_review` and
  `_maybe_fire_digest`.
- `slack_api.py` — in-process `slack_post()` helper with error
  classification.
- `inline_agents.py` OR new helper in `worker.py` — `run_inline_agent()`
  (implementation placement decided in the plan).
- `agents/review-announcer.md`.
- `agents/review-digest.md`.
- `tests/test_review_requests.py` (pure unit).
- `tests/test_review_requests_db.py` (Postgres).
- `tests/test_slack_post.py` (mocked HTTP).
- `tests/test_worker_digest.py` (fake-clock worker-loop transition).

**Modified:**

- `windows.py` — add `current_own_session_day`.
- `db.py` — migration for `pr_review_announcements` and
  `pr_review_digests`; helper functions `claim_pr_review_slot`,
  `finalize_pr_review_slot`, `release_pr_review_slot`, and the digest
  counterparts; 60-day retention helper wired into the existing 6-hour
  cleanup path (not the 5-minute stale-job path).
- `worker.py` — call `_maybe_announce_review` from the post-delta path;
  call `_maybe_fire_digest` from the main-loop `True → False` transition;
  initialize `was_in_own_window` and `CLAUDIA_BOT_USER_ID` at startup.
- `.env.example`, `README.md` — document `SLACK_REVIEW_CHANNEL`.

**Explicitly NOT touched:**

- `slack.py` / `utils.slack_send()` — unchanged. All existing agents
  keep using them as-is for `#claudia` posts.
- `AGENT_MAP` / `build_agent_prompt` — unchanged. Inline agents bypass
  them entirely.
- Repo agent-overlay loading — unchanged. Inline agents do not use
  overlays.

## 12. Non-goals

- Threaded replies / follow-up pings on PRs that sit open for many days.
- Per-reviewer mentions or routing.
- Posting anything when Claudia's PR is approved, merged, or closed
  (approved PRs are *excluded* from the digest; there is no separate
  "thanks" message).
- Retroactive digests after a worker restart that missed the transition.
- Automatic resume of stuck `posting` rows (ambiguous Slack failures are
  handled via alerts + manual intervention).
- Reacting to pushes made by humans on Claudia's PRs — triggers are
  strictly worker-side state deltas.
- True `gh pr list` pagination beyond the `--limit 200` first-cut. If
  Claudia ever has > 200 open PRs in one repo, the digest will mark
  itself partial and alert.
- CI-state gating of digest entries.
- Age-based filtering.
- A `max_turns` cap on the drafting agents (per user instruction; safety
  via 180s wall-clock timeout).
