"""run_inline_agent must NEVER raise; always returns a dict."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import inline_agents


def _write_agent(tmp_path: Path, name: str, fm: str, body: str) -> Path:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(exist_ok=True)
    p = agents_dir / f"{name}.md"
    p.write_text(f"---\n{fm}\n---\n{body}\n")
    return p


def test_missing_codex_field_returns_prompt_build_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(inline_agents, "SCRIPT_DIR", tmp_path)
    _write_agent(tmp_path, "x", "name: x\nmodel: opus\ncodex_model: gpt-5.5", "body")
    monkeypatch.setenv("CLAUDIA_BACKEND", "codex")
    result = inline_agents.run_inline_agent("x", {}, expected_type="ok")
    assert result == {"result": "agent_failure", "reason": "prompt_build_failed:codex_effort"}


def test_missing_agent_file_returns_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(inline_agents, "SCRIPT_DIR", tmp_path)
    (tmp_path / "agents").mkdir()
    result = inline_agents.run_inline_agent("does-not-exist", {}, expected_type="ok")
    assert result == {"result": "agent_failure", "reason": "agent_file_missing"}


def test_unresolved_placeholder_returns_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(inline_agents, "SCRIPT_DIR", tmp_path)
    _write_agent(
        tmp_path, "y",
        "name: y\nmodel: opus\ncodex_model: gpt-5.5\ncodex_effort: xhigh",
        "Hello {{MISSING_VAR}}",
    )
    monkeypatch.setenv("CLAUDIA_BACKEND", "codex")
    result = inline_agents.run_inline_agent("y", {}, expected_type="ok")
    assert result["result"] == "agent_failure"
    assert result["reason"].startswith("unresolved:")


def test_unreadable_agent_file_returns_unhandled(tmp_path, monkeypatch):
    """Agent file exists but read_text raises (e.g., permission error).
    Must NOT propagate; contract demands a dict result."""
    monkeypatch.setattr(inline_agents, "SCRIPT_DIR", tmp_path)
    agent = _write_agent(
        tmp_path, "z",
        "name: z\nmodel: opus\ncodex_model: gpt-5.5\ncodex_effort: xhigh",
        "body",
    )
    monkeypatch.setenv("CLAUDIA_BACKEND", "codex")

    def _boom(*a, **kw):
        raise PermissionError("denied")

    with patch.object(type(agent), "read_text", _boom):
        result = inline_agents.run_inline_agent("z", {}, expected_type="ok")
    assert result["result"] == "agent_failure"
    assert result["reason"] == "unhandled:PermissionError"


def test_unknown_backend_returns_unhandled(tmp_path, monkeypatch):
    """Lookup of a non-existent backend raises ValueError inside
    _get_backend; must be caught and reported as a failure dict."""
    monkeypatch.setattr(inline_agents, "SCRIPT_DIR", tmp_path)
    _write_agent(
        tmp_path, "w",
        "name: w\nmodel: opus\ncodex_model: gpt-5.5\ncodex_effort: xhigh",
        "body",
    )
    monkeypatch.setenv("CLAUDIA_BACKEND", "no-such-backend")
    result = inline_agents.run_inline_agent("w", {}, expected_type="ok")
    assert result["result"] == "agent_failure"
    # Reason format is "unhandled:<ExceptionClassName>"; exact class is
    # ValueError from get_backend, but allow either as long as it's
    # tagged unhandled.
    assert result["reason"].startswith("unhandled:")
