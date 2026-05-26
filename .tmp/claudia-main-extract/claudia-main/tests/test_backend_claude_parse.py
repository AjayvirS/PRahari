"""Unit tests for ClaudeBackend.parse_output."""
from pathlib import Path

from backends.base import RunContext
from backends.claude import ClaudeBackend

FIX = Path(__file__).parent / "fixtures" / "claude_jsonl"


def _ctx(model="opus"):
    return RunContext(prompt_path="/tmp/x", cwd="/r", model=model, effort_or_turns=None)


def test_success_fixture_extracts_delta_tokens_cost():
    b = ClaudeBackend()
    p = b.parse_output(_ctx(), str(FIX / "success.jsonl"))
    assert p.state_delta == {"type": "review", "message": "ok"}
    assert p.has_tool_use is True
    assert p.tokens_in == 1000 + 200
    assert p.cached_in == 50
    assert p.tokens_out == 300
    assert p.cost_usd == 0.0123
    assert p.model_used == "opus"
    assert p.unexpected_events == []
    assert p.malformed_state_deltas == []


def test_no_delta_fixture():
    b = ClaudeBackend()
    p = b.parse_output(_ctx(), str(FIX / "no_delta.jsonl"))
    assert p.state_delta is None
    assert p.has_tool_use is False
    assert p.tokens_in == 10


def test_malformed_middle_preserves_parseable():
    b = ClaudeBackend()
    p = b.parse_output(_ctx(), str(FIX / "malformed_middle.jsonl"))
    assert len(p.state_deltas) == 1
    assert p.state_deltas[0] == {"type": "ok"}
    assert len(p.malformed_state_deltas) == 1
    assert p.tokens_in is None


def test_two_deltas_one_block_in_order():
    b = ClaudeBackend()
    p = b.parse_output(_ctx(), str(FIX / "two_deltas_one_block.jsonl"))
    assert [d["a"] for d in p.state_deltas] == [1, 2]
    assert p.state_delta == {"a": 2}


def test_empty_file_returns_defaults():
    b = ClaudeBackend()
    p = b.parse_output(_ctx(), str(FIX / "empty.jsonl"))
    assert p.state_deltas == []
    assert p.has_tool_use is False
    assert p.tokens_in is None
    assert p.tokens_out is None
    assert p.cost_usd is None


def test_missing_file_returns_defaults(tmp_path):
    b = ClaudeBackend()
    p = b.parse_output(_ctx(), str(tmp_path / "does-not-exist.jsonl"))
    assert p.state_deltas == []
    assert p.tokens_in is None
