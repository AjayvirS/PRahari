#!/usr/bin/env python3
"""Send a message to Slack. Usage: python3 slack.py 'your message here'"""
import json, os, subprocess, sys
from pathlib import Path

# Load .env from the same directory as this script (fallback if env vars aren't inherited)
_env_file = Path(__file__).resolve().parent / ".env"
if _env_file.is_file():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip().strip("\"'")
            if _k and _k not in os.environ:
                os.environ[_k] = _v

token = os.environ.get("SLACK_BOT_TOKEN", "")
channel = os.environ.get("SLACK_CHANNEL", "")
if not token or not channel:
    print("SLACK_BOT_TOKEN or SLACK_CHANNEL not set", file=sys.stderr)
    sys.exit(1)

message = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
if not message.strip():
    print("No message provided", file=sys.stderr)
    sys.exit(1)

payload = json.dumps({"channel": channel, "text": message, "unfurl_links": False})
result = subprocess.run(
    ["curl", "-s", "-X", "POST", "https://slack.com/api/chat.postMessage",
     "-H", f"Authorization: Bearer {token}",
     "-H", "Content-Type: application/json",
     "-d", payload],
    capture_output=True, text=True, timeout=10,
)
resp = json.loads(result.stdout)
if not resp.get("ok"):
    print(f"Slack error: {resp.get('error')}", file=sys.stderr)
    sys.exit(1)
