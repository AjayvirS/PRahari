#!/usr/bin/env python3
"""FastAPI webhook receiver for GitHub events.

Validates HMAC signatures, deduplicates deliveries, classifies events,
and enqueues jobs into the PostgreSQL job queue.

Run: uvicorn webhook_receiver:app --host 0.0.0.0 --port 8080
"""

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response

import urllib.request

import db
import windows

# ── Configuration ─────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv(SCRIPT_DIR / ".env")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "#claudia")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Load allowed repos from repos.json
from utils import load_repos_config as _load_repos_config
try:
    _repos_config = _load_repos_config(SCRIPT_DIR / "repos.json")
    ALLOWED_REPOS: set[str] = set(_repos_config.keys())
except Exception as _exc:
    import sys as _sys
    print(f"FATAL: Cannot load repos.json: {_exc}", file=_sys.stderr)
    _sys.exit(1)
def _detect_github_user() -> str:
    """Get GitHub username from gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "api", "/user", "--jq", ".login"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""

GITHUB_USER = os.environ.get("GITHUB_USER", "") or _detect_github_user()

def _detect_github_token() -> str:
    """Get GitHub token from gh CLI auth."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "") or _detect_github_token()

MAX_BODY_SIZE = 1_048_576  # 1 MB


def _slack_send(text: str) -> None:
    """Fire-and-forget Slack message (best effort, never raises)."""
    if not SLACK_BOT_TOKEN:
        return
    try:
        data = json.dumps({"channel": SLACK_CHANNEL, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=data,
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def _gh_link(repo: str, pr: int | str | None = None, issue: int | str | None = None) -> str:
    """Return a Slack mrkdwn link for a PR or issue, e.g. '<url|Artemis PR #123>'."""
    short = repo.split("/")[-1] if "/" in repo else repo
    if pr:
        return f"<https://github.com/{repo}/pull/{pr}|{short} PR #{pr}>"
    if issue:
        return f"<https://github.com/{repo}/issues/{issue}|{short} issue #{issue}>"
    return ""


def _describe_job(job: dict, webhook_payload: dict) -> str:
    """Build a readable Slack message for a newly enqueued job."""
    job_type = job["job_type"]
    payload_data = job.get("payload", {})
    repo = payload_data.get("repo", "")
    pr_number = payload_data.get("pr_number")
    issue_number = payload_data.get("issue_number")
    reasons = payload_data.get("reasons", [])
    title = payload_data.get("title", "")

    # Extract who triggered this from the webhook payload
    sender = webhook_payload.get("sender", {}).get("login", "")
    if not title and pr_number:
        pr = webhook_payload.get("pull_request", {}) or webhook_payload.get("issue", {})
        title = pr.get("title", "")
    if not title and issue_number:
        title = webhook_payload.get("issue", {}).get("title", "")

    # Build linked references — include title inline for readability
    target = _gh_link(repo, pr=pr_number) if pr_number else _gh_link(repo, issue=issue_number) if issue_number else ""
    target_with_title = f"{target} — {title}" if title else target

    if job_type == "feedback":
        if "merge_conflict" in reasons:
            return f"⚠️ {target_with_title} has a merge conflict, adding to my queue"
        if "review_submitted" in reasons and sender:
            return f"💬 *{sender}* left a review on {target_with_title}, queued up"
        elif "review_comment" in reasons and sender:
            return f"💬 *{sender}* commented on my code in {target_with_title}, queued up"
        elif "issue_comment" in reasons and sender:
            return f"💬 *{sender}* left a comment on {target_with_title}, queued up"
        return f"💬 New feedback on {target_with_title}, queued up"

    elif job_type == "ci_check":
        return f"🔴 CI failed on {target_with_title}, added to my queue"

    elif job_type == "review":
        if "on_demand_command" in reasons and sender:
            return f"⚡ *{sender}* asked me to review {target_with_title} — queued for immediate review"
        if "review_requested" in reasons and sender:
            return f"📋 *{sender}* asked me to review {target_with_title}, on it soon"
        elif "thread_reply" in reasons and sender:
            return f"🔁 *{sender}* replied to my review thread on {target_with_title}, queued up"
        elif "mention_comment" in reasons and sender:
            return f"👋 *{sender}* mentioned me on {target_with_title}, queued up"
        elif "new_commits" in reasons:
            return f"🔄 New commits on {target_with_title}, re-review queued"
        elif "first_review" in reasons or "pr_opened" in reasons:
            who = f" by *{sender}*" if sender else ""
            return f"📋 {target_with_title}{who} is ready for review, queued up"
        return f"📋 {target_with_title} added to my review queue"

    elif job_type == "implement":
        return f"🛠️ Got assigned to {target_with_title}, added to my queue"

    return f"📩 New {job_type} job queued"


def _github_get(path: str) -> dict | None:
    """Lightweight GitHub API GET (best effort, returns None on failure)."""
    if not GITHUB_TOKEN:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.github.com{path}",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


# ── On-demand mention command ─────────────────────────────────────────────
# Maintainers can post `@<bot> review` in a PR comment to trigger an
# immediate review that bypasses the normal working-hours window.

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_BLOCKQUOTE_LINE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)


def _strip_markdown_for_mentions(body: str) -> str:
    """Remove fenced code, inline code spans, and blockquoted lines.

    These regions are ignored when scanning for a mention command so that
    docs, examples, and quoted-reply text do not trigger an off-hours
    review.
    """
    if not body:
        return ""
    out = _FENCED_CODE_RE.sub("", body)
    out = _INLINE_CODE_RE.sub("", out)
    out = _BLOCKQUOTE_LINE_RE.sub("", out)
    return out


@lru_cache(maxsize=4)
def _review_command_pattern(github_user: str) -> re.Pattern:
    """Compiled regex for `@<user> review` with word boundary.

    - Case-insensitive (GitHub logins are case-insensitive).
    - `\\b` after `review` so `reviewed`/`reviewing` do not match; trailing
      punctuation (`. , : ; ! ?`) is allowed.
    - Requires start-of-line or whitespace before `@name` so embedded
      usernames (e.g. `foo@user`) do not match.
    """
    return re.compile(
        rf"(?im)(?:^|(?<=\s))@{re.escape(github_user)}\s+review\b",
    )


def _is_review_command(body: str, github_user: str) -> bool:
    """True if `body` contains `@<github_user> review` after sanitization."""
    if not body or not github_user:
        return False
    sanitized = _strip_markdown_for_mentions(body)
    return _review_command_pattern(github_user).search(sanitized) is not None


def _check_trusted_commenter(repo: str, comment_user: dict) -> bool | None:
    """Tri-state trust check for a comment author.

    Returns:
        True  — human maintainer/admin on `repo` (grant bypass).
        False — definitely not trusted (human user, lookup succeeded,
                login not in the trusted set, or a bot account).
        None  — unknown: lookup failed / API error. The caller must
                treat this as "don't reject, fall through" so a
                transient GitHub API failure can never accidentally
                reject a real maintainer.
    """
    login = (comment_user or {}).get("login", "")
    if not login:
        return False
    # Bots are never trusted, but we also never decline them — an
    # upstream GitHub App with unknown relationship should not get a
    # funny rejection comment. Return False and let the caller decide
    # (the caller suppresses the decline for bots explicitly).
    if (comment_user or {}).get("type", "User") != "User":
        return False
    if login.endswith("[bot]"):
        return False
    try:
        from utils import get_trusted_users
        return login in set(get_trusted_users(repo))
    except Exception:
        log.warning("_check_trusted_commenter lookup failed", exc_info=True)
        return None


def _is_trusted_commenter(repo: str, comment_user: dict) -> bool:
    """Back-compat wrapper: True only if definitely trusted.

    Used by tests that assert a boolean contract. Production code
    paths should call `_check_trusted_commenter` and branch on the
    tri-state so transient failures fall through safely.
    """
    return _check_trusted_commenter(repo, comment_user) is True


def _post_command_decline(repo: str, pr_number: int, assigner_login: str) -> None:
    """Post a funny rejection comment on an untrusted `@bot review` command.

    Mirrors the unauthorized-issue-assignment decline path in
    utils.validate_issue_assignments. Best-effort: never raises.
    Works for both PRs and issues because GitHub numbers them in the
    same space and `gh issue comment <N>` accepts PR numbers.
    """
    try:
        import random
        from utils import FUNNY_REJECTIONS, SUBPROCESS_TIMEOUT
        message = random.choice(FUNNY_REJECTIONS).format(user=assigner_login)
        subprocess.run(
            ["gh", "issue", "comment", str(pr_number), "--repo", repo,
             "--body", message],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        log.info("Posted decline on %s#%s (untrusted: %s)",
                 repo, pr_number, assigner_login)
    except Exception:
        log.warning("Failed to post command decline", exc_info=True)


def _post_command_ack(repo: str, pr_number: int, requester_login: str) -> None:
    """Post a friendly acknowledgment when a trusted maintainer triggers
    an on-demand review. Mirrors issue-implementer's Phase 2.5 ack
    style but fires synchronously from the webhook so the requester
    gets immediate feedback. Best-effort: never raises.
    """
    try:
        import random
        from utils import FRIENDLY_REVIEW_ACKS, SUBPROCESS_TIMEOUT
        message = random.choice(FRIENDLY_REVIEW_ACKS).format(user=requester_login)
        subprocess.run(
            ["gh", "issue", "comment", str(pr_number), "--repo", repo,
             "--body", message],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        log.info("Posted command ack on %s#%s (requester: %s)",
                 repo, pr_number, requester_login)
    except Exception:
        log.warning("Failed to post command ack", exc_info=True)


IGNORED_BOTS = {"github-actions[bot]", "github-actions", "dependabot[bot]", "renovate[bot]"}


def _is_ignored_bot(login: str) -> bool:
    """Check if a GitHub login belongs to a bot we should ignore.

    We intentionally keep review bots like coderabbitai — their feedback
    is valuable and should be handled like human reviewer comments.
    """
    return login in IGNORED_BOTS


ALLOWED_EVENTS = {
    "pull_request_review",
    "pull_request_review_comment",
    "issue_comment",
    "check_suite",
    "pull_request",
    "issues",
}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("claudia.webhook")

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Claudia Webhook Receiver", docs_url=None, redoc_url=None)
_conn = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        db.ensure_database()
        _conn = db.connect()
        db.migrate(_conn)
        return _conn
    # Verify connection is alive
    try:
        cur = _conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
    except Exception:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = db.connect()
    return _conn


@app.on_event("startup")
def startup():
    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET must be set — refusing to start without signature validation")
    log.info("Webhook receiver starting (repos=%s, user=%s)", ALLOWED_REPOS, GITHUB_USER)
    _get_conn()


@app.get("/health")
def health():
    return {"status": "ok", "repos": sorted(ALLOWED_REPOS)}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
    x_github_delivery: str | None = Header(None),
    content_length: int | None = Header(None, alias="content-length"),
):
    # ── Size check ────────────────────────────────────────────────────────
    if content_length and content_length > MAX_BODY_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")

    body = await request.body()
    if len(body) > MAX_BODY_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")

    # ── HMAC validation (WEBHOOK_SECRET is required at startup) ─────────
    if not x_hub_signature_256:
        log.warning("Missing X-Hub-Signature-256 header")
        raise HTTPException(status_code=401, detail="Missing signature")
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, x_hub_signature_256):
        log.warning("Invalid HMAC signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # ── Parse payload ─────────────────────────────────────────────────────
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # ── Repo validation ───────────────────────────────────────────────────
    repo_name = payload.get("repository", {}).get("full_name", "")
    if repo_name not in ALLOWED_REPOS:
        log.info("Ignoring event for repo %s (not in repos.json)", repo_name)
        return Response(status_code=200)

    # ── Event filter ──────────────────────────────────────────────────────
    event = x_github_event or ""
    if event not in ALLOWED_EVENTS:
        log.debug("Ignoring event type: %s", event)
        return Response(status_code=200)

    delivery_id = x_github_delivery or ""
    action = payload.get("action", "")

    log.info("Received %s.%s (delivery=%s)", event, action, delivery_id)

    # ── Single PG transaction: dedup + classify + enqueue ─────────────────
    conn = _get_conn()
    try:
        # Check for duplicate delivery (fast path: already committed by a
        # prior request). A concurrent retry of the same delivery will
        # still pass this check, but the INSERT ... RETURNING on the
        # webhook_deliveries unique index below is the authoritative
        # guard that makes us exactly-once under races.
        if delivery_id and db.check_delivery(conn, delivery_id):
            log.info("Duplicate delivery %s, skipping", delivery_id)
            return Response(status_code=200)

        # Record delivery — the returned flag tells us whether *this*
        # call actually inserted the row. If another concurrent request
        # won the race, we must not run any user-visible side effects
        # (decline comments, Slack posts) or we will duplicate them.
        if delivery_id:
            inserted = db.record_delivery(conn, delivery_id, event, action)
            if not inserted:
                log.info("Delivery %s already recorded by concurrent request, skipping", delivery_id)
                conn.commit()
                return Response(status_code=200)

        # Classify and enqueue
        job = _classify_event(event, action, payload)
        if job:
            # Gate 1: clamp run_after forward if this job's window is closed.
            # Note: classify builds dicts with key "job_type", matching
            # enqueue_job's parameter name.
            min_run_after = _gate1_min_run_after(job, datetime.now(timezone.utc))
            job_id = db.enqueue_job(
                conn,
                **job,
                min_run_after=min_run_after,
            )
            log.info(
                "Enqueued %s job (dedup=%s, id=%s)",
                job["job_type"], job["dedup_key"], job_id,
            )
            if job_id:
                # New job created (not coalesced into existing pending)
                _slack_send(_describe_job(job, payload))
        else:
            conn.commit()  # commit the delivery record
            log.info("Event classified as uninteresting, no job created")

        return Response(status_code=200)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        log.exception("Error processing webhook")
        # Intentionally return 200: GitHub retries non-2xx responses, but if our
        # DB is down retries would just fail again. The worker's poll_github()
        # catch-all will discover any missed work within a few hours.
        return Response(status_code=200)


# ── Event classification ──────────────────────────────────────────────────────


def _gate1_min_run_after(job: dict, now: datetime) -> datetime | None:
    """Gate 1 clamp: forward `run_after` if `job`'s window is closed.

    On-demand bypass jobs (`bypass_window=True`) deliberately skip the
    clamp so they run as soon as the worker picks them up, regardless
    of the current time-of-day.
    """
    if job.get("bypass_window", False):
        return None
    return windows.next_allowed_after(job["job_type"], now)


def _classify_event(event: str, action: str, payload: dict) -> dict[str, Any] | None:
    """Classify a GitHub event into a job. Returns kwargs for enqueue_job or None."""
    repo = payload.get("repository", {}).get("full_name", "")

    if event == "pull_request_review" and action == "submitted":
        return _classify_pr_review(payload, repo)
    elif event == "pull_request_review_comment" and action == "created":
        return _classify_pr_review_comment(payload, repo)
    elif event == "issue_comment" and action == "created":
        return _classify_issue_comment(payload, repo)
    elif event == "check_suite":
        return _classify_check_suite(action, payload, repo)
    elif event == "pull_request":
        return _classify_pull_request(action, payload, repo)
    elif event == "issues":
        return _classify_issue(action, payload, repo)
    return None


def _classify_pr_review(payload: dict, repo: str) -> dict[str, Any] | None:
    """pull_request_review → feedback job if PR author is GITHUB_USER."""
    pr = payload.get("pull_request", {})
    pr_author = pr.get("user", {}).get("login", "")
    reviewer = payload.get("review", {}).get("user", {}).get("login", "")

    if pr_author != GITHUB_USER:
        return None
    if reviewer == GITHUB_USER or _is_ignored_bot(reviewer):
        return None  # Ignore own reviews and bots

    pr_number = pr.get("number")
    if not pr_number:
        return None

    return {
        "job_type": "feedback",
        "dedup_key": f"feedback:{repo}:PR:{pr_number}",
        "payload": {
            "repo": repo,
            "pr_number": pr_number,
            "title": pr.get("title", ""),
            "reasons": ["review_submitted"],
            "latest_head_sha": pr.get("head", {}).get("sha"),
            "base_ref": pr.get("base", {}).get("ref"),
            "head_ref": pr.get("head", {}).get("ref"),
        },
    }


def _classify_pr_review_comment(payload: dict, repo: str) -> dict[str, Any] | None:
    """pull_request_review_comment → feedback or review job.

    - Our PR + someone else comments → feedback job
    - Someone else's PR + comment @mentions us → review job
    """
    pr = payload.get("pull_request", {})
    pr_author = pr.get("user", {}).get("login", "")
    commenter = payload.get("comment", {}).get("user", {}).get("login", "")
    comment_body = payload.get("comment", {}).get("body", "")

    if commenter == GITHUB_USER or _is_ignored_bot(commenter):
        return None  # Ignore our own comments and bots

    pr_number = pr.get("number")
    if not pr_number:
        return None

    if pr_author == GITHUB_USER:
        # Someone commented on our PR → feedback
        return {
            "job_type": "feedback",
            "dedup_key": f"feedback:{repo}:PR:{pr_number}",
            "payload": {
                "repo": repo,
                "pr_number": pr_number,
                "title": pr.get("title", ""),
                "reasons": ["review_comment"],
                "latest_head_sha": pr.get("head", {}).get("sha"),
                "base_ref": pr.get("base", {}).get("ref"),
                "head_ref": pr.get("head", {}).get("ref"),
            },
        }
    elif GITHUB_USER:
        # On-demand command: `@<bot> review`.
        #   trusted  → bypass review job (skip window).
        #   untrusted + human → post funny decline, no job.
        #   untrusted + bot   → silent, fall through (no decline comment).
        #   unknown  → fall through (transient API failure — stay safe).
        if _is_review_command(comment_body, GITHUB_USER):
            commenter_user = payload.get("comment", {}).get("user", {}) or {}
            trust = _check_trusted_commenter(repo, commenter_user)
            if trust is True:
                _post_command_ack(repo, pr_number, commenter_user.get("login", ""))
                return {
                    "job_type": "review",
                    "dedup_key": f"review:{repo}:PR:{pr_number}",
                    "priority": 10,  # same class as feedback — user is waiting
                    "bypass_window": True,
                    "payload": {
                        "repo": repo,
                        "pr_number": pr_number,
                        "title": pr.get("title", ""),
                        "reasons": ["on_demand_command"],
                        "latest_head_sha": pr.get("head", {}).get("sha"),
                        "base_ref": pr.get("base", {}).get("ref"),
                        "head_ref": pr.get("head", {}).get("ref"),
                    },
                }
            if trust is False and commenter_user.get("type", "User") == "User" \
                    and not commenter_user.get("login", "").endswith("[bot]"):
                _post_command_decline(repo, pr_number, commenter_user.get("login", ""))
                return None
        # Not our PR — only react if:
        # A) Reply to a thread we opened (check parent comment author)
        # B) @mentioned us in the comment body
        # Sanitize body first so fenced-code examples, inline-code spans,
        # and quoted replies (`> @bot ...`) do not trigger a review.
        sanitized_body = _strip_markdown_for_mentions(comment_body)
        is_mention = f"@{GITHUB_USER}" in sanitized_body
        is_reply_to_us = False

        if not is_mention:
            in_reply_to = payload.get("comment", {}).get("in_reply_to_id")
            if in_reply_to:
                parent = _github_get(f"/repos/{repo}/pulls/comments/{in_reply_to}")
                if parent and parent.get("user", {}).get("login") == GITHUB_USER:
                    is_reply_to_us = True

        if is_mention or is_reply_to_us:
            reason = "mention_comment" if is_mention else "thread_reply"
            return {
                "job_type": "review",
                "dedup_key": f"review:{repo}:PR:{pr_number}",
                "payload": {
                    "repo": repo,
                    "pr_number": pr_number,
                    "title": pr.get("title", ""),
                    "reasons": [reason],
                    "latest_head_sha": pr.get("head", {}).get("sha"),
                    "base_ref": pr.get("base", {}).get("ref"),
                    "head_ref": pr.get("head", {}).get("ref"),
                },
            }

    return None


def _classify_issue_comment(payload: dict, repo: str) -> dict[str, Any] | None:
    """issue_comment → feedback or review job.

    - Our PR + someone else comments → feedback job
    - Someone else's PR + comment @mentions us → review job
    """
    issue = payload.get("issue", {})
    commenter = payload.get("comment", {}).get("user", {}).get("login", "")
    comment_body = payload.get("comment", {}).get("body", "")

    # Only care about comments on PRs (issues with pull_request field)
    if "pull_request" not in issue:
        return None

    if commenter == GITHUB_USER or _is_ignored_bot(commenter):
        return None  # Ignore our own comments and noise bots

    pr_number = issue.get("number")
    if not pr_number:
        return None

    pr_title = issue.get("title", "")
    pr_author = issue.get("user", {}).get("login", "")
    if pr_author == GITHUB_USER:
        # Someone commented on our PR → feedback
        return {
            "job_type": "feedback",
            "dedup_key": f"feedback:{repo}:PR:{pr_number}",
            "payload": {
                "repo": repo,
                "pr_number": pr_number,
                "title": pr_title,
                "reasons": ["issue_comment"],
            },
        }
    elif GITHUB_USER:
        # On-demand command: `@<bot> review`.
        #   trusted  → bypass review job.
        #   untrusted + human → decline comment, no job.
        #   untrusted + bot / unknown → fall through silently.
        if _is_review_command(comment_body, GITHUB_USER):
            commenter_user = payload.get("comment", {}).get("user", {}) or {}
            trust = _check_trusted_commenter(repo, commenter_user)
            if trust is True:
                _post_command_ack(repo, pr_number, commenter_user.get("login", ""))
                return {
                    "job_type": "review",
                    "dedup_key": f"review:{repo}:PR:{pr_number}",
                    "priority": 10,
                    "bypass_window": True,
                    "payload": {
                        "repo": repo,
                        "pr_number": pr_number,
                        "title": pr_title,
                        "reasons": ["on_demand_command"],
                    },
                }
            if trust is False and commenter_user.get("type", "User") == "User" \
                    and not commenter_user.get("login", "").endswith("[bot]"):
                _post_command_decline(repo, pr_number, commenter_user.get("login", ""))
                return None
        if f"@{GITHUB_USER}" in _strip_markdown_for_mentions(comment_body):
            # Someone @mentioned us on a PR we may have reviewed → review job.
            # validate_job will skip if we haven't actually reviewed this PR.
            # Sanitize so fenced/quoted `@bot` text does not trigger.
            return {
                "job_type": "review",
                "dedup_key": f"review:{repo}:PR:{pr_number}",
                "payload": {
                    "repo": repo,
                    "pr_number": pr_number,
                    "title": pr_title,
                    "reasons": ["mention_comment"],
                },
            }

    return None


def _classify_check_suite(action: str, payload: dict, repo: str) -> dict[str, Any] | None:
    """check_suite completed → ci_check job for failing PRs we authored."""
    if action != "completed":
        return None

    check_suite = payload.get("check_suite", {})
    conclusion = check_suite.get("conclusion", "")
    head_sha = check_suite.get("head_sha", "")
    head_branch = check_suite.get("head_branch", "")

    # Only care about failing check suites
    if conclusion in ("success", "neutral", "skipped"):
        return None

    # Check if this branch belongs to one of our PRs
    prs = check_suite.get("pull_requests", [])
    for pr in prs:
        pr_number = pr.get("number")
        pr_head_sha = pr.get("head", {}).get("sha", "")
        if pr_number and pr_head_sha == head_sha:
            # Verify we authored this PR (payload doesn't include author)
            if GITHUB_USER:
                pr_data = _github_get(f"/repos/{repo}/pulls/{pr_number}")
                if not pr_data or pr_data.get("user", {}).get("login") != GITHUB_USER:
                    continue
            pr_title = (pr_data or {}).get("title", "") if GITHUB_USER else ""
            return {
                "job_type": "ci_check",
                "dedup_key": f"ci_check:{repo}:PR:{pr_number}:SHA:{head_sha[:12]}",
                "payload": {
                    "repo": repo,
                    "pr_number": pr_number,
                    "title": pr_title,
                    "reasons": [f"check_suite_{conclusion}"],
                    "latest_head_sha": head_sha,
                    "head_ref": head_branch,
                    "conclusion": conclusion,
                },
            }

    return None


def _classify_pull_request(action: str, payload: dict, repo: str) -> dict[str, Any] | None:
    """pull_request events → review or feedback jobs."""
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number")
    pr_author = pr.get("user", {}).get("login", "")
    pr_title = pr.get("title", "")

    if not pr_number:
        return None

    # Skip draft PRs — never review or re-review drafts
    if pr.get("draft", False):
        return None

    # ── Explicitly requested to review — higher priority ──
    if action == "review_requested" and pr_author != GITHUB_USER:
        requested = payload.get("requested_reviewer", {}).get("login", "")
        if requested == GITHUB_USER:
            return {
                "job_type": "review",
                "dedup_key": f"review:{repo}:PR:{pr_number}",
                "priority": 25,  # Higher than default review (30)
                "payload": {
                    "repo": repo,
                    "pr_number": pr_number,
                    "title": pr_title,
                    "reasons": ["review_requested"],
                    "latest_head_sha": pr.get("head", {}).get("sha"),
                    "base_ref": pr.get("base", {}).get("ref"),
                    "head_ref": pr.get("head", {}).get("ref"),
                },
            }

    # ── Review request: PR opened/labeled, not our PR ──
    review_label = _repos_config.get(repo, {}).get("review_label")
    if action in ("opened", "labeled") and pr_author != GITHUB_USER:
        if review_label:
            # Repo requires a specific label to trigger reviews
            if action == "labeled":
                added_label = payload.get("label", {}).get("name", "")
                if added_label != review_label:
                    return None
            else:
                # For "opened", check existing labels on the PR
                labels = [l.get("name", "") for l in pr.get("labels", [])]
                if review_label not in labels:
                    return None
        # No review_label configured → all opened PRs trigger review (drafts already filtered above)

        return {
            "job_type": "review",
            "dedup_key": f"review:{repo}:PR:{pr_number}",
            "payload": {
                "repo": repo,
                "pr_number": pr_number,
                "title": pr_title,
                "reasons": [f"pr_{action}"],
                "latest_head_sha": pr.get("head", {}).get("sha"),
                "base_ref": pr.get("base", {}).get("ref"),
                "head_ref": pr.get("head", {}).get("ref"),
            },
        }

    # ── Re-review: PR synchronized (new commits) and we have an existing review ──
    if action == "synchronize" and pr_author != GITHUB_USER:
        return {
            "job_type": "review",
            "dedup_key": f"review:{repo}:PR:{pr_number}",
            "payload": {
                "repo": repo,
                "pr_number": pr_number,
                "title": pr_title,
                "reasons": ["new_commits"],
                "latest_head_sha": pr.get("head", {}).get("sha"),
                "base_ref": pr.get("base", {}).get("ref"),
                "head_ref": pr.get("head", {}).get("ref"),
            },
        }

    # ── Feedback: merge conflict detected on our PR ──
    if action == "synchronize" and pr_author == GITHUB_USER:
        mergeable = pr.get("mergeable")
        if mergeable is False:  # explicitly False, not None
            return {
                "job_type": "feedback",
                "dedup_key": f"feedback:{repo}:PR:{pr_number}",
                "payload": {
                    "repo": repo,
                    "pr_number": pr_number,
                    "title": pr_title,
                    "reasons": ["merge_conflict"],
                    "latest_head_sha": pr.get("head", {}).get("sha"),
                    "base_ref": pr.get("base", {}).get("ref"),
                    "head_ref": pr.get("head", {}).get("ref"),
                },
            }

    return None


def _classify_issue(action: str, payload: dict, repo: str) -> dict[str, Any] | None:
    """issues.assigned → implement job if assignee is GITHUB_USER."""
    if action != "assigned":
        return None

    issue = payload.get("issue", {})
    assignee = payload.get("assignee", {}).get("login", "")

    if assignee != GITHUB_USER:
        return None

    issue_number = issue.get("number")
    if not issue_number:
        return None

    return {
        "job_type": "implement",
        "dedup_key": f"implement:{repo}:ISSUE:{issue_number}",
        "payload": {
            "repo": repo,
            "issue_number": issue_number,
            "title": issue.get("title", ""),
            "reasons": ["assigned"],
        },
        "debounce_seconds": 10,  # Short debounce for issue assignments
    }
