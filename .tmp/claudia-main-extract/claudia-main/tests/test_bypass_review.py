"""Tests for on-demand PR review bypass (@<bot> review command).

Covers the three gates that allow a mention-command review to run
outside the normal review window:
  - Gate 1: enqueue_job's effective-bypass coalesce logic.
  - Gate 2: claim_next_job's review time-of-day CASE.
  - Gate 3: worker prefilter via pending_{ready,any}_bypass_review_exists.
Also covers the webhook classifier's command parsing + trust check.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import db
import webhook_receiver as wr


UTC = timezone.utc

# A 14:00 UTC timestamp — inside no own window and outside the review
# window (which closes at 12:30). Used throughout to exercise "outside
# review window" behaviour.
MIDDAY_UTC = datetime(2026, 4, 11, 14, 0, 0, tzinfo=UTC)


# ── Command regex / markdown sanitization ────────────────────────────

@pytest.mark.parametrize("body", [
    "@Claudia-Anthropica review",
    "Hey @Claudia-Anthropica review please",
    "@Claudia-Anthropica review.",
    "@Claudia-Anthropica review!",
    "@Claudia-Anthropica review?",
    "@Claudia-Anthropica review:",
    "@claudia-anthropica review",  # case-insensitive
    "some context\n@Claudia-Anthropica review\nthanks",
])
def test_is_review_command_matches(body):
    assert wr._is_review_command(body, "Claudia-Anthropica") is True


@pytest.mark.parametrize("body", [
    "@Claudia-Anthropica reviewed this PR",        # word boundary
    "@Claudia-Anthropica reviewing",               # word boundary
    "foo@Claudia-Anthropica review",               # no whitespace before @
    "```\n@Claudia-Anthropica review\n```",        # fenced code
    "`@Claudia-Anthropica review`",                # inline code
    "> @Claudia-Anthropica review",                # blockquote
    "  > @Claudia-Anthropica review",              # indented blockquote
    "@other-user review",                          # different user
    "",                                            # empty
])
def test_is_review_command_rejects(body):
    assert wr._is_review_command(body, "Claudia-Anthropica") is False


def test_strip_markdown_fenced_and_inline():
    body = "line one\n```\n@bot review\n```\nand `@bot review` here"
    stripped = wr._strip_markdown_for_mentions(body)
    assert "@bot review" not in stripped


# ── Classifier: on-demand command produces bypass job ────────────────

_REPO = "example/repo"
_BOT = "Claudia-Anthropica"


def _pr_comment_payload(body: str, commenter: str = "alice", bot_type: str = "User"):
    return {
        "pull_request": {
            "number": 42,
            "title": "Fix the thing",
            "user": {"login": "bob"},
            "head": {"sha": "abc123", "ref": "fix"},
            "base": {"ref": "main"},
        },
        "comment": {
            "body": body,
            "user": {"login": commenter, "type": bot_type},
            "in_reply_to_id": None,
        },
    }


def _issue_comment_payload(body: str, commenter: str = "alice"):
    return {
        "issue": {
            "number": 42,
            "title": "Fix the thing",
            "user": {"login": "bob"},
            "pull_request": {"url": "..."},
        },
        "comment": {
            "body": body,
            "user": {"login": commenter, "type": "User"},
        },
    }


def test_classify_pr_review_comment_trusted_bypass():
    payload = _pr_comment_payload(f"@{_BOT} review")
    with patch.object(wr, "GITHUB_USER", _BOT), \
         patch.object(wr, "_check_trusted_commenter", return_value=True):
        job = wr._classify_pr_review_comment(payload, _REPO)
    assert job is not None
    assert job["job_type"] == "review"
    assert job["bypass_window"] is True
    assert job["priority"] == 10
    assert job["payload"]["reasons"] == ["on_demand_command"]


def test_classify_issue_comment_trusted_bypass():
    payload = _issue_comment_payload(f"@{_BOT} review")
    with patch.object(wr, "GITHUB_USER", _BOT), \
         patch.object(wr, "_check_trusted_commenter", return_value=True):
        job = wr._classify_issue_comment(payload, _REPO)
    assert job is not None
    assert job["job_type"] == "review"
    assert job["bypass_window"] is True
    assert job["priority"] == 10
    assert job["payload"]["reasons"] == ["on_demand_command"]


@pytest.mark.parametrize("classifier,payload_factory", [
    (wr._classify_issue_comment, _issue_comment_payload),
    (wr._classify_pr_review_comment, _pr_comment_payload),
])
def test_classify_untrusted_human_declines_and_returns_none(classifier, payload_factory):
    """Untrusted human commenter with the command → funny decline
    comment posted, no job enqueued. Parity on both comment paths."""
    payload = payload_factory(f"@{_BOT} review", commenter="outsider")
    with patch.object(wr, "GITHUB_USER", _BOT), \
         patch.object(wr, "_check_trusted_commenter", return_value=False), \
         patch.object(wr, "_post_command_decline") as decline, \
         patch.object(wr, "_post_command_ack") as ack:
        job = classifier(payload, _REPO)
    assert job is None
    decline.assert_called_once()
    ack.assert_not_called()
    args, _ = decline.call_args
    assert args[0] == _REPO
    assert args[1] == 42
    assert args[2] == "outsider"


@pytest.mark.parametrize("classifier,payload_factory", [
    (wr._classify_issue_comment, _issue_comment_payload),
    (wr._classify_pr_review_comment, _pr_comment_payload),
])
def test_classify_trust_unknown_falls_through_no_decline(classifier, payload_factory):
    """Tri-state: _check_trusted_commenter returns None on API failure.
    Must NOT post a decline (could be a real maintainer) and must
    fall through to the plain-mention path. Parity on both paths."""
    payload = payload_factory(f"@{_BOT} review")
    with patch.object(wr, "GITHUB_USER", _BOT), \
         patch.object(wr, "_check_trusted_commenter", return_value=None), \
         patch.object(wr, "_post_command_decline") as decline, \
         patch.object(wr, "_post_command_ack") as ack:
        job = classifier(payload, _REPO)
    decline.assert_not_called()
    ack.assert_not_called()
    # Falls through: plain-mention path still creates a windowed review.
    assert job is not None
    assert job.get("bypass_window", False) is False
    assert "mention_comment" in job["payload"]["reasons"]


@pytest.mark.parametrize("classifier,payload_factory", [
    (wr._classify_issue_comment, _issue_comment_payload),
    (wr._classify_pr_review_comment, _pr_comment_payload),
])
def test_classify_bot_command_no_decline_no_bypass(classifier, payload_factory):
    """A bot account issuing the command must get neither a bypass
    job nor a funny decline (we don't bicker with automation).
    Parity on both paths."""
    payload = payload_factory(f"@{_BOT} review", commenter="somebot[bot]")
    with patch.object(wr, "GITHUB_USER", _BOT), \
         patch.object(wr, "_post_command_decline") as decline, \
         patch.object(wr, "_post_command_ack") as ack:
        job = classifier(payload, _REPO)
    decline.assert_not_called()
    ack.assert_not_called()
    if job is not None:
        assert job.get("bypass_window", False) is False


@pytest.mark.parametrize("classifier,payload_factory", [
    (wr._classify_issue_comment, _issue_comment_payload),
    (wr._classify_pr_review_comment, _pr_comment_payload),
])
def test_classify_trusted_bypass_posts_ack(classifier, payload_factory):
    """Trusted maintainer → bypass job AND friendly ack comment on
    both comment paths."""
    payload = payload_factory(f"@{_BOT} review", commenter="alice")
    with patch.object(wr, "GITHUB_USER", _BOT), \
         patch.object(wr, "_check_trusted_commenter", return_value=True), \
         patch.object(wr, "_post_command_ack") as ack, \
         patch.object(wr, "_post_command_decline") as decline:
        job = classifier(payload, _REPO)
    assert job is not None and job["bypass_window"] is True
    ack.assert_called_once()
    decline.assert_not_called()
    args, _ = ack.call_args
    assert args[0] == _REPO
    assert args[1] == 42
    assert args[2] == "alice"


def test_check_trusted_commenter_partial_get_trusted_users_failure():
    """Regression: if get_trusted_users raises (partial API failure),
    _check_trusted_commenter must return None (unknown), NOT False.
    This prevents a real maintainer from being misclassified as
    untrusted and getting a funny rejection on transient errors."""
    with patch("utils.get_trusted_users", side_effect=RuntimeError("API 503")):
        result = wr._check_trusted_commenter(
            _REPO, {"login": "real-maintainer", "type": "User"}
        )
    assert result is None


def test_get_trusted_users_fails_closed_on_partial_api_failure(monkeypatch):
    """get_trusted_users must raise if any permission query fails —
    returning a partial set would silently misclassify real users."""
    import utils

    class _FakeCompleted:
        def __init__(self, rc, out="", err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    def fake_run(cmd, *args, **kwargs):
        # Admin succeeds, maintain fails — the exact partial-failure scenario.
        if "permission=admin" in cmd[-1]:
            return _FakeCompleted(0, '[{"login": "admin-user"}]')
        return _FakeCompleted(1, "", "server ate my request")

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="maintain"):
        utils.get_trusted_users("x/y")


def test_funny_rejections_are_role_agnostic():
    """All rejection templates must read naturally for BOTH the issue
    assignment and the review-command use cases — no template may
    hardcode verbs like 'assign me' or 'send me on quests'."""
    from utils import FUNNY_REJECTIONS
    banned_phrases = ["send me on quests", "assign me"]
    for template in FUNNY_REJECTIONS:
        lowered = template.lower()
        for phrase in banned_phrases:
            assert phrase not in lowered, \
                f"Template has case-specific phrase {phrase!r}: {template}"
    assert len(FUNNY_REJECTIONS) >= 7


def test_classify_reviewed_word_boundary_no_match():
    payload = _issue_comment_payload(f"@{_BOT} reviewed this PR")
    with patch.object(wr, "GITHUB_USER", _BOT), \
         patch.object(wr, "_check_trusted_commenter", return_value=True):
        job = wr._classify_issue_comment(payload, _REPO)
    # reviewed → not a command match, but still a plain @mention → review job without bypass
    assert job is not None
    assert job.get("bypass_window", False) is False


def test_is_trusted_commenter_rejects_bot_type():
    assert wr._is_trusted_commenter(_REPO, {"login": "x", "type": "Bot"}) is False


def test_is_trusted_commenter_rejects_bot_suffix():
    assert wr._is_trusted_commenter(_REPO, {"login": "somebot[bot]", "type": "User"}) is False


def test_is_trusted_commenter_on_exception_returns_false():
    with patch("utils.get_trusted_users", side_effect=RuntimeError("boom")):
        assert wr._is_trusted_commenter(_REPO, {"login": "x", "type": "User"}) is False


# ── DB: enqueue_job with bypass_window ────────────────────────────────

def _fetch(conn, job_id: int) -> dict:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, run_after, priority, payload FROM jobs WHERE id = %s",
        (job_id,),
    )
    row = cur.fetchone()
    cur.close()
    return {
        "id": row[0],
        "run_after": row[1],
        "priority": row[2],
        "payload": row[3],
    }


def test_enqueue_bypass_fresh_sets_flag_and_runs_now(pg_conn):
    job_id = db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:bypass-fresh",
        payload={"repo": "x/y", "pr_number": 1},
        bypass_window=True,
    )
    row = _fetch(pg_conn, job_id)
    assert row["payload"]["bypass_window"] is True
    # Bypass forces debounce=0 and min_run_after=None, so run_after ≈ now.
    assert row["run_after"] <= datetime.now(UTC) + timedelta(seconds=2)


def test_enqueue_bypass_coalesces_over_far_future(pg_conn):
    """A pending non-bypass review with far-future run_after; a bypass
    enqueue must pull run_after back to ~now and set the flag."""
    far_future = datetime.now(UTC) + timedelta(hours=10)
    id1 = db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:bypass-coalesce",
        payload={"repo": "x/y", "pr_number": 7},
        min_run_after=far_future,
    )
    assert id1 is not None

    db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:bypass-coalesce",
        payload={"repo": "x/y", "pr_number": 7},
        priority=10,
        bypass_window=True,
    )
    row = _fetch(pg_conn, id1)
    assert row["payload"]["bypass_window"] is True
    assert row["priority"] == 10
    assert row["run_after"] <= datetime.now(UTC) + timedelta(seconds=2)


def test_enqueue_non_bypass_after_bypass_keeps_sticky_flag(pg_conn):
    """Sticky flag: a subsequent non-bypass enqueue (e.g. synchronize)
    on the same dedup_key must NOT clear bypass_window and must NOT
    push run_after forward."""
    id1 = db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:bypass-sticky",
        payload={"repo": "x/y", "pr_number": 9},
        bypass_window=True,
    )
    # Now a normal synchronize-style enqueue with a future clamp.
    future = datetime.now(UTC) + timedelta(hours=5)
    db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:bypass-sticky",
        payload={"repo": "x/y", "pr_number": 9},
        min_run_after=future,
    )
    row = _fetch(pg_conn, id1)
    assert row["payload"]["bypass_window"] is True
    # Effective-bypass OR rule → run_after stays at/earlier-than now.
    assert row["run_after"] <= datetime.now(UTC) + timedelta(seconds=2)


# ── DB: claim_next_job honors bypass flag outside window ─────────────

def _force_run_after(conn, job_id, when):
    cur = conn.cursor()
    cur.execute("UPDATE jobs SET run_after = %s WHERE id = %s", (when, job_id))
    conn.commit()
    cur.close()


def test_claim_review_bypass_claimable_outside_window(pg_conn):
    job_id = db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:claim-bypass",
        payload={"repo": "x/y", "pr_number": 1},
        bypass_window=True,
    )
    # Pin run_after strictly before check_time so the test is not
    # clock-fragile (claim_next_job compares run_after <= check_time).
    _force_run_after(pg_conn, job_id, MIDDAY_UTC - timedelta(minutes=1))
    job = db.claim_next_job(
        pg_conn,
        worker_pid=1,
        allowed_types=["review"],
        check_time=MIDDAY_UTC,
    )
    assert job is not None
    assert job["payload"]["bypass_window"] is True


def test_claim_review_non_bypass_blocked_outside_window(pg_conn):
    job_id = db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:claim-normal",
        payload={"repo": "x/y", "pr_number": 2},
    )
    _force_run_after(pg_conn, job_id, MIDDAY_UTC - timedelta(minutes=1))
    job = db.claim_next_job(
        pg_conn,
        worker_pid=1,
        allowed_types=["review"],
        check_time=MIDDAY_UTC,
    )
    assert job is None


def test_claim_non_review_types_unaffected_by_review_bypass_case(pg_conn):
    """Regression guard: the new bypass arm is on the review CASE only.
    A feedback job outside its window must still be blocked, and one
    inside its window must still be claimable — regardless of whether
    any review row carries bypass_window."""
    feedback_id = db.enqueue_job(
        pg_conn,
        job_type="feedback",
        dedup_key="test:feedback-regression",
        payload={"repo": "x/y", "pr_number": 99},
    )
    _force_run_after(pg_conn, feedback_id, MIDDAY_UTC - timedelta(minutes=1))

    # Midday: feedback window is closed (07:00–19:01).
    job = db.claim_next_job(
        pg_conn,
        worker_pid=1,
        allowed_types=["feedback"],
        check_time=MIDDAY_UTC,
    )
    assert job is None

    # Late evening (20:00 UTC): feedback window open → claimable.
    night = datetime(2026, 4, 11, 20, 0, 0, tzinfo=UTC)
    _force_run_after(pg_conn, feedback_id, night - timedelta(minutes=1))
    job = db.claim_next_job(
        pg_conn,
        worker_pid=1,
        allowed_types=["feedback"],
        check_time=night,
    )
    assert job is not None


def test_enqueue_bypass_coalesce_preserves_retry_backoff(pg_conn):
    """Regression: a pending bypass row whose run_after is in the future
    (retry backoff) must NOT have its backoff collapsed to now() by a
    later coalescing event on the same dedup_key."""
    job_id = db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:backoff-preserve",
        payload={"repo": "x/y", "pr_number": 1},
        bypass_window=True,
    )
    backoff_target = datetime.now(UTC) + timedelta(minutes=5)
    _force_run_after(pg_conn, job_id, backoff_target)

    # A later non-bypass synchronize event coalesces into the same row.
    db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:backoff-preserve",
        payload={"repo": "x/y", "pr_number": 1},
        min_run_after=datetime.now(UTC) + timedelta(hours=5),
    )
    row = _fetch(pg_conn, job_id)
    assert row["payload"]["bypass_window"] is True
    # Backoff must not be collapsed — run_after still near the target.
    delta = abs((row["run_after"] - backoff_target).total_seconds())
    assert delta < 2, f"backoff was altered: delta={delta}s"


# ── DB: pending_{ready,any}_bypass_review_exists ─────────────────────

def test_pending_bypass_ready_true_when_flag_and_run_after_now(pg_conn):
    db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:ready",
        payload={"repo": "x/y", "pr_number": 1},
        bypass_window=True,
    )
    assert db.pending_ready_bypass_review_exists(pg_conn) is True
    assert db.pending_any_bypass_review_exists(pg_conn) is True


def test_pending_bypass_ready_false_when_flag_missing(pg_conn):
    db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:no-flag",
        payload={"repo": "x/y", "pr_number": 1},
    )
    assert db.pending_ready_bypass_review_exists(pg_conn) is False
    assert db.pending_any_bypass_review_exists(pg_conn) is False


def test_pending_bypass_ready_false_when_run_after_future_but_any_true(pg_conn):
    """Bypass row in debounce/backoff: run_after in the future.
    ready_* → False (not claimable yet), any_* → True (exists)."""
    future = datetime.now(UTC) + timedelta(minutes=10)
    job_id = db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:future-bypass",
        payload={"repo": "x/y", "pr_number": 1},
        bypass_window=True,
    )
    # Force run_after into the future to simulate backoff.
    cur = pg_conn.cursor()
    cur.execute("UPDATE jobs SET run_after = %s WHERE id = %s", (future, job_id))
    pg_conn.commit()
    cur.close()
    assert db.pending_ready_bypass_review_exists(pg_conn) is False
    assert db.pending_any_bypass_review_exists(pg_conn) is True


def test_pending_bypass_ignores_processing_rows(pg_conn):
    job_id = db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:processing-bypass",
        payload={"repo": "x/y", "pr_number": 1},
        bypass_window=True,
    )
    cur = pg_conn.cursor()
    cur.execute("UPDATE jobs SET status='processing' WHERE id = %s", (job_id,))
    pg_conn.commit()
    cur.close()
    assert db.pending_ready_bypass_review_exists(pg_conn) is False
    assert db.pending_any_bypass_review_exists(pg_conn) is False


# ── Retry lifecycle: bypass flag survives retry_job ──────────────────

def test_retry_preserves_bypass_flag_and_stays_claimable_after_backoff(pg_conn):
    job_id = db.enqueue_job(
        pg_conn,
        job_type="review",
        dedup_key="test:retry-bypass",
        payload={"repo": "x/y", "pr_number": 1},
        bypass_window=True,
    )
    _force_run_after(pg_conn, job_id, MIDDAY_UTC - timedelta(minutes=1))
    claimed = db.claim_next_job(
        pg_conn, worker_pid=1, allowed_types=["review"], check_time=MIDDAY_UTC
    )
    assert claimed is not None
    new_status = db.retry_job(pg_conn, job_id, error="boom", backoff_seconds=1)
    assert new_status == "pending"

    row = _fetch(pg_conn, job_id)
    assert row["payload"]["bypass_window"] is True

    # Simulate backoff elapsing: force run_after back to just before a
    # mid-afternoon check_time. The row must be claimable again even
    # mid-afternoon because Gate 2 honors the bypass flag.
    _force_run_after(pg_conn, job_id, MIDDAY_UTC - timedelta(minutes=1))
    reclaimed = db.claim_next_job(
        pg_conn, worker_pid=1, allowed_types=["review"], check_time=MIDDAY_UTC
    )
    assert reclaimed is not None
    assert reclaimed["payload"]["bypass_window"] is True


# ── Worker nap-state classification with bypass-inclusive set ────────

def test_nap_state_debounce_when_review_only_via_bypass():
    """Only a review row is pending (bypass, not yet ready). The worker
    adds 'review' to nap_allowed, so classify_nap_state must return
    'debounce' — not 'window_blocked' — so no Slack sleep announce."""
    import worker
    state, target = worker.classify_nap_state(
        pending_by_type={"review": 1},
        allowed_types=["review"],  # nap_allowed after the bypass override
        now=MIDDAY_UTC,
    )
    assert state == "debounce"
    assert target is None


def test_nap_state_window_blocked_without_bypass_override(monkeypatch):
    """Sanity check: under claude, without the override, same midday
    scenario returns window_blocked with target=19:01 — the behavior we
    silence on bypass. (Under codex the peak-hour gate is dropped so
    `next_allowed_after('review', midday)` is None and target stays
    None; that case is irrelevant for the bypass override which only
    fires under claude.)"""
    monkeypatch.setenv("CLAUDIA_BACKEND", "claude")
    import worker
    state, target = worker.classify_nap_state(
        pending_by_type={"review": 1},
        allowed_types=[],  # review window closed, no bypass override
        now=MIDDAY_UTC,
    )
    assert state == "window_blocked"
    assert target == datetime(2026, 4, 11, 19, 1, 0, tzinfo=UTC)


# ── Gate 1: bypass jobs skip the working-hours clamp ────────────────

def test_gate1_bypass_job_returns_none_clamp():
    """The webhook's Gate 1 clamp must return None for bypass jobs so
    enqueue_job receives min_run_after=None and never applies the
    working-hours forward-clamp."""
    job = {"job_type": "review", "bypass_window": True}
    assert wr._gate1_min_run_after(job, MIDDAY_UTC) is None


def test_gate1_non_bypass_review_clamps_to_next_window(monkeypatch):
    """Under claude, a non-bypass review outside its window is clamped
    by the webhook's Gate 1 to the next 19:01 UTC so it cannot run
    mid-afternoon."""
    monkeypatch.setenv("CLAUDIA_BACKEND", "claude")
    job = {"job_type": "review"}
    clamped = wr._gate1_min_run_after(job, MIDDAY_UTC)
    assert clamped == datetime(2026, 4, 11, 19, 1, 0, tzinfo=UTC)


def test_gate1_non_bypass_review_no_clamp_under_codex(monkeypatch):
    """Under codex, review is 24/7 — Gate 1 must not clamp."""
    monkeypatch.setenv("CLAUDIA_BACKEND", "codex")
    job = {"job_type": "review"}
    assert wr._gate1_min_run_after(job, MIDDAY_UTC) is None


# ── _classify_event dispatch: edited comments are ignored ────────────

def test_classify_event_ignores_edited_pr_review_comment():
    # action != 'created' → dispatch never reaches the classifier.
    assert wr._classify_event("pull_request_review_comment", "edited", {}) is None


def test_classify_event_ignores_edited_issue_comment():
    assert wr._classify_event("issue_comment", "edited", {}) is None


# ── Sanitized plain-mention fallback (fix 2 regression) ──────────────

def test_plain_mention_in_blockquote_does_not_create_review_job():
    """`> @bot review` from a trusted user must produce no review job:
    - the bypass path rejects it (blockquote stripped → no command)
    - the plain-mention fallback also rejects it (blockquote stripped
      → no '@bot' in sanitized body)"""
    payload = _issue_comment_payload(f"> @{_BOT} review")
    with patch.object(wr, "GITHUB_USER", _BOT), \
         patch.object(wr, "_check_trusted_commenter", return_value=True):
        job = wr._classify_issue_comment(payload, _REPO)
    assert job is None


def test_plain_mention_in_fenced_code_does_not_create_review_job():
    payload = _issue_comment_payload(f"```\n@{_BOT} review\n```")
    with patch.object(wr, "GITHUB_USER", _BOT), \
         patch.object(wr, "_check_trusted_commenter", return_value=True):
        job = wr._classify_issue_comment(payload, _REPO)
    assert job is None


# ── Execution-time gating bypass (trusted user → always review) ──────

def test_validate_review_bypass_skips_all_checks():
    """A bypass review job must skip draft/label/state checks entirely.
    Trusted-user on-demand reviews always run, no questions asked."""
    import worker
    payload = {"pr_number": 42, "bypass_window": True}
    # If any check ran, subprocess.run would be invoked. Patch it to blow up
    # if called so the test fails loudly on any leak.
    with patch.object(worker.subprocess, "run",
                      side_effect=AssertionError("no gh calls for bypass review")):
        assert worker._validate_review(payload, "bot", "owner/repo") is None


def test_validate_review_non_bypass_still_checks_draft():
    import worker
    from unittest.mock import MagicMock
    payload = {"pr_number": 42}  # no bypass flag
    fake = MagicMock()
    fake.stdout = '{"state":"OPEN","labels":[],"isDraft":true}'
    with patch.object(worker.subprocess, "run", return_value=fake), \
         patch.object(worker, "REPO_CONTEXTS", {"owner/repo": {}}):
        assert worker._validate_review(payload, "bot", "owner/repo") == "pr_is_draft"


def test_build_agent_prompt_blanks_review_label_for_bypass(tmp_path, monkeypatch):
    """The pr-reviewer agent aborts on 'label removed during review' if
    {{REVIEW_LABEL}} is non-empty. A bypass review must blank it so the
    agent cannot gate itself on the label."""
    import worker
    job = {
        "type": "review",
        "payload": {"pr_number": 7, "bypass_window": True, "reasons": ["on_demand_command"]},
    }
    monkeypatch.setitem(worker.REPO_CONTEXTS, "owner/repo",
                        {"review_label": "ready for review", "default_branch": "main"})
    # Force claude backend so pick() uses model/max_turns (present in agent files).
    # Codex fields (codex_model/codex_effort) are added in Task 16.
    monkeypatch.setattr(worker.BACKEND, "name", "claude")
    prompt, _, _ = worker.build_agent_prompt(
        job, "bot", "[]", str(tmp_path), str(tmp_path), "owner/repo",
        extra_replacements={"{{PREVIOUS_REVIEW_STATE}}": "none"},
    )
    assert "{{REVIEW_LABEL}}" not in prompt
    # The agent template renders `--remove-label "{{REVIEW_LABEL}}"`;
    # with REVIEW_LABEL blanked, it becomes `--remove-label ""`.
    assert '--remove-label ""' in prompt
    assert '--remove-label "ready for review"' not in prompt


def test_setup_for_job_does_not_skip_bypass_when_already_reviewed(monkeypatch, tmp_path):
    import worker
    monkeypatch.setattr(worker, "_verify_github_identity", lambda _u: None)
    monkeypatch.setattr(worker, "compute_previous_review_state",
                        lambda *a, **kw: "ALREADY_REVIEWED")
    # Stub subprocess so the checkout/fetch calls don't actually run.
    from unittest.mock import MagicMock
    monkeypatch.setattr(worker.subprocess, "run", MagicMock())
    payload = {"pr_number": 5, "latest_head_sha": "abc", "base_ref": "main",
               "bypass_window": True}
    extra = worker.setup_for_job("review", payload, str(tmp_path),
                                 "owner/repo", "bot", "main")
    assert "skip" not in extra


def test_setup_for_job_still_skips_scheduled_already_reviewed(monkeypatch, tmp_path):
    import worker
    monkeypatch.setattr(worker, "_verify_github_identity", lambda _u: None)
    monkeypatch.setattr(worker, "compute_previous_review_state",
                        lambda *a, **kw: "ALREADY_REVIEWED")
    from unittest.mock import MagicMock
    monkeypatch.setattr(worker.subprocess, "run", MagicMock())
    payload = {"pr_number": 5, "latest_head_sha": "abc", "base_ref": "main"}
    extra = worker.setup_for_job("review", payload, str(tmp_path),
                                 "owner/repo", "bot", "main")
    assert extra.get("skip") is True


def test_build_agent_prompt_keeps_review_label_for_scheduled_review(tmp_path, monkeypatch):
    import worker
    job = {
        "type": "review",
        "payload": {"pr_number": 7, "reasons": ["pr_labeled"]},  # no bypass flag
    }
    monkeypatch.setitem(worker.REPO_CONTEXTS, "owner/repo",
                        {"review_label": "ready for review", "default_branch": "main"})
    # Force claude backend so pick() uses model/max_turns (present in agent files).
    # Codex fields (codex_model/codex_effort) are added in Task 16.
    monkeypatch.setattr(worker.BACKEND, "name", "claude")
    prompt, _, _ = worker.build_agent_prompt(
        job, "bot", "[]", str(tmp_path), str(tmp_path), "owner/repo",
        extra_replacements={"{{PREVIOUS_REVIEW_STATE}}": "none"},
    )
    assert "ready for review" in prompt
