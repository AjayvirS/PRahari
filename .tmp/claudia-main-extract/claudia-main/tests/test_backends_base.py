"""Sanity tests for backends.base dataclasses."""
from backends.base import ParsedRun, RunContext, RunResult


def test_runcontext_round_trip():
    ctx = RunContext(
        prompt_path="/tmp/x.md", cwd="/repo", model="opus", effort_or_turns=1000,
    )
    assert ctx.prompt_path == "/tmp/x.md"
    assert ctx.cwd == "/repo"
    assert ctx.model == "opus"
    assert ctx.effort_or_turns == 1000


def test_runcontext_codex_effort_is_str():
    ctx = RunContext(
        prompt_path="/tmp/x.md", cwd="/repo", model="gpt-5.5", effort_or_turns="xhigh",
    )
    assert ctx.effort_or_turns == "xhigh"


def test_parsedrun_state_delta_returns_last():
    parsed = ParsedRun(
        tokens_in=10, tokens_out=20, cached_in=0, model_used="opus", cost_usd=0.5,
        state_deltas=[{"a": 1}, {"a": 2}],
        malformed_state_deltas=[],
        has_tool_use=True, unexpected_events=[],
    )
    assert parsed.state_delta == {"a": 2}


def test_parsedrun_state_delta_none_when_empty():
    parsed = ParsedRun(
        tokens_in=None, tokens_out=None, cached_in=None, model_used=None, cost_usd=None,
        state_deltas=[], malformed_state_deltas=[],
        has_tool_use=False, unexpected_events=[],
    )
    assert parsed.state_delta is None


def test_runresult_carries_ctx():
    ctx = RunContext(prompt_path="/tmp/x", cwd="/r", model="m", effort_or_turns=None)
    r = RunResult(exit_code=0, ctx=ctx)
    assert r.exit_code == 0
    assert r.ctx is ctx
