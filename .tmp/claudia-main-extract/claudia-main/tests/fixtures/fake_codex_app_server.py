#!/usr/bin/env python3
"""Fake `codex -s read-only -a untrusted app-server` for tests.

Reads JSON-RPC requests from stdin line-by-line and emits responses on stdout.
Behavior controlled by `FAKE_CODEX_MODE` env var:
  success                 — well-formed initialize + rateLimits/read
  notifications_interleaved — emit a status/changed notification between calls
  auth_error              — return an error response to rateLimits/read
  malformed_response      — emit a non-JSON line for the rateLimits/read response
  hang                    — read forever, never answer rateLimits/read
"""
from __future__ import annotations

import json
import os
import sys
import time


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    mode = os.environ.get("FAKE_CODEX_MODE", "success")
    # codex-cli 0.133.0 shape: windows live under `rateLimits` (with
    # per-limit-id breakdowns under `rateLimitsByLimitId` we don't read).
    rate_limits = {
        "rateLimits": {
            "limitId": "codex",
            "limitName": None,
            "primary": {"usedPercent": 12.5, "resetsAt": 1735000000, "windowDurationMins": 300},
            "secondary": {"usedPercent": 60.0, "resetsAt": 1735604800, "windowDurationMins": 10080},
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "planType": "prolite",
            "rateLimitReachedType": None,
        },
        "rateLimitsByLimitId": {},
    }
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        req_id = req.get("id")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": {}}})
            if mode == "notifications_interleaved":
                _send({"jsonrpc": "2.0", "method": "remoteControl/status/changed",
                       "params": {"status": "idle"}})
        elif method == "initialized":
            # initialized is a notification — no response.
            pass
        elif method == "account/rateLimits/read":
            if mode == "hang":
                while True:
                    time.sleep(60)
            if mode == "auth_error":
                _send({"jsonrpc": "2.0", "id": req_id,
                       "error": {"code": -32001, "message": "not authenticated"}})
            elif mode == "malformed_response":
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
            else:
                _send({"jsonrpc": "2.0", "id": req_id, "result": rate_limits})
        else:
            _send({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": -32601, "message": "unknown method"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
