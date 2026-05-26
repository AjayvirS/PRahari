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


def test_digest_empty_and_complete_finalizes_without_posting(mock_conn, two_repos):
    """Claim-first: when enumeration yields nothing AND no failures, we
    hold the claim and finalize the slot in place as `posted` with
    pr_count=0. No agent, no Slack post."""
    tok = uuid.uuid4()
    with patch("review_requests.db.claim_pr_review_digest", return_value=tok), \
         patch("review_requests.db.finalize_pr_review_digest", return_value=True) as fin, \
         patch("review_requests._gh_list_prs") as gh, \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack:
        gh.return_value = ("ok", [])
        rr._maybe_fire_digest(mock_conn, NOW, github_user="claudia-bot")
    fin.assert_called_once()
    assert fin.call_args.kwargs["pr_count"] == 0
    assert fin.call_args.kwargs["partial"] is False
    agent.assert_not_called()
    slack.assert_not_called()


def test_digest_skipped_when_slot_already_claimed(mock_conn, two_repos):
    """Claim-first: if another worker already holds the slot, return
    immediately without enumerating or posting."""
    with patch("review_requests.db.claim_pr_review_digest", return_value=None), \
         patch("review_requests._gh_list_prs") as gh, \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack:
        rr._maybe_fire_digest(mock_conn, NOW, github_user="claudia-bot")
    gh.assert_not_called()
    agent.assert_not_called()
    slack.assert_not_called()


def test_digest_excludes_drafts_and_approved(mock_conn, two_repos):
    with patch("review_requests.db.claim_pr_review_digest", return_value=uuid.uuid4()), \
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
    sent_placeholders = agent.call_args.args[1]
    import json as _json
    pr_list = _json.loads(sent_placeholders["PR_LIST_JSON"])
    assert len(pr_list) == 1
    assert pr_list[0]["pr_number"] == 42


def test_digest_partial_label_enforced_on_agent_output(mock_conn, two_repos):
    tok = uuid.uuid4()
    with patch("review_requests.db.claim_pr_review_digest", return_value=tok), \
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
         patch("review_requests._gh_list_prs") as gh, \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.finalize_pr_review_digest", return_value=True) as fin, \
         patch("review_requests.slack_alert"):
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
    assert fin.call_args.kwargs["partial"] is True
    assert fin.call_args.kwargs["pr_count"] == 200


# ── Exception-handling tests (fix-3 coverage) ───────────────────────────

def test_announce_claim_raises_alerts_and_rolls_back(mock_conn):
    """If claim_pr_review_slot raises, the one-shot announce opportunity
    is lost — we must alert a human explicitly, not just log."""
    with patch("review_requests.db.claim_pr_review_slot",
               side_effect=RuntimeError("pg down")), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.slack_alert") as alert:
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented",
             "pr_url": "https://github.com/ls1intum/Artemis/pull/42",
             "pr_title": "General: Fix x"},
            NOW,
        )
    agent.assert_not_called()
    slack.assert_not_called()
    mock_conn.rollback.assert_called()
    assert any("claim_pr_review_slot raised" in c.args[0]
               for c in alert.call_args_list)


def test_announce_release_during_resolution_failure_raises_alerts(mock_conn):
    """Resolution-failure path: release_pr_review_slot raising there must
    surface a specific alert — the generic outer except is not enough."""
    tok = uuid.uuid4()
    with _mock_claim(tok), \
         patch("review_requests._resolve_pr_url_and_title", return_value=None), \
         patch("review_requests.db.release_pr_review_slot",
               side_effect=RuntimeError("db down")), \
         patch("review_requests.slack_alert") as alert:
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented"},
            NOW,
        )
    mock_conn.rollback.assert_called()
    messages = [c.args[0] for c in alert.call_args_list]
    assert any("release_pr_review_slot raised" in m and "resolution-failure" in m
               for m in messages)



def test_announce_finalize_raises_alerts_and_rolls_back(mock_conn):
    """If finalize_pr_review_slot raises, the row is already posted to
    Slack — we must rollback the failed tx AND alert a human."""
    tok = uuid.uuid4()
    with _mock_claim(tok), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.finalize_pr_review_slot",
               side_effect=RuntimeError("boom")) as fin, \
         patch("review_requests.slack_alert") as alert:
        agent.return_value = {"result": "ok", "delta": {
            "type": "review_announce",
            "message": "Review please: <https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix x>."
        }}
        slack.return_value = {"result": "ok", "ts": "1.2"}
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented",
             "pr_url": "https://github.com/ls1intum/Artemis/pull/42",
             "pr_title": "General: Fix x"},
            NOW,
        )
    fin.assert_called_once()
    mock_conn.rollback.assert_called()
    # At least one alert must mention finalize
    assert any("finalize_pr_review_slot raised" in c.args[0]
               for c in alert.call_args_list)


def test_announce_release_raises_alerts_and_rolls_back(mock_conn):
    tok = uuid.uuid4()
    with _mock_claim(tok), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.release_pr_review_slot",
               side_effect=RuntimeError("db down")), \
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
    fin.assert_not_called()
    mock_conn.rollback.assert_called()
    # Both alerts fire: release-raised AND definite_failure summary.
    messages = [c.args[0] for c in alert.call_args_list]
    assert any("release_pr_review_slot raised" in m for m in messages)
    assert any("definite_failure" in m for m in messages)


def test_announce_slack_post_raises_is_treated_as_ambiguous(mock_conn):
    """slack_post is contractually exception-free after fix 1, but we
    still defend against unexpected exceptions — they become ambiguous."""
    tok = uuid.uuid4()
    with _mock_claim(tok), \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post",
               side_effect=RuntimeError("network stack on fire")), \
         patch("review_requests.db.finalize_pr_review_slot") as fin, \
         patch("review_requests.db.release_pr_review_slot") as rel, \
         patch("review_requests.slack_alert") as alert:
        agent.return_value = {"result": "ok", "delta": {
            "type": "review_announce",
            "message": "Review please: <https://github.com/ls1intum/Artemis/pull/42|PR #42 — General: Fix x>."
        }}
        rr._maybe_announce_review(
            mock_conn, REPO,
            {"type": "implement", "pr_number": 42, "status": "implemented",
             "pr_url": "https://github.com/ls1intum/Artemis/pull/42",
             "pr_title": "General: Fix x"},
            NOW,
        )
    # Ambiguous path: no finalize, no release, one alert.
    fin.assert_not_called()
    rel.assert_not_called()
    alert.assert_called_once()
    assert "AMBIGUOUS" in alert.call_args.args[0]


def test_digest_finalize_raises_alerts_and_rolls_back(mock_conn, two_repos):
    tok = uuid.uuid4()
    with patch("review_requests.db.claim_pr_review_digest", return_value=tok), \
         patch("review_requests._gh_list_prs") as gh, \
         patch("review_requests.run_inline_agent") as agent, \
         patch("review_requests.slack_post") as slack, \
         patch("review_requests.db.finalize_pr_review_digest",
               side_effect=RuntimeError("db vanished")), \
         patch("review_requests.slack_alert") as alert:
        gh.side_effect = [
            ("ok", [{"number": 42, "title": "keep", "url": "u1",
                     "body": "b", "isDraft": False, "reviewDecision": ""}]),
            ("ok", []),
        ]
        agent.return_value = {"result": "ok", "delta":
            {"type": "review_digest", "message":
             "Good morning!\n\n• <u1|PR #42 — keep>\n  prose\n"}}
        slack.return_value = {"result": "ok", "ts": "1.2"}
        rr._maybe_fire_digest(mock_conn, NOW, github_user="claudia-bot")
    mock_conn.rollback.assert_called()
    assert any("finalize_pr_review_digest raised" in c.args[0]
               for c in alert.call_args_list)


def test_digest_empty_finalize_raises_alerts(mock_conn, two_repos):
    """Empty-and-complete path under the claim — if finalize raises, we
    alert and the row stays `posting` (stuck, but observable)."""
    tok = uuid.uuid4()
    with patch("review_requests.db.claim_pr_review_digest", return_value=tok), \
         patch("review_requests.db.finalize_pr_review_digest",
               side_effect=RuntimeError("db vanished")), \
         patch("review_requests._gh_list_prs") as gh, \
         patch("review_requests.slack_alert") as alert:
        gh.return_value = ("ok", [])
        rr._maybe_fire_digest(mock_conn, NOW, github_user="claudia-bot")
    mock_conn.rollback.assert_called()
    assert any("Empty-digest finalize raised" in c.args[0]
               for c in alert.call_args_list)


def test_digest_claim_raises_alerts(mock_conn, two_repos):
    with patch("review_requests.db.claim_pr_review_digest",
               side_effect=RuntimeError("pg down")), \
         patch("review_requests._gh_list_prs") as gh, \
         patch("review_requests.slack_alert") as alert:
        rr._maybe_fire_digest(mock_conn, NOW, github_user="claudia-bot")
    gh.assert_not_called()  # no enumeration without a claim
    mock_conn.rollback.assert_called()
    assert any("claim_pr_review_digest raised" in c.args[0]
               for c in alert.call_args_list)
