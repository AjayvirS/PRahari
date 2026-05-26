"""Unit tests for CodexBackend.parse_output."""
from pathlib import Path

from backends.base import RunContext
from backends.codex import CodexBackend

FIX = Path(__file__).parent / "fixtures" / "codex_jsonl"


def _ctx(model="gpt-5.5"):
    return RunContext(prompt_path="/tmp/x", cwd="/r", model=model, effort_or_turns="xhigh")


def test_success_fixture(tmp_path):
    b = CodexBackend(prices_path=tmp_path / "prices.json")  # missing → no cost
    p = b.parse_output(_ctx(), str(FIX / "success.jsonl"))
    assert p.state_delta == {"type": "review", "message": "ok"}
    assert p.has_tool_use is True
    # tokens_in = input - cached
    assert p.tokens_in == 54809 - 39040
    assert p.cached_in == 39040
    assert p.tokens_out == 238  # reasoning_output_tokens NOT added
    assert p.model_used == "gpt-5.5"
    assert p.unexpected_events == []
    assert p.cost_usd is None  # empty price table


def test_no_delta_fixture(tmp_path):
    b = CodexBackend(prices_path=tmp_path / "prices.json")
    p = b.parse_output(_ctx(), str(FIX / "no_delta.jsonl"))
    assert p.state_delta is None
    assert p.has_tool_use is False  # only agent_message, no command/file_change
    assert p.tokens_in == 100  # no cached portion


def test_unexpected_event_recorded(tmp_path):
    b = CodexBackend(prices_path=tmp_path / "prices.json")
    p = b.parse_output(_ctx(), str(FIX / "unexpected.jsonl"))
    assert p.unexpected_events == ["web_search_request"]
    assert p.state_delta == {"type": "ok"}  # parse still works


def test_todo_list_is_ignored_not_unexpected(tmp_path):
    """codex's internal `todo_list` item is a normal model feature, not
    capability drift — must not land in `unexpected_events` (would cause
    false `ambiguous` classification and unnecessary retries)."""
    b = CodexBackend(prices_path=tmp_path / "prices.json")
    p = b.parse_output(_ctx(), str(FIX / "todo_list_ignored.jsonl"))
    assert p.unexpected_events == []
    assert p.has_tool_use is True  # command_execution counted as tool use
    assert p.state_delta == {"type": "hygiene", "fixed": True}


def test_error_item_is_ignored_not_unexpected(tmp_path):
    """codex emits item.completed with type=error when a sub-command fails
    (observed in production review job 12296 — 11 errors in one second
    during exploration, but the review still completed cleanly). The
    state_delta is authoritative; sub-command errors are not capability
    drift and must not push the outcome to ambiguous."""
    b = CodexBackend(prices_path=tmp_path / "prices.json")
    p = b.parse_output(_ctx(), str(FIX / "error_item_ignored.jsonl"))
    assert p.unexpected_events == []
    assert p.has_tool_use is True
    assert p.state_delta == {"type": "review", "status": "reviewed"}


def test_invariant_violation_nulls_tokens_and_cost(tmp_path):
    b = CodexBackend(prices_path=tmp_path / "prices.json")
    p = b.parse_output(_ctx(), str(FIX / "invariant_violation.jsonl"))
    assert p.tokens_in is None
    assert p.cost_usd is None
    # cached_in still reported (informational)
    assert p.cached_in == 200


def test_malformed_delta_preserved_separately(tmp_path):
    b = CodexBackend(prices_path=tmp_path / "prices.json")
    p = b.parse_output(_ctx(), str(FIX / "malformed_delta.jsonl"))
    assert len(p.state_deltas) == 1
    assert p.state_deltas[0] == {"type": "ok"}
    assert len(p.malformed_state_deltas) == 1


def test_empty_file_defaults(tmp_path):
    b = CodexBackend(prices_path=tmp_path / "prices.json")
    p = b.parse_output(_ctx(), str(FIX / "empty.jsonl"))
    assert p.tokens_in is None
    assert p.cost_usd is None
    assert p.state_deltas == []
    assert p.unexpected_events == []


def test_missing_file_defaults(tmp_path):
    b = CodexBackend(prices_path=tmp_path / "prices.json")
    p = b.parse_output(_ctx(), str(tmp_path / "does-not-exist.jsonl"))
    assert p.tokens_in is None
    assert p.state_deltas == []
