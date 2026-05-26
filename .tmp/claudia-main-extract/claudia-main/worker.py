#!/usr/bin/env python3
"""Claudia worker — long-running process that picks jobs from the PG queue.

Replaces cron.sh + run.py. Runs continuously, claiming one job at a time
in priority order, executing Claude Code sessions, and handling all
failure modes (retry, ambiguous, dead_letter).

Usage:
    worker.py                    # Run worker loop
    worker.py requeue <job_id>   # dead_letter → pending
    worker.py drain --force      # pending → dead_letter (emergency stop)
    worker.py status             # Queue depth, processing, dead_letter counts
"""

import argparse
import fcntl
import json
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# ── Import shared modules ─────────────────────────────────────────────────────

# Reuse helpers extracted from run.py
sys.path.insert(0, str(SCRIPT_DIR))
from utils import (
    load_dotenv,
    slack_send,
    slack_alert,
    detect_github_user,
    check_gh_auth,
    get_trusted_users,
    ensure_repo,
    validate_repo_remote,
    clean_repo,
    setup_directories,
    read_state,
    compact_state_for_prompt,
    _kill_tree,
    _fmt_tokens,
    _progress_bar,
    validate_issue_assignments,
    load_repos_config,
    detect_default_branch,
    FUNNY_REJECTIONS,
    SUBPROCESS_TIMEOUT,
    KNOWLEDGE_FILES,
)
import psycopg2
import db
import windows

# ── Configuration ─────────────────────────────────────────────────────────────

load_dotenv(SCRIPT_DIR / ".env")

import backends
from backends.frontmatter import PromptBuildError

BACKEND = backends.get_backend(os.getenv("CLAUDIA_BACKEND", "codex"))

# Populated at startup from repos.json, keyed by "owner/repo"
# Each value: {"path": str, "default_branch": str, "screenshots": bool}
REPO_CONTEXTS: dict[str, dict] = {}

DEFAULT_MEMORIES_DIR = str(Path.home() / "memories")
DEFAULT_LOCK_PATH = str(Path.home() / ".claudia-worker.lock")

# Timeouts per job type (seconds)
JOB_TIMEOUTS = {
    "implement": 90 * 60,   # 90 min
    "feedback": 60 * 60,    # 60 min
    "review": 60 * 60,      # 60 min
    "ci_check": 60 * 60,    # 60 min
    "hygiene": 45 * 60,     # 45 min
    "memory": 30 * 60,      # 30 min
}

# How often to refresh trusted users (seconds)
TRUSTED_USERS_TTL = 3600  # 1 hour

# How often to enqueue periodic jobs (seconds)
PERIODIC_INTERVAL = 4 * 3600  # 4 hours

# How often to poll GitHub as catch-all (seconds)
GITHUB_POLL_INTERVAL = 6 * 60 * 60  # 6 hours

# Bots to ignore in feedback/review polling (noise bots, not review bots like coderabbitai)
IGNORED_BOTS = {"github-actions[bot]", "github-actions", "dependabot[bot]", "renovate[bot]"}

# Poll interval when no jobs are available
POLL_INTERVAL = 10  # seconds

# Backoff parameters
BASE_BACKOFF = 60  # seconds
MAX_BACKOFF = 900  # 15 min

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("claudia.worker")


# ── Agent prompt building ─────────────────────────────────────────────────────

AGENT_MAP = {
    "feedback": "pr-feedback-handler.md",
    "review": "pr-reviewer.md",
    "implement": "issue-implementer.md",
    "ci_check": "ci-check-handler.md",
    "hygiene": "pr-hygiene-checker.md",
    "memory": "memory-processor.md",
}


def build_agent_prompt(
    job: dict,
    github_user: str,
    trusted_users_json: str,
    memories_dir: str,
    claudia_dir: str,
    repo: str,
    extra_replacements: dict | None = None,
) -> tuple[str, str, str | int | None]:
    """Build prompt from preamble + agent file.

    Returns (prompt, model, effort_or_turns):
      - claude: effort_or_turns is int | None (max_turns).
      - codex:  effort_or_turns is str ("xhigh"|"medium"|"low").
    """
    job_type = job["type"]
    payload = job["payload"] if isinstance(job["payload"], dict) else json.loads(job["payload"])

    agent_file = SCRIPT_DIR / "agents" / AGENT_MAP[job_type]
    if not agent_file.is_file():
        raise FileNotFoundError(f"No agent file for job type: {job_type}")

    preamble_file = SCRIPT_DIR / "prompts" / "preamble.md"
    if not preamble_file.is_file():
        raise FileNotFoundError("prompts/preamble.md not found")

    preamble = preamble_file.read_text()
    agent_text = agent_file.read_text()
    from backends.frontmatter import parse_frontmatter, pick
    metadata, agent_body = parse_frontmatter(agent_text)
    model, effort_or_turns = pick(
        BACKEND.name, metadata,
        agent_name=agent_file.stem,
        agent_file=str(agent_file),
    )

    # Compose: preamble + agent body
    prompt = preamble + "\n\n" + agent_body

    repo_slug = repo.replace("/", "-")

    # Append per-repo overlay if it exists
    overlay_file = SCRIPT_DIR / "repos" / repo_slug / "agent-overlay.md"
    if overlay_file.is_file():
        prompt += "\n\n" + overlay_file.read_text()

    # Look up repo context for default_branch and other settings
    repo_ctx = REPO_CONTEXTS.get(repo, {})
    default_branch = repo_ctx.get("default_branch", "main")

    # Common replacements
    replacements = {
        "{{GITHUB_USER}}": github_user,
        "{{REPO}}": repo,
        "{{REPO_SLUG}}": repo_slug,
        "{{MEMORIES_DIR}}": memories_dir,
        "{{CLAUDIA_DIR}}": claudia_dir,
        "{{DEFAULT_BRANCH}}": default_branch,
        "{{REPO_PATH}}": repo_ctx.get("path", ""),
        "{{SCREENSHOTS_ENABLED}}": str(repo_ctx.get("screenshots", False)).lower(),
        "{{REVIEW_LABEL}}": (
            "" if (job_type == "review" and payload.get("bypass_window"))
            else (repo_ctx.get("review_label") or "")
        ),
    }

    # Job-type-specific replacements
    pr_number = payload.get("pr_number", "")
    issue_number = payload.get("issue_number", "")
    reasons = json.dumps(payload.get("reasons", []))
    head_sha = payload.get("latest_head_sha", "")
    base_ref = payload.get("base_ref", default_branch)
    head_ref = payload.get("head_ref", "")
    conclusion = payload.get("conclusion", "")

    replacements.update({
        "{{PR_NUMBER}}": str(pr_number),
        "{{ISSUE_NUMBER}}": str(issue_number),
        "{{REASONS}}": reasons,
        "{{HEAD_SHA}}": str(head_sha),
        "{{BASE_REF}}": str(base_ref),
        "{{HEAD_REF}}": str(head_ref),
        "{{CONCLUSION}}": str(conclusion),
    })

    # Extra job-specific replacements (e.g., BRANCH_NAME, ASSIGNER, PREVIOUS_REVIEW_STATE)
    if extra_replacements:
        replacements.update(extra_replacements)

    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)

    # Replace trusted users LAST to prevent template injection
    prompt = prompt.replace("{{TRUSTED_USERS}}", trusted_users_json)

    # Validate no unresolved {{...}} tokens remain
    unresolved = re.findall(r'\{\{[A-Z_]+\}\}', prompt)
    if unresolved:
        raise ValueError(f"Unresolved template tokens in prompt: {unresolved}")

    return prompt, model, effort_or_turns


# ── Pre-job setup ─────────────────────────────────────────────────────────────


def sanitize_instruction_files(repo_path: str, default_branch: str) -> None:
    """Delete untrusted instruction files and restore safe versions from default branch."""
    ap = Path(repo_path)

    # Delete untrusted instruction files
    for f in ["CLAUDE.md", "AGENTS.md"]:
        p = ap / f
        if p.exists():
            p.unlink()
    claude_md = ap / ".claude" / "CLAUDE.md"
    if claude_md.exists():
        claude_md.unlink()
    rules_dir = ap / ".claude" / "rules"
    if rules_dir.exists():
        shutil.rmtree(str(rules_dir))

    # Delete agents symlink/dir (prevent self-spawning)
    agents_path = ap / ".claude" / "agents"
    if agents_path.is_symlink():
        agents_path.unlink()
    elif agents_path.is_dir():
        shutil.rmtree(str(agents_path))

    # Restore safe versions from default branch
    subprocess.run(
        ["git", "checkout", f"origin/{default_branch}", "--", "CLAUDE.md", "AGENTS.md"],
        cwd=repo_path, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    # Ignore failures (files may not exist on default branch)


def compute_previous_review_state(
    repo: str,
    pr_number: int,
    head_sha: str,
    github_user: str,
) -> str:
    """Determine previous review state for a review job.

    Returns one of: NONE, DISMISSED, ALREADY_REVIEWED, APPROVED, CHANGES_REQUESTED
    """
    # Fetch our reviews on this PR
    result = subprocess.run(
        ["gh", "api", f"/repos/{repo}/pulls/{pr_number}/reviews",
         "--jq", f'[.[] | select(.user.login == "{github_user}")]'],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        return "NONE"

    try:
        reviews = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "NONE"

    if not reviews:
        return "NONE"

    latest = reviews[-1]

    # If latest review is DISMISSED → re-review needed
    if latest.get("state") == "DISMISSED":
        return "DISMISSED"

    # Find latest non-dismissed review
    non_dismissed = [r for r in reviews if r.get("state") != "DISMISSED"]
    if not non_dismissed:
        return "NONE"

    latest_nd = non_dismissed[-1]
    reviewed_sha = latest_nd.get("commit_id", "")

    # Check for new thread replies from non-bot users or coderabbitai
    has_new_replies = _check_thread_replies(repo, pr_number, github_user)

    # Thread replies from non-bot users after our last comment → treat as CHANGES_REQUESTED
    # (even without new commits, even after APPROVED)
    if has_new_replies:
        return "CHANGES_REQUESTED"

    if reviewed_sha == head_sha:
        return "ALREADY_REVIEWED"

    state = latest_nd.get("state", "")
    if state == "APPROVED":
        return "APPROVED"
    if state == "CHANGES_REQUESTED":
        return "CHANGES_REQUESTED"

    return "NONE"


def _check_thread_replies(repo: str, pr_number: int, github_user: str) -> bool:
    """Check if there are thread replies from non-bot users after our last comment."""
    owner, name = repo.split("/")
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"""query {{
  repository(owner: "{owner}", name: "{name}") {{
    pullRequest(number: {pr_number}) {{
      reviewThreads(first: 100) {{
        nodes {{
          isResolved
          comments(first: 20) {{
            nodes {{
              author {{ login }}
              updatedAt
            }}
          }}
        }}
      }}
    }}
  }}
}}"""],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        return False

    try:
        data = json.loads(result.stdout)
        threads = (data.get("data", {}).get("repository", {})
                   .get("pullRequest", {}).get("reviewThreads", {}).get("nodes", []))
    except (json.JSONDecodeError, AttributeError):
        return False

    for thread in threads:
        comments = thread.get("comments", {}).get("nodes", [])
        our_last = None
        their_last = None
        for c in comments:
            author = c.get("author", {}).get("login", "")
            updated = c.get("updatedAt", "")
            if author == github_user:
                our_last = updated
            elif author not in IGNORED_BOTS and (author == "coderabbitai" or not author.endswith("[bot]")):
                their_last = updated
        # Only flag threads where we participated and someone replied after us
        if their_last and our_last and their_last > our_last:
            return True

    return False


def compute_branch_name(repo: str, issue_number: int) -> str | None:
    """Derive branch name for an implement job from issue metadata.

    Returns None if no valid branch name can be derived (caller should skip + Slack alert).
    """
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--repo", repo,
         "--json", "title,labels"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to fetch issue #{issue_number}")

    issue_data = json.loads(result.stdout)
    title = issue_data.get("title", "")
    labels = [l.get("name", "").lower() for l in issue_data.get("labels", [])]

    # Determine type from labels
    if any("bug" in l or "bugfix" in l for l in labels):
        branch_type = "bugfix"
    elif any("feature" in l or "enhancement" in l for l in labels):
        branch_type = "feature"
    else:
        branch_type = "chore"

    # Slugify the title
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    slug = slug.strip('-')

    branch_name = f"{branch_type}/{slug}-{issue_number}"

    # Truncate to 80 chars
    if len(branch_name) > 80:
        branch_name = branch_name[:80].rstrip('-')

    # Validate with git
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch_name],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        # Simplify further
        branch_name = f"{branch_type}/issue-{issue_number}"
        result2 = subprocess.run(
            ["git", "check-ref-format", "--branch", branch_name],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result2.returncode != 0:
            return None

    return branch_name


def compute_assigner(repo: str, issue_number: int, github_user: str) -> str:
    """Find the user who assigned this issue to github_user."""
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp",
         f"/repos/{repo}/issues/{issue_number}/events?per_page=100"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        return "unknown"

    try:
        all_events = json.loads(result.stdout)
        if all_events and isinstance(all_events[0], list):
            all_events = [e for page in all_events for e in page]
    except json.JSONDecodeError:
        return "unknown"

    assigned_events = [
        e for e in all_events
        if e.get("event") == "assigned"
        and e.get("assignee", {}).get("login") == github_user
    ]
    if not assigned_events:
        return "unknown"

    latest = max(assigned_events, key=lambda e: e.get("created_at", ""))
    return latest.get("assigner", {}).get("login", "unknown")


def _verify_github_identity(github_user: str) -> None:
    """Verify gh CLI identity matches expected user. Raises on mismatch."""
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    actual = result.stdout.strip()
    if result.returncode != 0 or not actual:
        raise RuntimeError("gh identity check failed")
    if actual != github_user:
        raise RuntimeError(
            f"gh identity mismatch: expected {github_user}, got {actual}"
        )


def setup_for_job(
    job_type: str,
    payload: dict,
    repo_path: str,
    repo: str,
    github_user: str,
    default_branch: str = "main",
) -> dict:
    """Run per-job Python setup BEFORE launching Claude. Returns extra replacements dict.

    May raise RuntimeError on setup failure.
    Returns dict with 'skip' key set if the job should be skipped entirely.
    """
    # Identity check (was per-job in old wrapper prompts)
    _verify_github_identity(github_user)

    extra = {}
    ap = repo_path

    if job_type == "feedback":
        pr_number = payload.get("pr_number")
        subprocess.run(
            ["gh", "pr", "checkout", str(pr_number), "--repo", repo],
            cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            check=True,
        )
        subprocess.run(
            ["git", "pull"],
            cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )

    elif job_type == "review":
        pr_number = payload.get("pr_number")
        head_sha = payload.get("latest_head_sha", "")
        base_ref = payload.get("base_ref", default_branch)

        # Compute previous review state BEFORE checkout
        prev_state = compute_previous_review_state(repo, pr_number, head_sha, github_user)
        extra["{{PREVIOUS_REVIEW_STATE}}"] = prev_state

        if prev_state == "ALREADY_REVIEWED" and not payload.get("bypass_window"):
            extra["skip"] = True
            extra["skip_delta"] = {
                "type": "review",
                "pr_number": pr_number,
                "status": "skipped",
                "reason": "already_reviewed_this_sha",
            }
            return extra

        subprocess.run(
            ["gh", "pr", "checkout", str(pr_number), "--repo", repo],
            cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            check=True,
        )
        subprocess.run(
            ["git", "fetch", "origin", base_ref],
            cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )

    elif job_type == "implement":
        issue_number = payload.get("issue_number")
        branch_name = compute_branch_name(repo, issue_number)

        # If branch name is invalid even after simplification, skip + Slack alert
        if branch_name is None:
            slack_send(f":warning: Cannot derive valid branch name for {_gh_link(repo, issue=issue_number)}, skipping")
            return {
                "skip": True,
                "skip_delta": {
                    "type": "implement",
                    "issue_number": issue_number,
                    "status": "skipped",
                    "reason": "invalid_branch_name",
                },
            }

        assigner = compute_assigner(repo, issue_number, github_user)

        # Fail-closed: if assigner is unknown, skip the job (not retry — this
        # may be a permanent condition, e.g., self-assigned or no events)
        if assigner == "unknown":
            return {
                "skip": True,
                "skip_delta": {
                    "type": "implement",
                    "issue_number": issue_number,
                    "status": "skipped",
                    "reason": "assigner_unknown",
                },
            }

        extra["{{BRANCH_NAME}}"] = branch_name
        extra["{{ASSIGNER}}"] = assigner

        # Fetch and create/reset branch
        subprocess.run(
            ["git", "fetch", "origin", default_branch],
            cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            check=True,
        )
        # Try creating branch; if it already exists, reset it
        result = subprocess.run(
            ["git", "checkout", "-b", branch_name, f"origin/{default_branch}"],
            cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "checkout", branch_name],
                cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                check=True,
            )
            subprocess.run(
                ["git", "reset", "--hard", f"origin/{default_branch}"],
                cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                check=True,
            )

    elif job_type == "ci_check":
        pr_number = payload.get("pr_number")
        head_sha = payload.get("latest_head_sha", "")

        subprocess.run(
            ["gh", "pr", "checkout", str(pr_number), "--repo", repo],
            cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            check=True,
        )
        subprocess.run(
            ["git", "pull"],
            cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        # Verify HEAD matches expected SHA
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        actual_sha = result.stdout.strip()
        if head_sha and actual_sha != head_sha:
            log.warning("CI check: HEAD %s != expected %s, PR was updated", actual_sha, head_sha)
            extra["skip"] = True
            extra["skip_delta"] = {
                "type": "ci_check",
                "pr_number": pr_number,
                "status": "skipped",
                "reason": "sha_stale",
            }
            return extra

    elif job_type == "hygiene":
        # Hygiene agent handles per-PR checkout itself, just sanitize
        pass

    elif job_type == "memory":
        # No repo interaction needed
        pass

    return extra


# ── Hygiene batch runner ─────────────────────────────────────────────────────


def _run_hygiene_batch(
    job_id: int,
    job: dict,
    github_user: str,
    trusted_users_json: str,
    memories_dir: str,
    claudia_dir: str,
    repo: str,
) -> dict:
    """List open PRs and run hygiene agent once per PR. Returns aggregate delta."""
    repo_ctx = REPO_CONTEXTS[repo]
    ap = repo_ctx["path"]
    default_branch = repo_ctx["default_branch"]

    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--author", github_user,
         "--state", "open", "--json", "number,headRefName", "--limit", "20"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to list PRs: {result.stderr}")

    prs = json.loads(result.stdout)
    prs_checked = 0
    prs_fixed = 0
    prs_ambiguous = 0
    prs_failed = 0

    for pr in prs:
        pr_number = pr["number"]
        head_ref = pr.get("headRefName", "")
        log.info("Hygiene [%s]: processing PR #%d (branch=%s)",
                 _repo_short(repo), pr_number, head_ref)

        try:
            clean_repo(ap, default_branch)
            subprocess.run(
                ["gh", "pr", "checkout", str(pr_number), "--repo", repo],
                cwd=ap, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                check=True,
            )
            sanitize_instruction_files(ap, default_branch)

            extra = {"{{PR_NUMBER}}": str(pr_number), "{{HEAD_REF}}": head_ref}
            prompt, model, effort_or_turns = build_agent_prompt(
                job=job,
                github_user=github_user,
                trusted_users_json=trusted_users_json,
                memories_dir=memories_dir,
                claudia_dir=claudia_dir,
                repo=repo,
                extra_replacements=extra,
            )

            output_fd, output_file = tempfile.mkstemp(
                prefix=f"claudia-hygiene-{job_id}-pr{pr_number}-", suffix=".jsonl",
            )
            os.close(output_fd)
            timeout = JOB_TIMEOUTS.get("hygiene", 60 * 60)

            run_result = backends.run_with_heartbeat(
                BACKEND,
                prompt=prompt,
                cwd=ap,
                model=model,
                effort_or_turns=effort_or_turns,
                job_id=job_id,
                timeout_seconds=timeout,
                output_file=output_file,
            )

            parsed = BACKEND.parse_output(run_result.ctx, output_file)
            outcome, delta = classify_outcome(
                run_result.exit_code, parsed,
                require_delta_on_success=BACKEND.requires_delta_for_success,
            )

            # Hygiene requires a delta to mean anything. exit-0 / no-delta is
            # silent-success today; treat as ambiguous here regardless of backend.
            if outcome == "success" and delta is None:
                outcome = "ambiguous"

            if outcome == "success":
                prs_checked += 1
                if delta and delta.get("fixed"):
                    prs_fixed += 1
            elif outcome == "ambiguous":
                prs_checked += 1
                prs_ambiguous += 1
                log.warning(
                    "Hygiene [%s]: PR #%d ambiguous (unexpected_events=%s, has_delta=%s)",
                    _repo_short(repo), pr_number,
                    parsed.unexpected_events, delta is not None,
                )
            else:  # transient_failure
                prs_checked += 1
                prs_failed += 1

            try:
                os.unlink(output_file)
            except OSError:
                pass

        except PromptBuildError:
            # MUST be a sibling handler placed BEFORE except Exception.
            # Python matches handlers in source order; placing this AFTER
            # except Exception would never fire.
            raise  # propagate; outer hygiene job marks as transient_failure
        except Exception as exc:
            log.warning("Hygiene [%s]: failed on PR #%d: %s",
                        _repo_short(repo), pr_number, exc)
            prs_checked += 1
            prs_failed += 1

    branches_cleaned = _cleanup_stale_branches(repo, github_user, ap, default_branch)

    short = _repo_short(repo)
    if prs_ambiguous or prs_failed:
        slack_send(f">Hygiene [{short}]: checked {prs_checked} PRs, "
                   f"fixed {prs_fixed}, ambiguous {prs_ambiguous}, failed {prs_failed}")
    elif prs_fixed > 0:
        slack_send(f">Hygiene [{short}]: checked {prs_checked} PRs, fixed {prs_fixed}")
    else:
        slack_send(f">Hygiene [{short}]: checked {prs_checked} PRs, all good")

    return {
        "type": "hygiene",
        "status": "completed",
        "prs_checked": prs_checked,
        "prs_fixed": prs_fixed,
        "prs_ambiguous": prs_ambiguous,
        "prs_failed": prs_failed,
        "branches_cleaned": branches_cleaned,
    }


def _cleanup_stale_branches(repo: str, github_user: str, repo_path: str, default_branch: str) -> int:
    """Delete local branches for closed/merged PRs. Returns count of branches cleaned."""
    # Get list of open PR branches
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--author", github_user,
         "--state", "open", "--json", "headRefName"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    open_branches = set()
    if result.returncode == 0:
        try:
            for pr in json.loads(result.stdout):
                open_branches.add(pr.get("headRefName", ""))
        except json.JSONDecodeError:
            pass

    # List local branches
    result = subprocess.run(
        ["git", "branch", "--format", "%(refname:short)"],
        cwd=repo_path, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        return 0

    protected = {"develop", "main", "master", default_branch}
    cleaned = 0
    for branch in result.stdout.strip().split("\n"):
        branch = branch.strip()
        if not branch or branch in protected:
            continue
        if branch not in open_branches:
            del_result = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=repo_path, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            if del_result.returncode == 0:
                cleaned += 1
                log.info("Hygiene: cleaned stale branch %s", branch)

    return cleaned


# ── Outcome classification ────────────────────────────────────────────────────


def classify_outcome(
    exit_code: int,
    parsed,
    *,
    require_delta_on_success: bool = False,
):
    """Classify a backend run outcome from exit_code + ParsedRun.

    Returns (outcome, state_delta_dict_or_None) where outcome is one of:
      "success", "transient_failure", "ambiguous".

    Precedence rules:
      - exit_code == -2 (runner failure) → always transient_failure.
      - parsed.unexpected_events (and exit_code != -2) → ambiguous.
      - require_delta_on_success (codex) tightens success: any malformed
        delta OR more than one valid delta → ambiguous (output discipline
        breach, even if one of the deltas happens to be parseable).
      - exit_code == 0:
          require_delta_on_success + no delta → ambiguous (codex behavior)
          else → success.
      - nonzero + tool_use:
          delta present → success; else ambiguous.
      - nonzero + no tool_use → transient_failure.
    """
    if exit_code == -2:
        return ("transient_failure", None)
    if parsed.unexpected_events:
        return ("ambiguous", parsed.state_delta)
    if require_delta_on_success:
        # Output-discipline gate for codex: exactly one parseable delta,
        # zero malformed. Anything else means the agent didn't follow
        # "emit a SINGLE fenced state_delta block as your LAST output".
        if parsed.malformed_state_deltas:
            return ("ambiguous", parsed.state_delta)
        if len(parsed.state_deltas) > 1:
            return ("ambiguous", parsed.state_delta)
    if exit_code == 0:
        if require_delta_on_success and parsed.state_delta is None:
            return ("ambiguous", None)
        return ("success", parsed.state_delta)
    if parsed.has_tool_use:
        return ("success", parsed.state_delta) if parsed.state_delta else ("ambiguous", None)
    return ("transient_failure", None)


# ── Job validation (execution-time checks) ────────────────────────────────────


def validate_job(job: dict, github_user: str, repo: str) -> str | None:
    """Check if a job is still valid at execution time.

    Returns None if valid, or a skip reason string if the job should be skipped.
    """
    job_type = job["type"]
    payload = job["payload"] if isinstance(job["payload"], dict) else json.loads(job["payload"])

    if job_type == "feedback":
        return _validate_feedback(payload, repo)
    elif job_type == "review":
        return _validate_review(payload, github_user, repo)
    elif job_type == "implement":
        return _validate_implement(payload, github_user, repo)
    elif job_type == "ci_check":
        return _validate_ci_check(payload, repo)
    return None


def _validate_feedback(payload: dict, repo: str) -> str | None:
    pr_number = payload.get("pr_number")
    if not pr_number:
        return "missing_pr_number"
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "state", "--jq", ".state"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    state = result.stdout.strip()
    if state in ("MERGED", "CLOSED"):
        return f"pr_{state.lower()}"
    return None


def _validate_review(payload: dict, github_user: str, repo: str) -> str | None:
    pr_number = payload.get("pr_number")
    if not pr_number:
        return "missing_pr_number"
    # On-demand bypass from a trusted commenter: always run, skip all gating.
    if payload.get("bypass_window"):
        return None
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", repo,
         "--json", "state,labels,isDraft",
         "--jq", '{state: .state, labels: [.labels[].name], isDraft: .isDraft}'],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None  # Can't validate, let it proceed

    if info.get("state") in ("MERGED", "CLOSED"):
        return f"pr_{info['state'].lower()}"

    if info.get("isDraft", False):
        return "pr_is_draft"

    review_label = REPO_CONTEXTS.get(repo, {}).get("review_label")
    if review_label:
        labels = info.get("labels", [])
        if review_label not in labels:
            # Check if we have an existing review (re-reviews don't need the label)
            rev_result = subprocess.run(
                ["gh", "api", f"/repos/{repo}/pulls/{pr_number}/reviews",
                 "--jq", f'[.[] | select(.user.login == "{github_user}" and .state != "DISMISSED")] | length'],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            existing_count = int(rev_result.stdout.strip() or "0")
            if existing_count == 0:
                return "label_removed_no_prior_review"

    return None


def _validate_implement(payload: dict, github_user: str, repo: str) -> str | None:
    issue_number = payload.get("issue_number")
    if not issue_number:
        return "missing_issue_number"

    # Still assigned?
    result = subprocess.run(
        ["gh", "api", f"/repos/{repo}/issues/{issue_number}",
         "--jq", "[.assignees[].login]"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    try:
        assignees = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "cannot_verify_assignment"
    if github_user not in assignees:
        return "no_longer_assigned"

    # Verify assigner has maintain/admin permissions (fail-closed)
    events_result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp",
         f"/repos/{repo}/issues/{issue_number}/events?per_page=100"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    try:
        all_events = json.loads(events_result.stdout)
        if all_events and isinstance(all_events[0], list):
            all_events = [e for page in all_events for e in page]
        assigned_events = [
            e for e in all_events
            if e.get("event") == "assigned"
            and e.get("assignee", {}).get("login") == github_user
        ]
        if not assigned_events:
            return "assigner_unknown"
        latest = max(assigned_events, key=lambda e: e.get("created_at", ""))
        assigner = latest.get("assigner", {}).get("login")
        if not assigner:
            return "assigner_unknown"
        perm_result = subprocess.run(
            ["gh", "api", f"/repos/{repo}/collaborators/{assigner}/permission",
             "--jq", ".role_name"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        role = perm_result.stdout.strip().strip('"').lower()
        if role not in ("maintain", "admin"):
            # Side effect: unassign + comment (mirrors old wrapper behavior)
            subprocess.run(
                ["gh", "issue", "edit", str(issue_number), "--repo", repo,
                 "--remove-assignee", github_user],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            message = random.choice(FUNNY_REJECTIONS).format(user=assigner)
            subprocess.run(
                ["gh", "issue", "comment", str(issue_number), "--repo", repo,
                 "--body", message],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            return f"assigner_{assigner}_unauthorized_role_{role}"
    except (json.JSONDecodeError, KeyError):
        return "cannot_verify_assigner_permission"

    # Already have an open PR for this?
    timeline_result = subprocess.run(
        ["gh", "api", "--paginate",
         f"/repos/{repo}/issues/{issue_number}/timeline",
         "--jq", f'[.[] | select(.event == "cross-referenced") | select(.source.issue.pull_request) | select(.source.issue.user.login == "{github_user}") | select(.source.issue.state == "open")] | length'],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if timeline_result.returncode == 0 and timeline_result.stdout.strip().isdigit():
        if int(timeline_result.stdout.strip()) > 0:
            return "pr_already_exists"

    return None


def _validate_ci_check(payload: dict, repo: str) -> str | None:
    pr_number = payload.get("pr_number")
    head_sha = payload.get("latest_head_sha", "")
    if not pr_number:
        return "missing_pr_number"

    # SHA still HEAD?
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", repo,
         "--json", "headRefOid", "--jq", ".headRefOid"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    current_sha = result.stdout.strip()
    if head_sha and current_sha and current_sha != head_sha:
        return "sha_stale"

    return None


# ── Reconciliation (pre-retry check) ──────────────────────────────────────────


def reconcile_shows_complete(job: dict, github_user: str, repo: str) -> bool:
    """Check if a job's work was already done (for retry/ambiguous cases).

    Returns True if the work appears complete (skip re-execution).
    """
    job_type = job["type"]
    payload = job["payload"] if isinstance(job["payload"], dict) else json.loads(job["payload"])

    if job_type == "feedback":
        # Check if latest comments are from us (we already replied)
        pr_number = payload.get("pr_number")
        if not pr_number:
            return False
        result = subprocess.run(
            ["gh", "api", f"/repos/{repo}/pulls/{pr_number}/comments",
             "--jq", ".[].user.login"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode == 0:
            logins = result.stdout.strip().split("\n")
            # If our latest comment is more recent than others, work was likely done
            if logins and logins[-1] == github_user:
                return True

    elif job_type == "review":
        pr_number = payload.get("pr_number")
        head_sha = payload.get("latest_head_sha", "")
        if not pr_number or not head_sha:
            return False
        result = subprocess.run(
            ["gh", "api", f"/repos/{repo}/pulls/{pr_number}/reviews",
             "--jq", f'[.[] | select(.user.login == "{github_user}" and .state != "DISMISSED")] | last | .commit_id'],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        reviewed_sha = result.stdout.strip().strip('"')
        if reviewed_sha == head_sha:
            return True

    return False


# ── Periodic job scheduling ───────────────────────────────────────────────────


def enqueue_periodic_jobs(conn) -> None:
    """Enqueue hygiene and memory jobs if not already pending, for each repo."""
    for repo in REPO_CONTEXTS:
        for job_type, suffix in [("hygiene", "hygiene"), ("memory", "memory")]:
            dedup_key = f"{suffix}:{repo}:periodic"
            if not db.has_pending_job(conn, dedup_key):
                job_id = db.enqueue_job(
                    conn, job_type, dedup_key,
                    payload={"repo": repo, "reasons": ["periodic"]},
                    debounce_seconds=0,
                    min_run_after=windows.next_allowed_after(
                        job_type, datetime.now(timezone.utc)
                    ),
                )
                if job_id:
                    log.info("Enqueued periodic %s job for %s (id=%d)", job_type, repo, job_id)


# ── GitHub polling (catch-all discovery) ──────────────────────────────────────


def poll_github(conn, github_user: str) -> int:
    """Poll GitHub for any work that webhooks might have missed.

    Discovers feedback, reviews, CI failures, and issue assignments by
    querying the GitHub API directly. Uses the same dedup_key system as
    webhooks, so duplicate jobs are impossible.

    Runs on startup (to catch everything from before webhooks existed or
    during downtime) and then periodically as a safety net.

    Returns the number of jobs enqueued.
    """
    total_enqueued = 0
    for repo in REPO_CONTEXTS:
        try:
            enqueued = _poll_github_repo(conn, github_user, repo)
            total_enqueued += enqueued
        except Exception as exc:
            log.warning("Poll: failed for %s: %s", repo, exc)
    if total_enqueued > 0:
        slack_send(f"🔍 Checked GitHub, found {total_enqueued} new thing{'s' if total_enqueued != 1 else ''} to work on")
    return total_enqueued


def _poll_github_repo(conn, github_user: str, repo: str) -> int:
    """Poll a single GitHub repo for missed work. Returns jobs enqueued."""
    default_branch = REPO_CONTEXTS[repo]["default_branch"]
    enqueued = 0

    # ── 1. Own PRs: feedback (pending reviews/comments) + merge conflicts ─
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--author", github_user,
             "--state", "open", "--json",
             "number,headRefName,headRefOid,baseRefName,mergeable,title",
             "--limit", "50"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode == 0:
            own_prs = json.loads(result.stdout)
            for pr in own_prs:
                pr_number = pr.get("number")
                if not pr_number:
                    continue
                head_sha = pr.get("headRefOid", "")
                base_ref = pr.get("baseRefName", default_branch)
                head_ref = pr.get("headRefName", "")
                pr_title = pr.get("title", "")

                needs_feedback = False
                reasons = []

                # Check merge conflicts
                if pr.get("mergeable") == "CONFLICTING":
                    needs_feedback = True
                    reasons.append("merge_conflict")

                # Check for unhandled reviews/comments by comparing latest
                # external activity timestamp against our latest response timestamp.
                # "Our response" = latest of: our comments, our review comments,
                # or our latest commit push (pushing a fix IS our response to feedback).
                our_latest_result = subprocess.run(
                    ["gh", "api",
                     f"/repos/{repo}/pulls/{pr_number}/comments",
                     "--jq", f'[.[] | select(.user.login == "{github_user}")] | last | .updated_at'],
                    capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                )
                our_latest_issue_result = subprocess.run(
                    ["gh", "api",
                     f"/repos/{repo}/issues/{pr_number}/comments",
                     "--jq", f'[.[] | select(.user.login == "{github_user}")] | last | .updated_at'],
                    capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                )
                # Our latest commit on the PR branch — pushing commits IS our response
                our_latest_commit_result = subprocess.run(
                    ["gh", "api",
                     f"/repos/{repo}/pulls/{pr_number}/commits",
                     "--jq", 'last | .commit.committer.date'],
                    capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                )
                our_latest_ts = ""
                for r in (our_latest_result, our_latest_issue_result, our_latest_commit_result):
                    ts = r.stdout.strip().strip('"')
                    if ts and ts != "null" and ts > our_latest_ts:
                        our_latest_ts = ts

                # Latest external activity per channel (excluding noise bots)
                ignore_jq = " and ".join(
                    f'.user.login != "{b}"' for b in sorted(IGNORED_BOTS | {github_user})
                )
                rev_result = subprocess.run(
                    ["gh", "api", "--paginate",
                     f"/repos/{repo}/pulls/{pr_number}/reviews",
                     "--jq", f'[.[] | select({ignore_jq})] | last | .submitted_at'],
                    capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                )
                comments_result = subprocess.run(
                    ["gh", "api",
                     f"/repos/{repo}/pulls/{pr_number}/comments",
                     "--jq", f'[.[] | select({ignore_jq})] | last | .updated_at'],
                    capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                )
                issue_comments_result = subprocess.run(
                    ["gh", "api",
                     f"/repos/{repo}/issues/{pr_number}/comments",
                     "--jq", f'[.[] | select({ignore_jq})] | last | .updated_at'],
                    capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                )

                latest_review = rev_result.stdout.strip().strip('"')
                latest_comment = comments_result.stdout.strip().strip('"')
                latest_issue_comment = issue_comments_result.stdout.strip().strip('"')

                # Only enqueue if external activity is newer than our latest response
                if latest_review and latest_review != "null" and latest_review > our_latest_ts:
                    needs_feedback = True
                    reasons.append("review_submitted")
                if latest_comment and latest_comment != "null" and latest_comment > our_latest_ts:
                    needs_feedback = True
                    if "review_comment" not in reasons:
                        reasons.append("review_comment")
                if latest_issue_comment and latest_issue_comment != "null" and latest_issue_comment > our_latest_ts:
                    needs_feedback = True
                    if "issue_comment" not in reasons:
                        reasons.append("issue_comment")

                if needs_feedback:
                    job_id = db.enqueue_job(
                        conn, "feedback", f"feedback:{repo}:PR:{pr_number}",
                        payload={
                            "repo": repo,
                            "pr_number": pr_number,
                            "title": pr_title,
                            "reasons": reasons,
                            "latest_head_sha": head_sha,
                            "base_ref": base_ref,
                            "head_ref": head_ref,
                        },
                        debounce_seconds=0,
                        min_run_after=windows.next_allowed_after(
                            "feedback", datetime.now(timezone.utc)
                        ),
                    )
                    if job_id:
                        log.info("Poll [%s]: enqueued feedback for PR #%d (reasons=%s)", _repo_short(repo), pr_number, reasons)
                        enqueued += 1

                # Check CI failures on own PRs
                ci_result = subprocess.run(
                    ["gh", "pr", "checks", str(pr_number), "--repo", repo,
                     "--json", "name,state,conclusion"],
                    capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                )
                if ci_result.returncode == 0:
                    try:
                        checks = json.loads(ci_result.stdout)
                        has_failure = any(
                            c.get("conclusion") in ("failure", "timed_out", "action_required")
                            for c in checks
                        )
                        if has_failure:
                            ci_dedup = f"ci_check:{repo}:PR:{pr_number}:SHA:{head_sha[:12]}"
                            # Skip if we already handled this SHA (completed or skipped as flaky)
                            if db.has_finished_job(conn, ci_dedup):
                                continue
                            job_id = db.enqueue_job(
                                conn, "ci_check", ci_dedup,
                                payload={
                                    "repo": repo,
                                    "pr_number": pr_number,
                                    "title": pr_title,
                                    "reasons": ["poll_ci_failure"],
                                    "latest_head_sha": head_sha,
                                    "head_ref": head_ref,
                                    "conclusion": "failure",
                                },
                                debounce_seconds=0,
                                min_run_after=windows.next_allowed_after(
                                    "ci_check", datetime.now(timezone.utc)
                                ),
                            )
                            if job_id:
                                log.info("Poll [%s]: enqueued ci_check for PR #%d", _repo_short(repo), pr_number)
                                enqueued += 1
                    except json.JSONDecodeError:
                        pass
    except Exception as exc:
        log.warning("Poll: failed to scan own PRs: %s", exc)

    # ── 2. PRs needing review ────────────────────────────────────────────
    review_label = REPO_CONTEXTS[repo].get("review_label")
    pr_list_cmd = [
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,headRefName,headRefOid,baseRefName,author,title,isDraft",
        "--limit", "100",
    ]
    if review_label:
        pr_list_cmd.extend(["--label", review_label])
    try:
        result = subprocess.run(
            pr_list_cmd,
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode == 0:
            candidates = json.loads(result.stdout)
            for pr in candidates:
                if pr.get("isDraft", False):
                    continue  # Never review draft PRs
                pr_author = pr.get("author", {}).get("login", "")
                if pr_author == github_user:
                    continue  # Never review own PRs

                pr_number = pr.get("number")
                if not pr_number:
                    continue

                head_sha = pr.get("headRefOid", "")
                base_ref = pr.get("baseRefName", default_branch)
                head_ref = pr.get("headRefName", "")
                pr_title = pr.get("title", "")

                # Check if we already have a non-dismissed review
                rev_result = subprocess.run(
                    ["gh", "api", "--paginate", "--slurp",
                     f"/repos/{repo}/pulls/{pr_number}/reviews"],
                    capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                )
                last_reviewed_sha = ""
                last_review_time = ""
                if rev_result.returncode == 0:
                    try:
                        all_reviews = []
                        for page in json.loads(rev_result.stdout):
                            if isinstance(page, list):
                                all_reviews.extend(page)
                            else:
                                all_reviews.append(page)
                        our_reviews = [
                            r for r in all_reviews
                            if r.get("user", {}).get("login") == github_user
                        ]
                        if our_reviews:
                            last_reviewed_sha = our_reviews[-1].get("commit_id", "")
                            last_review_time = our_reviews[-1].get("submitted_at", "")
                    except (json.JSONDecodeError, TypeError):
                        pass

                if last_reviewed_sha == head_sha:
                    # Reviewed current HEAD — check for @mentions, otherwise skip
                    mention_result = subprocess.run(
                        ["gh", "api",
                         f"/repos/{repo}/issues/{pr_number}/comments",
                         "--jq", f'[.[] | select(.user.login != "{github_user}" and (.body | test("@{github_user}")))] | last | .updated_at'],
                        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                    )
                    latest_mention = mention_result.stdout.strip().strip('"')
                    if latest_mention and latest_mention != "null":
                        our_reply_result = subprocess.run(
                            ["gh", "api",
                             f"/repos/{repo}/issues/{pr_number}/comments",
                             "--jq", f'[.[] | select(.user.login == "{github_user}")] | last | .updated_at'],
                            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                        )
                        our_reply_ts = our_reply_result.stdout.strip().strip('"')
                        if not our_reply_ts or our_reply_ts == "null" or latest_mention > our_reply_ts:
                            reasons = ["mention_comment"]
                        else:
                            continue
                    else:
                        continue
                elif last_reviewed_sha:
                    # Check if the PR author pushed real work since our review,
                    # or if HEAD only moved due to a base branch merge.
                    # Use PR commits endpoint (only PR-branch commits, not all of develop).
                    has_new_work = False
                    pr_commits_result = subprocess.run(
                        ["gh", "api", "--paginate", "--slurp",
                         f"/repos/{repo}/pulls/{pr_number}/commits"],
                        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
                    )
                    if pr_commits_result.returncode == 0 and last_review_time:
                        try:
                            pr_commits = []
                            for page in json.loads(pr_commits_result.stdout):
                                if isinstance(page, list):
                                    pr_commits.extend(page)
                                else:
                                    pr_commits.append(page)
                            for c in pr_commits:
                                # Skip merge commits (2+ parents) — these are base branch merges
                                parents = c.get("parents", [])
                                if len(parents) >= 2:
                                    continue
                                # Check if this non-merge commit was pushed after our review
                                commit_date = c.get("commit", {}).get("committer", {}).get("date", "")
                                if commit_date and commit_date > last_review_time:
                                    has_new_work = True
                                    break
                        except (json.JSONDecodeError, TypeError):
                            has_new_work = True  # Err on the side of reviewing
                    else:
                        has_new_work = True  # Can't determine, review to be safe

                    if not has_new_work:
                        continue  # Only base branch merges, diff unchanged
                    reasons = ["new_commits"]
                else:
                    reasons = ["first_review"]

                job_id = db.enqueue_job(
                    conn, "review", f"review:{repo}:PR:{pr_number}",
                    payload={
                        "repo": repo,
                        "pr_number": pr_number,
                        "title": pr_title,
                        "reasons": reasons,
                        "latest_head_sha": head_sha,
                        "base_ref": base_ref,
                        "head_ref": head_ref,
                    },
                    debounce_seconds=0,
                    min_run_after=windows.next_allowed_after(
                        "review", datetime.now(timezone.utc)
                    ),
                )
                if job_id:
                    log.info("Poll [%s]: enqueued review for PR #%d (reasons=%s)", _repo_short(repo), pr_number, reasons)
                    enqueued += 1
    except Exception as exc:
        log.warning("Poll: failed to scan review candidates: %s", exc)

    # ── 3. Also check for thread replies on previously reviewed PRs ───────
    try:
        # Use GraphQL to batch-query PRs where we have reviews
        owner, name = repo.split("/")
        gql_result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"""query {{
  repository(owner: "{owner}", name: "{name}") {{
    pullRequests(states: OPEN, first: 50) {{
      nodes {{
        number
        title
        headRefName
        baseRefName
        headRefOid
        reviews(author: "{github_user}", last: 1) {{
          nodes {{ id state }}
        }}
        reviewThreads(first: 100) {{
          nodes {{
            isResolved
            comments(first: 20) {{
              nodes {{
                author {{ login }}
                updatedAt
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}"""],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if gql_result.returncode == 0:
            gql_data = json.loads(gql_result.stdout)
            prs = gql_data.get("data", {}).get("repository", {}).get("pullRequests", {}).get("nodes", [])
            for pr in prs:
                reviews = pr.get("reviews", {}).get("nodes", [])
                if not reviews:
                    continue  # We haven't reviewed this PR

                pr_number = pr.get("number")
                head_sha = pr.get("headRefOid", "")
                base_ref = pr.get("baseRefName", default_branch)
                head_ref = pr.get("headRefName", "")

                # Check for unresolved threads with replies after our last comment
                threads = pr.get("reviewThreads", {}).get("nodes", [])
                has_new_replies = False
                for thread in threads:
                    if thread.get("isResolved"):
                        continue
                    comments = thread.get("comments", {}).get("nodes", [])
                    our_last = None
                    their_last = None
                    for c in comments:
                        author = c.get("author", {}).get("login", "")
                        updated = c.get("updatedAt", "")
                        if author == github_user:
                            our_last = updated
                        elif author not in IGNORED_BOTS:
                            their_last = updated
                    if their_last and our_last and their_last > our_last:
                        has_new_replies = True
                        break
                    if their_last and not our_last:
                        has_new_replies = True
                        break

                if has_new_replies:
                    pr_title = pr.get("title", "")
                    job_id = db.enqueue_job(
                        conn, "review", f"review:{repo}:PR:{pr_number}",
                        payload={
                            "repo": repo,
                            "pr_number": pr_number,
                            "title": pr_title,
                            "reasons": ["thread_reply"],
                            "latest_head_sha": head_sha,
                            "base_ref": base_ref,
                            "head_ref": head_ref,
                        },
                        debounce_seconds=0,
                        min_run_after=windows.next_allowed_after(
                            "review", datetime.now(timezone.utc)
                        ),
                    )
                    if job_id:
                        log.info("Poll [%s]: enqueued review for PR #%d (thread replies)", _repo_short(repo), pr_number)
                        enqueued += 1
    except Exception as exc:
        log.warning("Poll: failed to scan thread replies: %s", exc)

    # ── 4. Issues assigned to us ──────────────────────────────────────────
    try:
        valid_issues = validate_issue_assignments(repo, github_user)
        for issue in valid_issues:
            issue_number = issue["number"]
            # Check if we already have a PR linked to this issue via timeline API
            timeline_result = subprocess.run(
                ["gh", "api", "--paginate",
                 f"/repos/{repo}/issues/{issue_number}/timeline",
                 "--jq", f'[.[] | select(.event == "cross-referenced") | select(.source.issue.pull_request) | select(.source.issue.user.login == "{github_user}") | select(.source.issue.state == "open")] | length'],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            if timeline_result.returncode == 0 and timeline_result.stdout.strip().isdigit():
                if int(timeline_result.stdout.strip()) > 0:
                    continue

            issue_title = issue.get("title", "")
            job_id = db.enqueue_job(
                conn, "implement", f"implement:{repo}:ISSUE:{issue_number}",
                payload={
                    "repo": repo,
                    "issue_number": issue_number,
                    "title": issue_title,
                    "reasons": ["poll_assigned"],
                },
                debounce_seconds=0,
                min_run_after=windows.next_allowed_after(
                    "implement", datetime.now(timezone.utc)
                ),
            )
            if job_id:
                log.info("Poll [%s]: enqueued implement for issue #%d", _repo_short(repo), issue_number)
                enqueued += 1
    except Exception as exc:
        log.warning("Poll: failed to scan assigned issues: %s", exc)

    log.info("Poll complete for %s: %d jobs enqueued", repo, enqueued)
    return enqueued


_SKIP_REASON_LABELS = {
    "pr_merged": "PR was merged",
    "pr_closed": "PR was closed",
    "already_reviewed_this_sha": "already reviewed this version",
    "no_longer_assigned": "no longer assigned",
    "assigner_unknown": "couldn't identify who assigned it",
    "pr_is_draft": "PR is a draft",
    "label_removed_no_prior_review": "review label was removed",
    "sha_stale": "new commits were pushed since",
    "pr_already_exists": "PR already exists for this",
    "missing_pr_number": "no PR number",
    "missing_issue_number": "no issue number",
    "cannot_verify_assignment": "couldn't verify assignment",
    "cannot_verify_assigner_permission": "couldn't verify assigner permissions",
    "invalid_branch_name": "couldn't derive a valid branch name",
    "setup_skip": "skipped during setup",
}


def _humanize_skip(reason: str) -> str:
    """Turn an internal skip reason into a readable phrase."""
    # Check for dynamic patterns like "assigner_X_unauthorized_role_Y"
    if "unauthorized_role" in reason:
        parts = reason.split("_")
        # assigner_{user}_unauthorized_role_{role}
        user_idx = parts.index("assigner") + 1
        return f"assigner *{parts[user_idx]}* doesn't have permission"
    return _SKIP_REASON_LABELS.get(reason, reason.replace("_", " "))


def _repo_short(repo: str) -> str:
    """Return the short name of a repo, e.g. 'ls1intum/Artemis' → 'Artemis'."""
    return repo.split("/")[-1] if "/" in repo else repo


def _gh_link(repo: str, pr: int | str | None = None, issue: int | str | None = None) -> str:
    """Return a Slack mrkdwn link for a PR or issue, e.g. '<url|Artemis PR #123>'."""
    short = _repo_short(repo)
    if pr:
        return f"<https://github.com/{repo}/pull/{pr}|{short} PR #{pr}>"
    if issue:
        return f"<https://github.com/{repo}/issues/{issue}|{short} issue #{issue}>"
    return ""


def _describe_job(job_type: str, payload: dict, verb: str = "start") -> str:
    """Return a natural-sounding phrase for a job, e.g. 'Reviewing PR #123 — Fix grading bug'."""
    repo = payload.get("repo", "")
    pr = payload.get("pr_number", "")
    issue = payload.get("issue_number", "")
    target = _gh_link(repo, pr=pr) if pr else _gh_link(repo, issue=issue) if issue else ""
    title = payload.get("title", "")

    # For start/done, include the title inline for context
    if title and verb in ("start", "done"):
        target_with_title = f"{target} — {title}"
    else:
        target_with_title = target

    if verb == "start":
        phrases = {
            "feedback": f"Looking at feedback on {target_with_title}",
            "review": f"Reviewing {target_with_title}",
            "ci_check": f"Checking CI results on {target_with_title}",
            "implement": f"Working on {target_with_title}",
            "hygiene": "Running hygiene checks",
            "memory": "Organizing my notes",
        }
    elif verb == "done":
        phrases = {
            "feedback": f"Handled feedback on {target_with_title}",
            "review": f"Finished reviewing {target_with_title}",
            "ci_check": f"Looked into CI on {target_with_title}",
            "implement": f"Done with {target_with_title}",
            "hygiene": "Finished hygiene checks",
            "memory": "Notes organized",
        }
    elif verb == "skip":
        phrases = {
            "feedback": f"feedback on {target}",
            "review": f"review of {target}",
            "ci_check": f"CI check on {target}",
            "implement": target,
            "hygiene": "hygiene checks",
            "memory": "memory processing",
        }
    elif verb == "retry":
        phrases = {
            "feedback": f"feedback on {target}",
            "review": f"reviewing {target}",
            "ci_check": f"CI check on {target}",
            "implement": target,
            "hygiene": "hygiene checks",
            "memory": "memory processing",
        }
    else:
        return f"{job_type} {target}".strip()

    return phrases.get(job_type, f"{job_type} {target}".strip())


# ── Worker loop ───────────────────────────────────────────────────────────────


def _ensure_conn(conn):
    """Return a healthy connection, reconnecting if needed.

    Recovers from any `psycopg2.Error` — not just transport-level errors —
    because a prior query can have left the connection in
    `InFailedSqlTransaction`. A best-effort `rollback()` resets that state
    without forcing a full reconnect.
    """
    try:
        if conn and not conn.closed:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # Transport error → full reconnect below.
        pass
    except psycopg2.Error:
        # SQL-level error (likely InFailedSqlTransaction from a prior
        # failed query). Try to rollback and reuse the connection; if
        # rollback itself fails we fall through to reconnect.
        try:
            conn.rollback()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return conn
        except Exception:
            pass
    try:
        if conn and not conn.closed:
            conn.close()
    except Exception:
        pass
    log.info("Reconnecting to database...")
    new_conn = db.connect()
    return new_conn


def classify_nap_state(
    pending_by_type: dict[str, int],
    allowed_types: list[str],
    now: datetime,
) -> tuple[str, datetime | None]:
    """Classify the 'claim returned nothing' state into one of three branches.

    Returns:
        ("empty", None)            — no pending jobs at all.
        ("window_blocked", target) — pending jobs exist, none of their types
                                     overlap allowed_types; target is the
                                     earliest next-allowed datetime across
                                     the blocked pending types.
        ("debounce", None)         — pending jobs exist and overlap
                                     allowed_types, but claim still returned
                                     nothing (their run_after is in the future).
    """
    if not pending_by_type:
        return ("empty", None)
    pending_types = set(pending_by_type.keys())
    allowed_set = set(allowed_types)
    if pending_types & allowed_set:
        return ("debounce", None)
    target = windows.next_allowed_for_types(pending_types, now)
    return ("window_blocked", target)


def worker_loop(conn, github_user: str, memories_dir: Path) -> None:
    """Main worker loop — runs until interrupted."""
    claudia_dir = str(SCRIPT_DIR)
    worker_pid = os.getpid()

    trusted_users: dict[str, list[str]] = {}  # keyed by repo
    trusted_users_at: dict[str, float] = {}
    periodic_at: float = 0
    github_poll_at: float = 0  # 0 = run immediately on first iteration
    cleanup_at: float = 0
    stale_recovery_at: float = 0
    quota_paused_until: float = 0
    idle_announced: bool = False
    window_sleep_announced_until: datetime | None = None
    # Review-digest transition detection (None = cold start — no retroactive fire).
    was_in_own_window: bool | None = None

    while True:
        # ── Ensure DB connection is healthy ───────────────────────────
        try:
            conn = _ensure_conn(conn)
        except Exception as exc:
            log.error("DB reconnect failed: %s, sleeping 30s", exc)
            time.sleep(30)
            continue

        now = time.monotonic()

        # ── Refresh trusted users (hourly, per repo) ─────────────────────
        for _repo in REPO_CONTEXTS:
            if now - trusted_users_at.get(_repo, 0) > TRUSTED_USERS_TTL:
                try:
                    trusted_users[_repo] = get_trusted_users(_repo)
                    log.info("Refreshed trusted users for %s: %s", _repo, trusted_users[_repo])
                except Exception as exc:
                    log.warning("Failed to refresh trusted users for %s: %s", _repo, exc)
                trusted_users_at[_repo] = now

        # ── Enqueue periodic jobs (every 4h) ──────────────────────────────
        if now - periodic_at > PERIODIC_INTERVAL:
            try:
                enqueue_periodic_jobs(conn)
            except Exception as exc:
                log.warning("Failed to enqueue periodic jobs: %s", exc)
            periodic_at = now

        # ── Poll GitHub for missed work (every 30min, also on startup) ────
        if now - github_poll_at > GITHUB_POLL_INTERVAL:
            try:
                poll_github(conn, github_user)
            except Exception as exc:
                log.warning("GitHub poll failed: %s", exc)
            github_poll_at = now

        # ── Recover stale processing jobs (every 5 min) ──────────────────
        if now - stale_recovery_at > 300:
            try:
                recovered = db.recover_stale_jobs(conn)
                if recovered:
                    log.info("Recovered %d stale jobs", recovered)
            except Exception as exc:
                log.warning("Stale job recovery failed: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
            stale_recovery_at = now

        # ── Cleanup old jobs/deliveries (every 6h) ────────────────────────
        if now - cleanup_at > 6 * 3600:
            try:
                archived = db.cleanup_old_jobs(conn)
                if archived:
                    log.info("Archived %d old jobs", archived)
                cleaned = db.cleanup_old_deliveries(conn)
                if cleaned:
                    log.info("Cleaned %d old webhook deliveries", cleaned)
                pruned = db.cleanup_old_review_rows(conn)
                if pruned:
                    log.info("Pruned %d old review rows", pruned)
            except Exception as exc:
                log.warning("Cleanup failed: %s", exc)
                # A failed cleanup query can leave the connection in
                # `InFailedSqlTransaction`. Rollback here so the next
                # claim_next_job doesn't degrade the main loop into
                # "DB reconnect failed" spam.
                try:
                    conn.rollback()
                except Exception:
                    pass
            cleanup_at = now

        # ── Quota pause ───────────────────────────────────────────────────
        if time.time() < quota_paused_until:
            sleep_for = quota_paused_until - time.time()
            log.info("Quota paused, sleeping %.0fs", sleep_for)
            time.sleep(min(sleep_for, 60))
            continue

        # ── Claim next job (with working-hours gating) ───────────────────
        now_utc = datetime.now(timezone.utc)
        was_in_own_window = _run_digest_tick(
            conn, now_utc,
            prev_in_own=was_in_own_window,
            github_user=github_user,
        )
        window_allowed = [
            t for t in db.JOB_TYPES if windows.is_allowed_now(t, now_utc)
        ]
        claim_allowed = list(window_allowed)
        nap_allowed = list(window_allowed)
        # Gate 3 bypass: if a bypass review row exists outside the review
        # window, let the worker actually claim it. If one exists but is
        # still in debounce/backoff, silence the "sleeping until 19:01"
        # announce by including review in the nap-state allowed set so
        # classify_nap_state returns "debounce" instead of "window_blocked".
        if "review" not in window_allowed:
            try:
                if db.pending_ready_bypass_review_exists(conn):
                    claim_allowed.append("review")
                    nap_allowed.append("review")
                elif db.pending_any_bypass_review_exists(conn):
                    nap_allowed.append("review")
            except Exception as exc:
                log.warning("pending_bypass_review check failed: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass

        if not claim_allowed:
            job = None
        else:
            try:
                job = db.claim_next_job(
                    conn, worker_pid, claim_allowed, backend=BACKEND.name,
                )
            except Exception as exc:
                log.error("Failed to claim job: %s", exc)
                time.sleep(POLL_INTERVAL)
                continue

        if not job:
            try:
                pending = db.pending_by_type(conn)
            except Exception as exc:
                log.warning("pending_by_type failed: %s", exc)
                pending = {}

            state, target = classify_nap_state(pending, nap_allowed, now_utc)

            if state == "empty":
                if not idle_announced:
                    slack_send(
                        "😴 Nothing in the queue — taking a nap until something comes in"
                    )
                    idle_announced = True
                window_sleep_announced_until = None
            elif state == "window_blocked":
                # Re-announce only when the target datetime changes.
                if target is not None and target != window_sleep_announced_until:
                    slack_send(
                        f"😴 Sleeping until {target.strftime('%H:%M')} UTC — "
                        f"outside my working hours"
                    )
                    window_sleep_announced_until = target
                idle_announced = False
            else:  # "debounce"
                # Normal debounce silence — nothing to announce.
                pass

            time.sleep(POLL_INTERVAL)
            continue

        idle_announced = False
        window_sleep_announced_until = None
        job_id = job["id"]
        job_type = job["type"]
        payload = job["payload"] if isinstance(job["payload"], dict) else json.loads(job["payload"])
        retry_count = job["retry_count"]
        ambiguous_count = job["ambiguous_count"]

        # ── Extract repo from payload ─────────────────────────────────
        repo = payload.get("repo")
        if not repo:
            log.error("Job %d has no repo field, skipping", job_id)
            db.skip_job(conn, job_id, "missing_repo_field")
            continue
        if repo not in REPO_CONTEXTS:
            log.error("Job %d references unknown repo %s, skipping", job_id, repo)
            db.skip_job(conn, job_id, f"unknown_repo:{repo}")
            continue

        repo_ctx = REPO_CONTEXTS[repo]
        repo_path = repo_ctx["path"]
        default_branch = repo_ctx["default_branch"]
        trusted_users_json = json.dumps(trusted_users.get(repo, []), separators=(",", ":"))

        log.info(
            "Claimed job %d [%s]: type=%s, dedup=%s, priority=%d, retry=%d, ambiguous=%d",
            job_id, _repo_short(repo), job_type, job["dedup_key"], job["priority"],
            retry_count, ambiguous_count,
        )

        # Slack: announce job start
        _job_desc = _describe_job(job_type, payload, "start")
        _retry_info = ""
        if retry_count > 0:
            _retry_info = f" (retry {retry_count})"
        elif ambiguous_count > 0:
            _retry_info = f" (retry {ambiguous_count})"
        try:
            _pending = db.queue_status(conn).get("pending", 0)
            _queue_info = f" · {_pending} more in queue"
        except Exception:
            _queue_info = ""
        slack_send(f"⚙️ {_job_desc}{_retry_info}{_queue_info}")

        # ── Quota check (fail-closed: if we can't check, don't run) ────
        quota_blocked = False
        quota = BACKEND.query_quota()
        if quota is None:
            # Can't determine quota — release job and retry shortly
            log.warning("Quota check failed (no data), releasing job %d", job_id)
            slack_send(f"⚠️ Can't check my quota right now, putting {_describe_job(job_type, payload, 'retry')} back for a bit")
            db.release_job(conn, job_id, run_after_seconds=60)
            time.sleep(POLL_INTERVAL)
            quota_blocked = True
        elif quota:
            for key, threshold in [("session", 10)]:
                w = quota.get(key)
                if w and w["remaining_pct"] < threshold:
                    log.info("Quota %s at %.0f%% (<%d%%), entering backpressure",
                             key, w["remaining_pct"], threshold)
                    db.release_job(conn, job_id, run_after_seconds=300)
                    reset_str = w.get("resets_in", "5m")
                    quota_paused_until = time.time() + _parse_duration(reset_str)
                    slack_send(f"😴 Taking a nap — quota's running low ({key}: {w['remaining_pct']:.0f}%), I'll be back when it resets")
                    quota_blocked = True
                    break
        if quota_blocked:
            continue

        # ── Reconcile (for retries/ambiguous) ─────────────────────────────
        if retry_count > 0 or ambiguous_count > 0:
            try:
                if reconcile_shows_complete(job, github_user, repo):
                    log.info("Job %d: reconciliation shows work already done, completing", job_id)
                    db.complete_job(conn, job_id)
                    db.record_attempt(
                        conn, job_id, "success",
                        datetime.now(timezone.utc), datetime.now(timezone.utc),
                        result_metadata={"reconciled": True},
                        backend=BACKEND.name,
                    )
                    continue
            except Exception as exc:
                log.warning("Reconciliation failed for job %d: %s", job_id, exc)

        # ── Validate job ──────────────────────────────────────────────────
        try:
            skip_reason = validate_job(job, github_user, repo)
        except Exception as exc:
            log.warning("Validation failed for job %d: %s", job_id, exc)
            skip_reason = None

        if skip_reason:
            log.info("Job %d [%s] skipped: %s", job_id, _repo_short(repo), skip_reason)
            db.skip_job(conn, job_id, skip_reason)
            db.record_attempt(
                conn, job_id, "permanent_skip",
                datetime.now(timezone.utc), datetime.now(timezone.utc),
                error_message=skip_reason,
                backend=BACKEND.name,
            )
            _skip_desc = _describe_job(job_type, payload, "skip")
            slack_send(f"⏭️ Skipping {_skip_desc} — {_humanize_skip(skip_reason)}")
            continue

        # ── Setup repo ────────────────────────────────────────────────────
        try:
            clean_repo(repo_path, default_branch)
        except Exception as exc:
            log.error("Repo cleanup failed for job %d: %s", job_id, exc)
            db.retry_job(conn, job_id, f"repo_cleanup_failed: {exc}", BASE_BACKOFF)
            continue

        # ── Setup for job ─────────────────────────────────────────────────
        try:
            extra_replacements = setup_for_job(
                job_type=job_type,
                payload=payload,
                repo_path=repo_path,
                repo=repo,
                github_user=github_user,
                default_branch=default_branch,
            )
        except Exception as exc:
            log.error("Job setup failed for job %d [%s]: %s", job_id, _repo_short(repo), exc)
            db.retry_job(conn, job_id, f"setup_failed: {exc}", BASE_BACKOFF)
            continue

        # Short-circuit if setup says to skip (e.g., ALREADY_REVIEWED)
        if extra_replacements.get("skip"):
            skip_delta = extra_replacements.get("skip_delta", {})
            skip_reason = skip_delta.get("reason", "setup_skip")
            log.info("Job %d short-circuited by setup: %s", job_id, skip_reason)
            db.complete_job(conn, job_id)
            db.record_attempt(
                conn, job_id, "success",
                datetime.now(timezone.utc), datetime.now(timezone.utc),
                result_metadata=skip_delta,
                backend=BACKEND.name,
            )
            _skip_desc = _describe_job(job_type, payload, "skip")
            slack_send(f"⏭️ Skipping {_skip_desc} — {_humanize_skip(skip_reason)}")
            continue

        # ── Hygiene: per-PR iteration ────────────────────────────────────
        if job_type == "hygiene":
            try:
                started_at = datetime.now(timezone.utc)
                hygiene_delta = _run_hygiene_batch(
                    job_id=job_id,
                    job=job,
                    github_user=github_user,
                    trusted_users_json=trusted_users_json,
                    memories_dir=str(memories_dir),
                    claudia_dir=claudia_dir,
                    repo=repo,
                )
                finished_at = datetime.now(timezone.utc)
                db.complete_job(conn, job_id)
                db.record_attempt(
                    conn, job_id, "success",
                    started_at, finished_at,
                    result_metadata=hygiene_delta,
                    backend=BACKEND.name,
                )
                _log_success(job, hygiene_delta, None, None, None, started_at, finished_at)
            except KeyboardInterrupt:
                log.info("Interrupted during hygiene job %d, releasing", job_id)
                db.release_job(conn, job_id)
                raise
            except Exception as exc:
                log.error("Hygiene batch failed for job %d: %s", job_id, exc)
                db.retry_job(conn, job_id, f"hygiene_batch_failed: {exc}", BASE_BACKOFF)
            continue

        # Sanitize instruction files
        try:
            sanitize_instruction_files(repo_path, default_branch)
        except Exception as exc:
            log.warning("Instruction file sanitization failed for job %d: %s", job_id, exc)

        # ── Build prompt ──────────────────────────────────────────────────
        try:
            prompt, model, effort_or_turns = build_agent_prompt(
                job=job,
                github_user=github_user,
                trusted_users_json=trusted_users_json,
                memories_dir=str(memories_dir),
                claudia_dir=claudia_dir,
                repo=repo,
                extra_replacements=extra_replacements,
            )
        except Exception as exc:
            log.error("Prompt build failed for job %d: %s", job_id, exc)
            db.retry_job(conn, job_id, f"prompt_build_failed: {exc}", BASE_BACKOFF)
            continue

        # ── Run via backend strategy ──────────────────────────────────────
        output_fd, output_file = tempfile.mkstemp(prefix=f"claudia-job-{job_id}-", suffix=".jsonl")
        os.close(output_fd)
        timeout = JOB_TIMEOUTS.get(job_type, 60 * 60)
        started_at = datetime.now(timezone.utc)
        run_ctx = None

        try:
            run_result = backends.run_with_heartbeat(
                BACKEND,
                prompt=prompt,
                cwd=repo_path,
                model=model,
                effort_or_turns=effort_or_turns,
                job_id=job_id,
                timeout_seconds=timeout,
                output_file=output_file,
            )
            exit_code = run_result.exit_code
            run_ctx = run_result.ctx
        except KeyboardInterrupt:
            log.info("Interrupted during job %d, releasing", job_id)
            db.release_job(conn, job_id)
            raise
        except Exception as exc:
            log.error("Backend execution error for job %d: %s", job_id, exc)
            exit_code = -2

        finished_at = datetime.now(timezone.utc)

        # ── Parse output & classify outcome ───────────────────────────────
        if run_ctx is not None:
            parsed = BACKEND.parse_output(run_ctx, output_file)
        else:
            # We never got a ctx (runner raised before constructing one).
            # parse_output is total — feed it a synthetic ctx so it can read
            # whatever (likely empty) output_file is there.
            from backends.base import RunContext
            parsed = BACKEND.parse_output(
                RunContext(prompt_path="", cwd=repo_path, model=model, effort_or_turns=effort_or_turns),
                output_file,
            )

        cost_usd = parsed.cost_usd
        tokens_in = parsed.tokens_in
        tokens_out = parsed.tokens_out

        outcome, state_delta = classify_outcome(
            exit_code, parsed, require_delta_on_success=BACKEND.requires_delta_for_success,
        )

        log.info(
            "Job %d: exit=%d, outcome=%s, cost=$%.2f, delta=%s",
            job_id, exit_code, outcome,
            cost_usd or 0, "yes" if state_delta else "no",
        )

        # ── Record attempt ────────────────────────────────────────────────
        attempt_outcome = outcome
        if outcome == "success":
            attempt_outcome = "success"
        elif outcome == "ambiguous":
            attempt_outcome = "ambiguous"
        else:
            attempt_outcome = "transient_failure"

        error_msg = None
        if exit_code != 0:
            error_msg = f"exit_code={exit_code}"
        if exit_code == -1:
            error_msg = "timeout"

        db.record_attempt(
            conn, job_id,
            outcome=attempt_outcome,
            started_at=started_at,
            finished_at=finished_at,
            error_message=error_msg,
            result_metadata=state_delta,
            claude_exit_code=exit_code if exit_code >= 0 else None,
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            backend=BACKEND.name,
        )

        # ── Handle outcome ────────────────────────────────────────────────
        if outcome == "success":
            db.complete_job(conn, job_id)
            _log_success(job, state_delta, cost_usd, tokens_in, tokens_out, started_at, finished_at)
            if state_delta:
                try:
                    import review_requests
                    review_requests._maybe_announce_review(
                        conn, repo, state_delta, datetime.now(timezone.utc)
                    )
                except Exception as exc:
                    log.warning("_maybe_announce_review failed: %s", exc)

        elif outcome == "ambiguous":
            backoff = _exponential_backoff(ambiguous_count)
            new_status = db.ambiguous_job(conn, job_id, error_msg or "ambiguous", backoff)
            log.warning(
                "Job %d ambiguous (count=%d) → %s (backoff=%ds)",
                job_id, ambiguous_count + 1, new_status, backoff,
            )
            if new_status == "dead_letter":
                _throttled_slack_alert(f"🚨 Tried {_describe_job(job_type, payload, 'retry')} {ambiguous_count + 1} times but keep getting unclear results — giving up")

        else:  # transient_failure
            backoff = _exponential_backoff(retry_count)
            new_status = db.retry_job(conn, job_id, error_msg or "transient_failure", backoff)
            log.warning(
                "Job %d transient failure (retry=%d) → %s (backoff=%ds)",
                job_id, retry_count + 1, new_status, backoff,
            )
            if new_status == "dead_letter":
                _retry_desc = _describe_job(job_type, payload, "retry")
                _throttled_slack_alert(f"🚨 {_retry_desc.capitalize()} keeps failing ({retry_count + 1} attempts), moving on")

        # ── Cleanup ───────────────────────────────────────────────────────
        try:
            os.unlink(output_file)
        except OSError:
            pass

        # Reset repo to default branch between jobs
        try:
            clean_repo(repo_path, default_branch)
        except Exception:
            pass


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


def _log_success(job, delta, cost, tokens_in, tokens_out, started, finished):
    """Send a Slack message for successful job completion."""
    job_type = job["type"]
    payload = job["payload"] if isinstance(job["payload"], dict) else json.loads(job["payload"])
    duration = (finished - started).total_seconds()

    done_desc = _describe_job(job_type, payload, "done")
    parts = [f"✅ {done_desc} · {duration / 60:.0f}min"]
    if cost:
        parts[0] += f" · `${cost:.2f}`"
    if tokens_in and tokens_out:
        parts[0] += f" · `↑{_fmt_tokens(tokens_in)} ↓{_fmt_tokens(tokens_out)}`"

    # Append quota status
    quota = BACKEND.query_quota()
    if quota:
        for key, label in [("session", "5h"), ("weekly", "7d")]:
            w = quota.get(key)
            if not w:
                continue
            pct = w["remaining_pct"]
            bar = _progress_bar(pct)
            resets = w.get("resets_in", "?")
            parts.append(f">`{bar}` *{pct:.0f}%* left ({label}) \u2014 resets in {resets}")

    slack_send("\n".join(parts))


_ALERT_WINDOW = 120  # seconds
_ALERT_MAX_PER_WINDOW = 3
_alert_count = 0
_alert_window_start = 0.0
_alert_suppressed = 0


def _throttled_slack_alert(msg: str) -> None:
    """Send a Slack alert, but suppress if too many in a short window."""
    global _alert_count, _alert_window_start, _alert_suppressed
    now = time.time()
    if now - _alert_window_start > _ALERT_WINDOW:
        # New window — flush suppression count if any
        if _alert_suppressed > 0:
            slack_alert(f"🔇 ({_alert_suppressed} more alert{'s' if _alert_suppressed != 1 else ''} suppressed)")
        _alert_count = 0
        _alert_suppressed = 0
        _alert_window_start = now
    _alert_count += 1
    if _alert_count <= _ALERT_MAX_PER_WINDOW:
        slack_alert(msg)
    else:
        _alert_suppressed += 1
        log.warning("Slack alert suppressed (throttled): %s", msg)


def _exponential_backoff(attempt: int) -> int:
    """Calculate exponential backoff with jitter."""
    backoff = min(BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)
    jitter = random.randint(0, backoff // 2)
    return backoff + jitter


def _parse_duration(s: str) -> float:
    """Parse a duration string like '5m', '2h', '30s' into seconds."""
    match = re.match(r"(\d+)\s*([smhd])", s.lower())
    if not match:
        return 300  # Default 5 minutes
    val, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * multipliers.get(unit, 60)


# ── CLI subcommands ───────────────────────────────────────────────────────────


def cmd_status(conn) -> int:
    """Print queue status."""
    status = db.queue_status(conn)
    by_type = db.pending_by_type(conn)

    print("=== Claudia Job Queue ===")
    for s in ("pending", "processing", "completed", "skipped", "dead_letter", "archived"):
        count = status.get(s, 0)
        if count > 0 or s in ("pending", "processing", "dead_letter"):
            print(f"  {s:15s}: {count}")

    if by_type:
        print("\nPending by type:")
        for t, count in sorted(by_type.items()):
            print(f"  {t:15s}: {count}")
    print("=========================")
    return 0


def cmd_requeue(conn, job_id: int) -> int:
    """Requeue a dead_letter job."""
    if db.requeue_job(conn, job_id):
        print(f"Job {job_id} requeued (dead_letter → pending)")
        return 0
    else:
        print(f"Job {job_id} not found or not in dead_letter status")
        return 1


def cmd_release(conn, job_id: int, *, force: bool) -> int:
    """Release a job back to pending."""
    status = db.get_job_status(conn, job_id)
    if status is None:
        print(f"Job {job_id} not found")
        return 1
    if status != "processing" and not force:
        print(f"Job {job_id} is in status {status!r}, not 'processing'. "
              f"Use --force to release anyway.")
        return 1
    db.release_job(conn, job_id)
    if status == "processing":
        print(f"Released job {job_id}")
    else:
        log.warning("Released job %d (was: %s)", job_id, status)
        print(f"Released job {job_id} (was: {status})")
    return 0


def cmd_drain(conn) -> int:
    """Emergency: move all pending jobs to dead_letter."""
    count = db.drain_all(conn)
    print(f"Drained {count} pending jobs → dead_letter")
    slack_alert(f"🚨 Emergency stop — moved {count} pending jobs to dead letter")
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Claudia worker")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show queue status")

    requeue_p = sub.add_parser("requeue", help="Requeue a dead_letter job")
    requeue_p.add_argument("job_id", type=int, help="Job ID to requeue")

    release_p = sub.add_parser("release", help="Release a processing job back to pending")
    release_p.add_argument("job_id", type=int)
    release_p.add_argument("--force", action="store_true",
                           help="Release regardless of current status (otherwise only 'processing')")

    drain_p = sub.add_parser("drain", help="Emergency: drain all pending jobs")
    drain_p.add_argument("--force", action="store_true", required=True, help="Confirm drain")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Connect to PG
    try:
        db.ensure_database()
        conn = db.connect()
        db.migrate(conn)
    except Exception as exc:
        log.error("Database connection failed: %s", exc)
        return 1

    # Handle subcommands
    if args.command == "status":
        return cmd_status(conn)
    elif args.command == "requeue":
        return cmd_requeue(conn, args.job_id)
    elif args.command == "release":
        return cmd_release(conn, args.job_id, force=args.force)
    elif args.command == "drain":
        return cmd_drain(conn)

    # ── Backend preflight + agent validation (worker mode only) ─────────────
    BACKEND.preflight()  # may SystemExit(1)
    BACKEND.validate_agents(SCRIPT_DIR / "agents")  # may raise PromptBuildError

    # ── Worker mode ───────────────────────────────────────────────────────

    # File lock
    lock_path = os.path.expanduser(
        os.environ.get("LOCK_FILE_PATH", DEFAULT_LOCK_PATH)
    )
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another worker is already running (lock held at %s)", lock_path)
        os.close(lock_fd)
        return 1

    log.info("Lock acquired at %s", lock_path)

    memories_dir = Path(os.path.expanduser(
        os.environ.get("MEMORIES_DIR", DEFAULT_MEMORIES_DIR)
    )).resolve()

    # Load repos config
    try:
        global REPO_CONTEXTS
        repos_config = load_repos_config(SCRIPT_DIR / "repos.json")
    except Exception as exc:
        slack_alert(f"🚨 Can't load repos.json: {exc}")
        return 1

    # Validate prerequisites
    if not check_gh_auth(repos_config):
        slack_alert("🚨 Can't authenticate with GitHub — check gh CLI setup")
        return 1

    github_user = detect_github_user()
    if not github_user:
        slack_alert("🚨 Can't figure out who I am — GITHUB_USER not set and detection failed")
        return 1

    # Initialize each repo
    for repo, settings in repos_config.items():
        repo_path = Path(settings["checkout_path"])
        try:
            ensure_repo(repo_path, repo)
        except Exception as exc:
            slack_alert(f"🚨 Can't set up {repo}: {exc}")
            return 1
        if not validate_repo_remote(repo_path, repo):
            slack_alert(f"🚨 Repo at {repo_path} points to wrong remote for {repo}")
            return 1
        default_branch = detect_default_branch(repo)
        setup_directories(memories_dir, repo)

        REPO_CONTEXTS[repo] = {
            "path": str(repo_path),
            "default_branch": default_branch,
            "screenshots": settings.get("screenshots", False),
            "review_label": settings.get("review_label"),
        }
        log.info("Configured repo %s: path=%s, branch=%s, screenshots=%s, review_label=%s",
                 repo, repo_path, default_branch, settings.get("screenshots", False),
                 settings.get("review_label", "(none)"))

    # Review-request helper state. Resolve the Slack bot user id via
    # auth.test in-process — never shell out, the token must NOT appear
    # in the process command line.
    import review_requests
    from slack_api import slack_auth_test
    try:
        auth_result = slack_auth_test()
    except Exception as exc:
        log.warning("slack_auth_test raised: %s", exc)
        auth_result = {"result": "ambiguous_failure", "error": str(exc)}
    if auth_result.get("result") == "ok":
        uid = auth_result.get("user_id") or None
        review_requests.WORKER_STATE["claudia_bot_user_id"] = uid
        log.info("Resolved Slack bot user id: %s", uid)
    else:
        log.warning(
            "Slack auth.test %s: %s",
            auth_result.get("result"), auth_result.get("error"),
        )

    review_requests.REPO_LIST_PROVIDER = lambda: list(REPO_CONTEXTS.keys())

    # Recover stale jobs from previous crashes
    recovered = db.recover_stale_jobs(conn)
    if recovered:
        log.info("Recovered %d stale jobs", recovered)

    # Backfill pending/processing jobs from pre-multi-repo era:
    # 1) Add repo to payload, 2) Scope dedup keys with repo prefix
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE jobs
                   SET payload = payload || '{"repo": "ls1intum/Artemis"}'::jsonb,
                       updated_at = now()
                   WHERE status IN ('pending', 'processing')
                     AND (payload->>'repo' IS NULL)""",
            )
            if cur.rowcount > 0:
                log.info("Backfilled %d legacy jobs with repo=ls1intum/Artemis", cur.rowcount)
            # Rewrite unscoped dedup keys: "feedback:PR:123" → "feedback:ls1intum/Artemis:PR:123"
            # First, delete old unscoped jobs where a scoped one already exists (avoids duplicates)
            cur.execute(
                r"""DELETE FROM jobs j
                   WHERE j.status IN ('pending', 'processing')
                     AND j.dedup_key NOT LIKE '%%/%%'
                     AND EXISTS (
                         SELECT 1 FROM jobs j2
                         WHERE j2.status = 'pending'
                           AND j2.dedup_key = split_part(j.dedup_key, ':', 1)
                                              || ':ls1intum/Artemis:'
                                              || substr(j.dedup_key, length(split_part(j.dedup_key, ':', 1)) + 2)
                     )""",
            )
            if cur.rowcount > 0:
                log.info("Deleted %d legacy jobs superseded by scoped ones", cur.rowcount)
            # Then, rewrite remaining unscoped keys
            cur.execute(
                r"""UPDATE jobs AS j
                   SET dedup_key = split_part(j.dedup_key, ':', 1)
                                   || ':ls1intum/Artemis:'
                                   || substr(j.dedup_key, length(split_part(j.dedup_key, ':', 1)) + 2),
                       updated_at = now()
                   WHERE j.status IN ('pending', 'processing')
                     AND j.dedup_key NOT LIKE '%%/%%'""",
            )
            if cur.rowcount > 0:
                log.info("Rescoped %d legacy dedup keys", cur.rowcount)

    # Clean all repos
    for repo, ctx in REPO_CONTEXTS.items():
        try:
            clean_repo(ctx["path"], ctx["default_branch"])
        except Exception as exc:
            log.warning("Initial cleanup failed for %s: %s", repo, exc)

    slack_send("👋 I'm online and ready to work!")

    try:
        worker_loop(conn, github_user, memories_dir)
    except KeyboardInterrupt:
        log.info("Worker interrupted")
        slack_send("👋 Shutting down, see you later!")
    except Exception as exc:
        log.error("Worker crashed: %s", exc)
        slack_alert(f"💥 I crashed: {exc}")
        return 1
    finally:
        conn.close()
        os.close(lock_fd)
        log.info("Lock released")

    return 0


if __name__ == "__main__":
    sys.exit(main())
