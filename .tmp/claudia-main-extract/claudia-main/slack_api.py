"""In-process Slack posting with structured error classification.

The returned dict has a `result` field:
    - "ok"                 → {"result":"ok","ts":"<slack ts>"}
    - "definite_failure"   → {"result":"definite_failure","error":"<reason>"}
    - "ambiguous_failure"  → {"result":"ambiguous_failure","error":"<reason>"}

Classification follows observable uncertainty: default to ambiguous_failure
whenever we cannot rule out that Slack accepted the message. Do NOT auto-
retry on ambiguous failure.

Config/precondition failures (missing token, empty channel) also return a
structured result — they never raise — so callers have a single failure
surface to reason about.
"""
import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

_SLACK_POST_URL = "https://slack.com/api/chat.postMessage"
_SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"

# Slack `ok:false` errors where the server has definitively rejected the
# request and no message was delivered. Anything NOT in this set is treated
# as definite anyway (ok:false is itself a contract: no message sent), but
# we keep the list for documentation and future expansion.
_DEFINITE_HTTP_CODES = frozenset({400, 401, 403, 404, 429})


def _urlopen(req, timeout):
    # Wrapped for monkeypatching in tests.
    return urllib.request.urlopen(req, timeout=timeout)


def slack_auth_test(*, timeout: float = 10.0) -> dict[str, Any]:
    """Call Slack's `auth.test` in-process to resolve the bot user id.

    Never shells out — keeps `SLACK_BOT_TOKEN` off the process command line
    (where it would be visible via `ps` to any user on the host). Returns
    one of:
        {"result": "ok", "user_id": "<id>"}
        {"result": "definite_failure", "error": "<reason>"}
        {"result": "ambiguous_failure", "error": "<reason>"}
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return {"result": "definite_failure", "error": "config:no_token"}
    req = urllib.request.Request(
        _SLACK_AUTH_TEST_URL,
        data=b"",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        resp = _urlopen(req, timeout=timeout)
    except (socket.timeout, ConnectionResetError) as e:
        return {"result": "ambiguous_failure", "error": str(e) or type(e).__name__}
    except (ConnectionRefusedError, socket.gaierror) as e:
        return {"result": "definite_failure", "error": str(e) or type(e).__name__}
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            return {"result": "ambiguous_failure", "error": f"http_{e.code}"}
        return {"result": "definite_failure", "error": f"http_{e.code}"}
    except urllib.error.URLError as e:
        return {"result": "ambiguous_failure", "error": f"urlerror: {e.reason}"}
    with resp:
        try:
            payload = json.loads(resp.read().decode() or "{}")
        except Exception as e:
            return {"result": "ambiguous_failure", "error": f"unparseable: {e}"}
    if payload.get("ok"):
        return {"result": "ok", "user_id": payload.get("user_id", "")}
    return {"result": "definite_failure", "error": payload.get("error", "ok_false")}


def slack_post(text: str, channel: str, *, timeout: float = 10.0) -> dict[str, Any]:
    if not isinstance(channel, str) or not channel:
        return {"result": "definite_failure", "error": "config:empty_channel"}
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return {"result": "definite_failure", "error": "config:no_token"}

    body = json.dumps({
        "channel": channel,
        "text": text,
        "unfurl_links": False,
    }).encode()
    req = urllib.request.Request(
        _SLACK_POST_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )

    try:
        resp = _urlopen(req, timeout=timeout)
    except socket.timeout as e:
        return {"result": "ambiguous_failure", "error": f"socket timeout: {e}"}
    except ConnectionResetError as e:
        return {"result": "ambiguous_failure", "error": f"connection reset: {e}"}
    except ConnectionRefusedError as e:
        return {"result": "definite_failure", "error": f"connection refused: {e}"}
    except socket.gaierror as e:
        return {"result": "definite_failure", "error": f"dns failure: {e}"}
    except urllib.error.HTTPError as e:
        # HTTPError: Slack received the request. For 5xx, we cannot tell
        # whether the message was applied — that is ambiguous by definition.
        # For definite-rejection codes (400/401/403/404), parse the body if
        # possible to extract Slack's own error label.
        try:
            payload = json.loads(e.read().decode() or "{}")
            err_label = payload.get("error") or f"http_{e.code}"
        except Exception:
            err_label = f"http_{e.code}"
        if e.code >= 500:
            return {"result": "ambiguous_failure", "error": err_label}
        if e.code in _DEFINITE_HTTP_CODES:
            return {"result": "definite_failure", "error": err_label}
        # Unknown 4xx we haven't explicitly classified → ambiguous (safer).
        return {"result": "ambiguous_failure", "error": err_label}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", "")
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            return {"result": "definite_failure", "error": f"urlerror: {reason}"}
        if isinstance(reason, socket.timeout):
            return {"result": "ambiguous_failure", "error": f"urlerror: {reason}"}
        return {"result": "ambiguous_failure", "error": f"urlerror: {reason}"}

    with resp:
        try:
            raw = resp.read()
            payload = json.loads(raw.decode())
        except Exception as e:
            return {"result": "ambiguous_failure", "error": f"unparseable response: {e}"}

        status = getattr(resp, "status", 200)
        if status >= 500:
            # 5xx reached us inside a normal response — Slack might have
            # accepted the write regardless. Ambiguous.
            return {"result": "ambiguous_failure", "error": payload.get("error") or f"http_{status}"}
        if status >= 400:
            return {"result": "definite_failure", "error": payload.get("error") or f"http_{status}"}

        if not payload.get("ok"):
            # ok:false is Slack telling us the write was refused. Definite.
            return {"result": "definite_failure", "error": payload.get("error") or "ok_false"}

        ts = payload.get("ts", "")
        return {"result": "ok", "ts": ts}
