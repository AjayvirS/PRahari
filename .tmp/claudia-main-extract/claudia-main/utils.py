#!/usr/bin/env python3
"""Shared utilities for Claudia worker and webhook receiver.

Extracted from run.py to decouple the event-driven worker from the
legacy cron launcher.
"""

import fcntl
import json
import logging
import os
import random
import signal
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# ── Configuration ─────────────────────────────────────────────────────────────

SUBPROCESS_TIMEOUT = 300  # 5 minutes for individual git/gh commands

# state.json schema version
STATE_SCHEMA_VERSION = 1

# Knowledge JSONL files per repo
KNOWLEDGE_FILES = [
    "coding-patterns.jsonl",
    "review-lessons.jsonl",
    "common-mistakes.jsonl",
    "tooling-notes.jsonl",
]

log = logging.getLogger("claudia")


# ── .env loader ──────────────────────────────────────────────────────────────

def load_dotenv(path: Path) -> None:
    """Load variables from a .env file into os.environ (without overriding)."""
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


# ── Slack helpers ────────────────────────────────────────────────────────────

def slack_send(message: str) -> None:
    """Send a message to Slack. Best-effort — never raises."""
    slack_script = SCRIPT_DIR / "slack.py"
    try:
        subprocess.run(
            [sys.executable, str(slack_script), message],
            timeout=15,
        )
    except Exception as exc:
        log.warning("Failed to send Slack message: %s", exc)


def slack_alert(message: str) -> None:
    """Send an error alert to Slack."""
    slack_send(f":rotating_light: *Claudia launcher error*\n>{message}")


# ── Usage reporting ──────────────────────────────────────────────────────────

def _fmt_tokens(n: int) -> str:
    """Format token count: 942, 12.4k, 1.2M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _progress_bar(remaining_pct: float, width: int = 15) -> str:
    """Build a text progress bar: [========       ]."""
    filled = round(remaining_pct / 100 * width)
    return "[" + "=" * filled + " " * (width - filled) + "]"


# ── State management ─────────────────────────────────────────────────────────

def _default_state(repo: str) -> dict:
    """Return a valid default state.json structure."""
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": None,
        "updated_at": None,
        "last_successful_run_at": None,
        "prior_stages_completed": [],
        "prior_stages_skipped": {},
        "repos": {
            repo: {
                "active_prs": {},
                "issues_in_progress": {},
                "reviewed_prs": {},
            }
        },
        "quota_at_end": {},
    }


def read_state(memories_dir: Path, repo: str) -> dict:
    """Read and validate state.json. Returns default state on failure."""
    state_file = memories_dir / "state.json"
    if not state_file.is_file():
        log.info("No state.json found, creating default state")
        return _default_state(repo)

    try:
        with open(state_file) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Corrupt state.json (%s), rebuilding from default", exc)
        return _default_state(repo)

    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        log.warning(
            "state.json schema_version=%s (expected %d), rebuilding",
            state.get("schema_version"), STATE_SCHEMA_VERSION,
        )
        return _default_state(repo)

    if not isinstance(state.get("run_id"), (str, type(None))):
        state["run_id"] = None
    if not isinstance(state.get("updated_at"), (str, type(None))):
        state["updated_at"] = None
    if not isinstance(state.get("last_successful_run_at"), (str, type(None))):
        state["last_successful_run_at"] = None
    if not isinstance(state.get("prior_stages_completed"), list):
        state["prior_stages_completed"] = []
    if not isinstance(state.get("prior_stages_skipped"), dict):
        state["prior_stages_skipped"] = {}
    if not isinstance(state.get("quota_at_end"), dict):
        state["quota_at_end"] = {}

    if not isinstance(state.get("repos"), dict):
        state["repos"] = {}
    if repo not in state["repos"] or not isinstance(state["repos"][repo], dict):
        state["repos"][repo] = {
            "active_prs": {},
            "issues_in_progress": {},
            "reviewed_prs": {},
        }
    else:
        repo_state = state["repos"][repo]
        for field in ("active_prs", "issues_in_progress", "reviewed_prs"):
            if not isinstance(repo_state.get(field), dict):
                repo_state[field] = {}

    return state


def compact_state_for_prompt(state: dict) -> str:
    """Produce compact JSON suitable for prompt injection."""
    return json.dumps(state, separators=(",", ":"))


# ── GitHub user detection ────────────────────────────────────────────────────

def detect_github_user() -> str:
    """Auto-detect GITHUB_USER via gh api, or fall back to env var."""
    env_user = os.environ.get("GITHUB_USER", "")
    if env_user:
        log.info("Using GITHUB_USER from env: %s", env_user)
        return env_user

    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode == 0 and result.stdout.strip():
            user = result.stdout.strip()
            log.info("Auto-detected GITHUB_USER: %s", user)
            return user
    except Exception as exc:
        log.warning("Failed to auto-detect GITHUB_USER: %s", exc)

    log.error("Could not determine GITHUB_USER — set it in .env or ensure gh is authenticated")
    return ""


# ── Assignment validation ────────────────────────────────────────────────────

FUNNY_REJECTIONS = [
    "@{user} YOU SHALL NOT PASS!",
    "@{user} I appreciate the enthusiasm, but you don't have the authority to boss me around. Nice try though!",
    "@{user} Error 403: Insufficient permissions to send me on missions. Maybe ask a maintainer?",
    "@{user} *checks notes* ...nope, you're not on the list. Only maintainers and admins can tell me what to do!",
    "@{user} I don't take orders from just anyone! Grab a maintainer if you want this done.",
    "@{user} Sorry, I only take marching orders from maintainers. Please escalate up the chain!",
    "@{user} Nice try, but my permission matrix says no. Ping a maintainer to make it happen.",
    "@{user} *adjusts monocle* I'm afraid that request requires maintainer credentials, my good friend.",
]

# Friendly acknowledgments posted when a trusted maintainer triggers an
# on-demand review command. Vary the wording so it doesn't feel robotic.
FRIENDLY_REVIEW_ACKS = [
    "@{user} on it! Taking a look now :eyes:",
    "@{user} got it — starting the review right away.",
    "@{user} acknowledged, queuing this up for immediate review!",
    "@{user} sure thing, diving in now :rocket:",
    "@{user} roger that — review incoming!",
]


def validate_issue_assignments(repo: str, github_user: str) -> list[dict]:
    """Check all issues assigned to github_user. Reject unauthorized ones.

    Returns list of valid issues (dicts with number, title, labels, assigner).
    """
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--assignee", github_user,
         "--state", "open", "--json", "number,title,labels", "--limit", "50"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        log.warning("Failed to list assigned issues (rc=%d): %s",
                    result.returncode, result.stderr.strip())
        return []

    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("Invalid JSON from gh issue list")
        return []

    if not issues:
        return []

    valid = []
    for issue in issues:
        number = issue.get("number")
        if not number:
            continue

        events_result = subprocess.run(
            ["gh", "api", "--paginate", "--slurp",
             f"/repos/{repo}/issues/{number}/events?per_page=100"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if events_result.returncode != 0:
            log.warning("Failed to fetch events for issue #%d (rc=%d), skipping",
                        number, events_result.returncode)
            continue

        try:
            all_events = json.loads(events_result.stdout)
        except json.JSONDecodeError:
            log.warning("Invalid JSON from events API for issue #%d, skipping", number)
            continue

        if all_events and isinstance(all_events[0], list):
            all_events = [e for page in all_events for e in page]

        assigned_events = [
            e for e in all_events
            if e.get("event") == "assigned"
            and e.get("assignee", {}).get("login") == github_user
        ]

        if not assigned_events:
            log.info("No assigned events found for issue #%d, unassigning", number)
            subprocess.run(
                ["gh", "issue", "edit", str(number), "--repo", repo,
                 "--remove-assignee", github_user],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            subprocess.run(
                ["gh", "issue", "comment", str(number), "--repo", repo,
                 "--body", "I couldn't determine who assigned me to this issue, "
                 "so I'm unassigning myself. A maintainer or admin can re-assign me if needed."],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            continue

        latest = max(assigned_events, key=lambda e: e.get("created_at", ""))
        assigner = latest.get("assigner", {}).get("login")

        if not assigner:
            log.info("Assigner is null for issue #%d, unassigning", number)
            subprocess.run(
                ["gh", "issue", "edit", str(number), "--repo", repo,
                 "--remove-assignee", github_user],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            subprocess.run(
                ["gh", "issue", "comment", str(number), "--repo", repo,
                 "--body", "I couldn't determine who assigned me to this issue, "
                 "so I'm unassigning myself. A maintainer or admin can re-assign me if needed."],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            continue

        perm_result = subprocess.run(
            ["gh", "api", f"/repos/{repo}/collaborators/{assigner}/permission",
             "--jq", ".role_name"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if perm_result.returncode != 0:
            log.warning("Permission check failed for %s on issue #%d (rc=%d), skipping",
                        assigner, number, perm_result.returncode)
            continue

        role = perm_result.stdout.strip().strip('"').lower()

        if role in ("maintain", "admin"):
            valid.append({
                "number": number,
                "title": issue.get("title", ""),
                "labels": issue.get("labels", []),
                "assigner": assigner,
            })
            log.info("Issue #%d: valid assignment by %s (role: %s)", number, assigner, role)
        else:
            log.info("Issue #%d: rejecting assignment by %s (role: %s)", number, assigner, role)
            message = random.choice(FUNNY_REJECTIONS).format(user=assigner)
            subprocess.run(
                ["gh", "issue", "edit", str(number), "--repo", repo,
                 "--remove-assignee", github_user],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            subprocess.run(
                ["gh", "issue", "comment", str(number), "--repo", repo,
                 "--body", message],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )

    return valid


def get_trusted_users(repo: str) -> list[str]:
    """Get GitHub usernames with maintain/admin role on the repo.

    Fails closed: raises `RuntimeError` if *either* the admin or the
    maintain permission query fails. A partial result would silently
    misclassify real maintainers as untrusted, which is particularly
    dangerous on paths that post user-visible rejection comments.
    Callers that prefer best-effort behaviour should catch the
    exception and decide how to fall back.
    """
    trusted: set[str] = set()

    for permission in ("admin", "maintain"):
        result = subprocess.run(
            ["gh", "api", "--paginate", "--slurp",
             f"/repos/{repo}/collaborators?permission={permission}&per_page=100"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to fetch {permission} collaborators for {repo} "
                f"(rc={result.returncode}): {result.stderr.strip()}"
            )

        try:
            all_users = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Invalid JSON from collaborators API for {repo}/{permission}"
            ) from exc

        if all_users and isinstance(all_users[0], list):
            all_users = [u for page in all_users for u in page]

        trusted |= {u["login"] for u in all_users if "login" in u}

    return sorted(trusted)


# ── Infrastructure ───────────────────────────────────────────────────────────

def check_gh_auth(repos: dict) -> bool:
    """Verify gh CLI is authenticated and has access to all configured repos."""
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        log.error("gh is not authenticated:\n%s", result.stderr.strip())
        return False

    for repo in repos:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}", "--jq", ".full_name"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            log.error("Cannot access %s:\n%s", repo, result.stderr.strip())
            return False
        canonical = result.stdout.strip()
        if canonical != repo:
            log.error(
                "Repo key mismatch: repos.json has '%s' but GitHub says '%s'",
                repo, canonical,
            )
            return False
        log.info("gh access to %s confirmed", canonical)

    return True


def ensure_repo(repo_path: Path, repo: str) -> None:
    """Clone the repo if not present, and set up .claude/ directory."""
    repo_url = f"https://github.com/{repo}.git"
    if not repo_path.is_dir():
        log.info("Repo not found at %s, cloning %s...", repo_path, repo)
        result = subprocess.run(
            ["git", "clone", repo_url, str(repo_path)],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            log.error("Failed to clone %s:\n%s", repo, result.stderr.strip())
            raise RuntimeError(f"git clone failed for {repo}")
        log.info("Repo cloned to %s", repo_path)

    claude_dir = repo_path / ".claude"
    claude_dir.mkdir(exist_ok=True)

    # Remove stale agents symlink if present (no longer needed)
    agents_link = claude_dir / "agents"
    if agents_link.is_symlink():
        agents_link.unlink()
        log.info("Removed stale agents symlink at %s", agents_link)
    elif agents_link.is_dir():
        shutil.rmtree(str(agents_link))
        log.info("Removed stale agents directory at %s", agents_link)

    exclude_file = repo_path / ".git" / "info" / "exclude"
    exclude_content = exclude_file.read_text() if exclude_file.exists() else ""
    if ".claude/" not in exclude_content:
        with open(exclude_file, "a") as f:
            f.write(".claude/\n")
        log.info("Added .claude/ to .git/info/exclude")


ALLOWED_GIT_HOSTS = {"github.com"}


def validate_repo_remote(repo_path: Path, expected_repo: str) -> bool:
    """Verify the repo at repo_path has the expected remote origin."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            log.error("Failed to get remote URL: %s", result.stderr.strip())
            return False
        remote_url = result.stdout.strip()
        actual_host, actual_repo = _normalize_repo_from_url(remote_url)
        if actual_host not in ALLOWED_GIT_HOSTS:
            log.error(
                "Remote host '%s' not in allowed hosts %s (URL: '%s')",
                actual_host, ALLOWED_GIT_HOSTS, remote_url,
            )
            return False
        if actual_repo != expected_repo:
            log.error(
                "Remote origin mismatch: got '%s' (from URL '%s'), expected '%s'",
                actual_repo, remote_url, expected_repo,
            )
            return False
        return True
    except Exception as exc:
        log.error("Failed to validate remote: %s", exc)
        return False


def _normalize_repo_from_url(url: str) -> "tuple[str, str]":
    """Extract (host, owner/repo) from a git remote URL (https or ssh)."""
    url = url.strip()
    if url.startswith("git@"):
        host_part, _, path = url.partition(":")
        host = host_part.split("@", 1)[1] if "@" in host_part else host_part
        repo = path.rstrip("/").removesuffix(".git")
        return (host, repo)
    if url.startswith("ssh://"):
        url_no_scheme = url[len("ssh://"):]
        if "@" in url_no_scheme.split("/")[0]:
            host_and_path = url_no_scheme.split("@", 1)[1]
        else:
            host_and_path = url_no_scheme
        parts = host_and_path.rstrip("/").removesuffix(".git").split("/")
        host = parts[0] if parts else ""
        repo = "/".join(parts[1:]) if len(parts) >= 3 else ""
        return (host, repo)
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            url = url[len(scheme):]
            break
    parts = url.rstrip("/").removesuffix(".git").split("/")
    host = parts[0] if parts else ""
    repo = f"{parts[1]}/{parts[2]}" if len(parts) >= 3 else ""
    return (host, repo)


def clean_repo(repo_path: str, default_branch: str) -> None:
    """Reset the repo checkout to a clean state on the default branch."""
    log.info("Cleaning repo at %s (branch: %s)", repo_path, default_branch)
    commands = [
        ["git", "fetch", "origin", default_branch],
        ["git", "checkout", "-f", default_branch],
        ["git", "reset", "--hard", f"origin/{default_branch}"],
        ["git", "clean", "-fd", "-e", ".claude"],
    ]
    for cmd in commands:
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            log.error("Command %s failed:\n%s", cmd, result.stderr.strip())
            raise RuntimeError(f"Repo cleanup failed: {' '.join(cmd)}")
    log.info("Repo clean on %s", default_branch)


def setup_directories(memories_dir: Path, repo: str) -> None:
    """Create runtime directories and seed knowledge files."""
    repo_slug = repo.replace("/", "-")
    knowledge_dir = memories_dir / "knowledge" / repo_slug
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    log.info("Knowledge dir: %s", knowledge_dir)

    for filename in KNOWLEDGE_FILES:
        filepath = knowledge_dir / filename
        if not filepath.is_file():
            filepath.touch()
            log.info("Seeded %s", filepath)


def load_repos_config(config_path: Path) -> dict:
    """Load and validate repos.json. Returns dict keyed by 'owner/repo'."""
    if not config_path.is_file():
        raise FileNotFoundError(f"repos.json not found at {config_path}")
    with open(config_path) as f:
        config = json.load(f)
    if not isinstance(config, dict) or not config:
        raise ValueError("repos.json must be a non-empty JSON object")
    seen_paths: dict[str, str] = {}
    for repo, settings in config.items():
        if not isinstance(settings, dict):
            raise ValueError(f"Repo '{repo}' settings must be a JSON object")
        if "/" not in repo or repo.count("/") != 1:
            raise ValueError(f"Invalid repo key '{repo}' — must be 'owner/repo'")
        if "checkout_path" not in settings:
            raise ValueError(f"Repo '{repo}' missing required 'checkout_path'")
        # Resolve path
        resolved = str(Path(settings["checkout_path"]).expanduser().resolve())
        settings["checkout_path"] = resolved
        # Check for overlapping checkout paths
        for other_repo, other_path in seen_paths.items():
            if resolved == other_path:
                raise ValueError(f"Repos '{repo}' and '{other_repo}' share checkout_path '{resolved}'")
            if resolved.startswith(other_path + "/") or other_path.startswith(resolved + "/"):
                raise ValueError(f"Repos '{repo}' and '{other_repo}' have overlapping checkout paths")
        seen_paths[repo] = resolved
        settings.setdefault("screenshots", False)
    return config


def detect_default_branch(repo: str) -> str:
    """Query GitHub API for the repo's default branch. Raises on failure."""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}", "--jq", ".default_branch"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            f"Failed to detect default branch for {repo}: {result.stderr.strip()}"
        )
    branch = result.stdout.strip()
    log.info("Default branch for %s: %s", repo, branch)
    return branch


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a subprocess and its entire process group."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return

    if pgid == os.getpgrp():
        proc.kill()
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning("Process group %d did not exit after SIGTERM, sending SIGKILL", pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass
