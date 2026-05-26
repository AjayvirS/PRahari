"""Unit tests for slack_api.slack_post classification."""
from unittest.mock import patch, MagicMock
import io
import json
import socket
import urllib.error

import pytest

import slack_api


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")


def _mock_response(status: int, body: dict):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
    return resp


def test_ok_response():
    with patch("slack_api._urlopen", return_value=_mock_response(200, {"ok": True, "ts": "1.2"})):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r == {"result": "ok", "ts": "1.2"}


def test_slack_ok_false_is_definite_failure():
    with patch("slack_api._urlopen", return_value=_mock_response(200, {"ok": False, "error": "channel_not_found"})):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r == {"result": "definite_failure", "error": "channel_not_found"}


def test_http_401_is_definite_failure():
    """urllib.request.urlopen raises HTTPError for 4xx, not a status=401
    response — we must mock that raise path accurately."""
    err = urllib.error.HTTPError(
        url="https://slack.com/api/chat.postMessage",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(json.dumps({"ok": False, "error": "invalid_auth"}).encode()),
    )
    with patch("slack_api._urlopen", side_effect=err):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "definite_failure"
    assert r["error"] == "invalid_auth"


def test_dns_failure_is_definite_failure():
    with patch("slack_api._urlopen", side_effect=socket.gaierror("name resolution failed")):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "definite_failure"
    assert "name resolution" in r["error"].lower() or "gaierror" in r["error"].lower()


def test_connection_refused_is_definite_failure():
    with patch("slack_api._urlopen", side_effect=ConnectionRefusedError("refused")):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "definite_failure"


def test_socket_timeout_is_ambiguous_failure():
    with patch("slack_api._urlopen", side_effect=socket.timeout("read timed out")):
        r = slack_api.slack_post("hi", "C012NFRM76F", timeout=1.0)
    assert r["result"] == "ambiguous_failure"


def test_connection_reset_mid_request_is_ambiguous():
    with patch("slack_api._urlopen", side_effect=ConnectionResetError("reset by peer")):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "ambiguous_failure"


def test_unparseable_body_after_200_is_ambiguous():
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = b"not json"
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
    with patch("slack_api._urlopen", return_value=resp):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "ambiguous_failure"


def test_http_500_is_ambiguous_failure():
    """5xx means Slack received the request; whether it applied the write
    is uncertain, so the observable-uncertainty rule says ambiguous."""
    err = urllib.error.HTTPError(
        url="https://slack.com/api/chat.postMessage",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=io.BytesIO(b""),
    )
    with patch("slack_api._urlopen", side_effect=err):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "ambiguous_failure"
    assert r["error"] == "http_500"


def test_http_503_with_body_is_ambiguous_failure():
    err = urllib.error.HTTPError(
        url="https://slack.com/api/chat.postMessage",
        code=503,
        msg="Service Unavailable",
        hdrs={},
        fp=io.BytesIO(json.dumps({"error": "service_unavailable"}).encode()),
    )
    with patch("slack_api._urlopen", side_effect=err):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "ambiguous_failure"
    assert r["error"] == "service_unavailable"


def test_http_429_is_definite_failure():
    """429 means Slack definitively rejected the request (rate limited) —
    the message was NOT delivered. Classifying as ambiguous would strand
    the row in `posting` state; definite releases the slot for later cycles."""
    err = urllib.error.HTTPError(
        url="https://slack.com/api/chat.postMessage",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=io.BytesIO(json.dumps({"error": "ratelimited"}).encode()),
    )
    with patch("slack_api._urlopen", side_effect=err):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "definite_failure"
    assert r["error"] == "ratelimited"


def test_http_403_is_definite_failure():
    err = urllib.error.HTTPError(
        url="https://slack.com/api/chat.postMessage",
        code=403,
        msg="Forbidden",
        hdrs={},
        fp=io.BytesIO(json.dumps({"error": "missing_scope"}).encode()),
    )
    with patch("slack_api._urlopen", side_effect=err):
        r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "definite_failure"
    assert r["error"] == "missing_scope"


def test_missing_token_returns_definite_failure():
    import os
    os.environ.pop("SLACK_BOT_TOKEN", None)
    r = slack_api.slack_post("hi", "C012NFRM76F")
    assert r["result"] == "definite_failure"
    assert r["error"] == "config:no_token"


def test_empty_channel_returns_definite_failure():
    r = slack_api.slack_post("hi", "")
    assert r["result"] == "definite_failure"
    assert r["error"] == "config:empty_channel"


# ── slack_auth_test ────────────────────────────────────────────────────

def test_auth_test_ok():
    with patch("slack_api._urlopen",
               return_value=_mock_response(200, {"ok": True, "user_id": "U123"})):
        r = slack_api.slack_auth_test()
    assert r == {"result": "ok", "user_id": "U123"}


def test_auth_test_ok_false_is_definite_failure():
    with patch("slack_api._urlopen",
               return_value=_mock_response(200, {"ok": False, "error": "invalid_auth"})):
        r = slack_api.slack_auth_test()
    assert r["result"] == "definite_failure"
    assert r["error"] == "invalid_auth"


def test_auth_test_missing_token_returns_definite_failure():
    import os
    os.environ.pop("SLACK_BOT_TOKEN", None)
    r = slack_api.slack_auth_test()
    assert r["result"] == "definite_failure"
    assert r["error"] == "config:no_token"


def test_auth_test_5xx_is_ambiguous():
    err = urllib.error.HTTPError(
        url="https://slack.com/api/auth.test",
        code=503, msg="x", hdrs={},
        fp=io.BytesIO(b""),
    )
    with patch("slack_api._urlopen", side_effect=err):
        r = slack_api.slack_auth_test()
    assert r["result"] == "ambiguous_failure"


def test_auth_test_never_shells_out(monkeypatch):
    """Regression guard: the implementation must not invoke subprocess —
    that was the old design that leaked the bot token via argv."""
    import subprocess

    def boom(*args, **kwargs):
        raise AssertionError("slack_auth_test must NOT shell out")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    # Use a mocked _urlopen so we don't actually hit the network.
    with patch("slack_api._urlopen",
               return_value=_mock_response(200, {"ok": True, "user_id": "U1"})):
        r = slack_api.slack_auth_test()
    assert r["result"] == "ok"
