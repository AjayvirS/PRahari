#!/usr/bin/env python3
"""Query Claude Code usage limits and parse run consumption data.

Subcommands:
    quota   Query remaining rate limits (5h session, weekly, model-specific).
            Reads OAuth token from macOS keychain or CLAUDE_OAUTH_TOKEN env var.

    parse   Parse --output-format json/stream-json output and extract usage.
            Reads from stdin or a file argument.

Usage:
    python3 claude-usage.py quota
    claude -p "..." --output-format json | python3 claude-usage.py parse
    python3 claude-usage.py parse output.json
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone


# ── quota subcommand ──────────────────────────────────────────────────────────

USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"


def _get_oauth_token() -> str:
    """Get OAuth access token from env var, or macOS keychain."""
    token = os.environ.get("CLAUDE_OAUTH_TOKEN", "")
    if token:
        return token

    # Try macOS keychain
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            creds = json.loads(result.stdout.strip())
            return creds["claudeAiOauth"]["accessToken"]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
        pass

    # Try Linux: credentials stored in a dotfile
    cred_file = os.path.expanduser("~/.claude/.credentials.json")
    if os.path.isfile(cred_file):
        try:
            with open(cred_file) as f:
                creds = json.load(f)
            return creds["claudeAiOauth"]["accessToken"]
        except (json.JSONDecodeError, KeyError):
            pass

    return ""


def query_quota() -> dict:
    """Query the Anthropic OAuth usage endpoint for rate limit data."""
    token = _get_oauth_token()
    if not token:
        print("No OAuth token found. Set CLAUDE_OAUTH_TOKEN or log in via `claude`.",
              file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(
        ["curl", "-s", USAGE_ENDPOINT,
         "-H", f"Authorization: Bearer {token}",
         "-H", "anthropic-beta: oauth-2025-04-20",
         "-H", "Accept: application/json"],
        capture_output=True, text=True, timeout=10,
    )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Failed to parse response: {result.stdout[:200]}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    out = {}

    for key, label in [
        ("five_hour", "session"),
        ("seven_day", "weekly"),
        ("seven_day_opus", "weekly_opus"),
        ("seven_day_sonnet", "weekly_sonnet"),
    ]:
        window = data.get(key)
        if not window or window.get("utilization") is None:
            continue

        used_pct = window["utilization"]
        remaining_pct = round(100.0 - used_pct, 1)
        resets_at = window.get("resets_at")
        resets_in = None
        if resets_at:
            try:
                reset_dt = datetime.fromisoformat(resets_at)
                delta = reset_dt - now
                secs = max(0, int(delta.total_seconds()))
                if secs >= 86400:
                    resets_in = f"{secs // 86400}d {(secs % 86400) // 3600}h"
                elif secs >= 3600:
                    resets_in = f"{secs // 3600}h {(secs % 3600) // 60}m"
                else:
                    resets_in = f"{secs // 60}m"
            except (ValueError, TypeError):
                pass

        out[label] = {
            "used_pct": used_pct,
            "remaining_pct": remaining_pct,
            "resets_at": resets_at,
            "resets_in": resets_in,
        }

    extra = data.get("extra_usage", {})
    if extra.get("is_enabled"):
        out["extra_credits"] = {
            "monthly_limit_usd": (extra.get("monthly_limit") or 0) / 100,
            "used_usd": (extra.get("used_credits") or 0) / 100,
            "utilization_pct": extra.get("utilization"),
        }

    return out


# ── parse subcommand ──────────────────────────────────────────────────────────

def parse_run_output(text: str) -> dict:
    """Parse Claude Code json/stream-json output and extract usage summary."""
    result = None
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("type") == "result" or "total_cost_usd" in obj:
                result = obj
        except json.JSONDecodeError:
            continue

    if result is None:
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            pass

    if result is None:
        print("No result found in input", file=sys.stderr)
        sys.exit(1)

    model_usage = result.get("modelUsage", {})
    models = {}
    for model, d in model_usage.items():
        models[model] = {
            "input_tokens": d.get("inputTokens", 0),
            "output_tokens": d.get("outputTokens", 0),
            "cache_read_tokens": d.get("cacheReadInputTokens", 0),
            "cache_creation_tokens": d.get("cacheCreationInputTokens", 0),
            "cost_usd": d.get("costUSD", 0),
        }

    return {
        "cost_usd": result.get("total_cost_usd", 0),
        "duration_ms": result.get("duration_ms", 0),
        "duration_api_ms": result.get("duration_api_ms", 0),
        "num_turns": result.get("num_turns", 0),
        "session_id": result.get("session_id", ""),
        "input_tokens": sum(m["input_tokens"] for m in models.values()),
        "output_tokens": sum(m["output_tokens"] for m in models.values()),
        "cache_read_tokens": sum(m["cache_read_tokens"] for m in models.values()),
        "cache_creation_tokens": sum(m["cache_creation_tokens"] for m in models.values()),
        "models": models,
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "quota":
        print(json.dumps(query_quota(), indent=2))

    elif cmd == "parse":
        if len(sys.argv) > 2 and sys.argv[2] != "-":
            with open(sys.argv[2]) as f:
                text = f.read()
        else:
            text = sys.stdin.read()
        print(json.dumps(parse_run_output(text), indent=2))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Usage: claude-usage.py [quota|parse]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
