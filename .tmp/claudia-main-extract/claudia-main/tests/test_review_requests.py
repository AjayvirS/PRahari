"""Unit tests for review_requests.py (pure-function layer)."""
import pytest

import review_requests as rr


# ── Delta classifier ─────────────────────────────────────────────────────

def test_classifies_implement_with_pr_number():
    assert rr.delta_triggers_announce({"type": "implement", "pr_number": 42, "status": "implemented"}) is True

def test_classifies_implement_without_pr_number_false():
    assert rr.delta_triggers_announce({"type": "implement", "status": "implemented"}) is False

def test_classifies_implement_without_implemented_status_false():
    """Spec §2.1: only the terminal status=='implemented' delta triggers."""
    assert rr.delta_triggers_announce(
        {"type": "implement", "pr_number": 42, "status": "in_progress"}
    ) is False
    assert rr.delta_triggers_announce(
        {"type": "implement", "pr_number": 42}
    ) is False

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
