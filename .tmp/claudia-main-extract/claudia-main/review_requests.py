"""Slack review-request notifications: pure helpers.

Non-pure orchestration (`_maybe_announce_review`, `_maybe_fire_digest`)
is added in later tasks alongside these helpers. This layer is pure:
no DB, no subprocess, no network.
"""
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import db
import windows
from inline_agents import run_inline_agent
from slack_api import slack_post
from utils import slack_alert

SCRIPT_DIR = Path(__file__).resolve().parent

# ── Delta classifier ────────────────────────────────────────────────────

def delta_triggers_announce(delta: dict) -> bool:
    """True iff this state delta should trigger a per-PR announcement."""
    if not isinstance(delta, dict):
        return False
    t = delta.get("type")
    if t == "implement":
        # Per spec §2.1: only the terminal "implemented" delta triggers an
        # announcement. Earlier implement deltas may carry a pr_number for
        # resumability but do not mean the PR is ready for review.
        return (
            delta.get("status") == "implemented"
            and bool(delta.get("pr_number"))
        )
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
    expected_host_path = pr_url.split("://", 1)[-1]
    for hit in _PR_LINK_RE.findall(msg):
        if hit != expected_host_path:
            return Validation("hard_reject", f"unexpected_link:{hit}")
    if _has_mention(msg):
        return Validation("hard_reject", "mention_present")

    prose = msg.replace(expected_link, "").strip()
    if len(prose) > 280:
        return Validation("warn", "over_length")
    sentences = re.findall(r"[.!?](?:\s|$)", prose)
    if len(sentences) > 2:
        return Validation("warn", "over_sentences")
    return Validation("ok")


def validate_digest_message(
    msg: str, *, pr_list: list[dict], failed_repos: list[str]
) -> Validation:
    expected_links = [
        (pr, f"<{pr['url']}|PR #{pr['pr_number']} — {pr['sanitized_title']}>")
        for pr in pr_list
    ]
    for pr, expected in expected_links:
        if expected not in msg:
            return Validation("hard_reject", f"missing:{pr['pr_number']}")
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

    # Warn-only per-bullet checks: slice message between consecutive expected
    # links (in document order) and enforce length / sentence caps.
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


# ── Orchestration ──────────────────────────────────────────────────────

log = logging.getLogger("claudia.review_requests")


def should_fire_digest(prev_in_own: bool | None, is_now_in_own: bool) -> bool:
    """True iff we just crossed the own-window's closing edge.

    `prev_in_own=None` means cold start — we never fire retroactively.
    """
    return prev_in_own is True and is_now_in_own is False


# Module-level state populated by worker.main().
WORKER_STATE = {
    "claudia_bot_user_id": None,  # may stay None
}


def _bot_user_literal() -> str:
    v = WORKER_STATE.get("claudia_bot_user_id")
    return v if v else "null"


def _review_channel() -> str:
    return os.environ.get("SLACK_REVIEW_CHANNEL", "C012NFRM76F")


def _rollback_quiet(conn) -> None:
    """Best-effort rollback so a failed DB op can't leave psycopg2 conn
    stuck in `InFailedSqlTransaction`."""
    try:
        conn.rollback()
    except Exception:
        pass


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

        try:
            claim_token = db.claim_pr_review_slot(
                conn, repo, pr_number, session_day
            )
        except Exception as exc:
            _rollback_quiet(conn)
            log.exception("claim_pr_review_slot raised: %s", exc)
            slack_alert(
                f":rotating_light: claim_pr_review_slot raised "
                f"({type(exc).__name__}) for {repo} #{pr_number} — "
                f"one-shot announce opportunity lost"
            )
            return
        if claim_token is None:
            log.debug("Review slot already claimed: %s #%d %s", repo, pr_number, session_day)
            return

        resolved = _resolve_pr_url_and_title(delta, repo, pr_number)
        if resolved is None:
            try:
                db.release_pr_review_slot(
                    conn, repo, pr_number, session_day, claim_token
                )
            except Exception as exc:
                _rollback_quiet(conn)
                log.exception(
                    "release_pr_review_slot raised (resolution path): %s", exc
                )
                slack_alert(
                    f":rotating_light: release_pr_review_slot raised "
                    f"({type(exc).__name__}) for {repo} #{pr_number} "
                    f"during resolution-failure path — row stuck in `posting`"
                )
                return
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

        try:
            slack_result = slack_post(message, _review_channel())
        except Exception as exc:
            # slack_post is contractually exception-free, but we defend.
            log.exception("slack_post raised unexpectedly: %s", exc)
            slack_result = {
                "result": "ambiguous_failure",
                "error": f"exception:{type(exc).__name__}",
            }

        if slack_result["result"] == "ok":
            try:
                ok = db.finalize_pr_review_slot(
                    conn, repo, pr_number, session_day, claim_token,
                    slack_ts=slack_result["ts"],
                )
            except Exception as exc:
                _rollback_quiet(conn)
                log.exception("finalize_pr_review_slot raised: %s", exc)
                slack_alert(
                    f":rotating_light: finalize_pr_review_slot raised "
                    f"({type(exc).__name__}) for {repo} #{pr_number} — "
                    f"row stays `posting` (message already posted, ts={slack_result.get('ts')})"
                )
            else:
                if not ok:
                    slack_alert(
                        f":rotating_light: Review slot finalize UPDATE matched 0 rows "
                        f"for {repo} #{pr_number} — row stays `posting`"
                    )
        elif slack_result["result"] == "definite_failure":
            try:
                db.release_pr_review_slot(
                    conn, repo, pr_number, session_day, claim_token
                )
            except Exception as exc:
                _rollback_quiet(conn)
                log.exception("release_pr_review_slot raised: %s", exc)
                slack_alert(
                    f":rotating_light: release_pr_review_slot raised "
                    f"({type(exc).__name__}) for {repo} #{pr_number} — "
                    f"row stuck in `posting`"
                )
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
        _rollback_quiet(conn)
        log.exception("_maybe_announce_review crashed: %s", exc)


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
    """Fire the daily digest, if the slot is still open for this session.

    Ordering follows the spec's `claim → external work → finalize/release`
    model: we claim the digest slot FIRST, then enumerate, draft, and post.
    An exception during enumeration (or anywhere else) would otherwise
    consume the main-loop's `True → False` transition edge silently, losing
    the session's digest forever. With claim-first, the row is always in
    `posting` state on failure so the next cycle — or manual inspection —
    can observe the stuck delivery.
    """
    session_day = None
    try:
        session_day = windows.current_own_session_day(now)

        # ── Claim first ──────────────────────────────────────────────────
        try:
            claim_token = db.claim_pr_review_digest(conn, session_day)
        except Exception as exc:
            _rollback_quiet(conn)
            log.exception("claim_pr_review_digest raised: %s", exc)
            slack_alert(
                f":rotating_light: claim_pr_review_digest raised "
                f"({type(exc).__name__}) for session {session_day}"
            )
            return
        if claim_token is None:
            log.debug("Digest slot already claimed for %s", session_day)
            return

        # ── External work (enumerate) under the claim ────────────────────
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

        # ── Empty-and-complete path: finalize as posted with pr_count=0,
        #    no Slack post. We already hold the claim, so this is a
        #    single-row finalize inside the state machine.
        if not pr_list and not failed_repos:
            try:
                ok = db.finalize_pr_review_digest(
                    conn, session_day, claim_token,
                    slack_ts="", pr_count=0, partial=False,
                )
            except Exception as exc:
                _rollback_quiet(conn)
                log.exception(
                    "finalize_pr_review_digest (empty) raised: %s", exc
                )
                slack_alert(
                    f":rotating_light: Empty-digest finalize raised "
                    f"({type(exc).__name__}) for session {session_day} — "
                    f"row stays `posting`"
                )
                return
            if not ok:
                slack_alert(
                    f":rotating_light: Empty-digest finalize matched 0 rows "
                    f"for session {session_day}"
                )
            else:
                log.info(
                    "Empty-and-complete digest finalized for %s", session_day
                )
            return

        # ── Draft ────────────────────────────────────────────────────────
        import json as _json
        placeholders = {
            "SLACK_REVIEW_CHANNEL": _review_channel(),
            "CLAUDIA_BOT_USER_ID": _bot_user_literal(),
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
                v = validate_digest_message(
                    draft, pr_list=pr_list, failed_repos=failed_repos
                )
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
            # pr_list empty but failed_repos not — pure partial digest.
            message = render_digest_fallback(pr_list=[], failed_repos=failed_repos)

        if use_fallback_reason:
            slack_alert(
                f":construction: Digest fell back to template "
                f"({use_fallback_reason}) for session {session_day}"
            )

        # ── Post ─────────────────────────────────────────────────────────
        try:
            slack_result = slack_post(message, _review_channel())
        except Exception as exc:
            log.exception("slack_post raised unexpectedly: %s", exc)
            slack_result = {
                "result": "ambiguous_failure",
                "error": f"exception:{type(exc).__name__}",
            }

        # ── Finalize / release ───────────────────────────────────────────
        if slack_result["result"] == "ok":
            try:
                ok = db.finalize_pr_review_digest(
                    conn, session_day, claim_token,
                    slack_ts=slack_result["ts"],
                    pr_count=len(pr_list),
                    partial=bool(failed_repos),
                )
            except Exception as exc:
                _rollback_quiet(conn)
                log.exception("finalize_pr_review_digest raised: %s", exc)
                slack_alert(
                    f":rotating_light: finalize_pr_review_digest raised "
                    f"({type(exc).__name__}) for session {session_day} — "
                    f"row stays `posting` (message already posted, "
                    f"ts={slack_result.get('ts')})"
                )
                return
            if not ok:
                slack_alert(
                    f":rotating_light: Digest finalize matched 0 rows "
                    f"for session {session_day} — row stays `posting`"
                )
        elif slack_result["result"] == "definite_failure":
            try:
                db.release_pr_review_digest(conn, session_day, claim_token)
            except Exception as exc:
                _rollback_quiet(conn)
                log.exception("release_pr_review_digest raised: %s", exc)
                slack_alert(
                    f":rotating_light: release_pr_review_digest raised "
                    f"({type(exc).__name__}) for session {session_day} — "
                    f"row stuck in `posting`"
                )
            slack_alert(
                f":rotating_light: Digest slack_post definite_failure "
                f"({slack_result.get('error')}) for session {session_day}"
            )
        else:  # ambiguous_failure
            slack_alert(
                f":rotating_light: Digest slack_post AMBIGUOUS for "
                f"{session_day} ({slack_result.get('error')}) — "
                f"row stays `posting`, no auto-retry"
            )
    except Exception as exc:
        _rollback_quiet(conn)
        log.exception("_maybe_fire_digest crashed: %s", exc)
