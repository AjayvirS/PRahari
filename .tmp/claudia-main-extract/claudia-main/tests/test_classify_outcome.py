"""Unit tests for the backend-agnostic classify_outcome."""
from backends.base import ParsedRun
from worker import classify_outcome


def _parsed(*, deltas=None, malformed=None, tool_use=False, unexpected=None):
    return ParsedRun(
        tokens_in=None, tokens_out=None, cached_in=None,
        model_used=None, cost_usd=None,
        state_deltas=deltas or [],
        malformed_state_deltas=malformed or [],
        has_tool_use=tool_use,
        unexpected_events=unexpected or [],
    )


def test_runner_failure_precedence_minus_two():
    # exit -2 + everything else green → still transient_failure.
    p = _parsed(deltas=[{"a": 1}], tool_use=True, unexpected=["mcp_tool_call"])
    outcome, delta = classify_outcome(-2, p)
    assert outcome == "transient_failure"
    assert delta is None


def test_exit_zero_with_delta_success():
    p = _parsed(deltas=[{"a": 1}])
    outcome, delta = classify_outcome(0, p)
    assert outcome == "success"
    assert delta == {"a": 1}


def test_exit_zero_no_delta_claude_success():
    p = _parsed()
    outcome, delta = classify_outcome(0, p, require_delta_on_success=False)
    assert outcome == "success"
    assert delta is None


def test_exit_zero_no_delta_codex_ambiguous():
    p = _parsed()
    outcome, delta = classify_outcome(0, p, require_delta_on_success=True)
    assert outcome == "ambiguous"
    assert delta is None


def test_nonzero_with_tool_use_and_delta_success():
    p = _parsed(deltas=[{"a": 1}], tool_use=True)
    outcome, delta = classify_outcome(1, p)
    assert outcome == "success"
    assert delta == {"a": 1}


def test_nonzero_with_tool_use_no_delta_ambiguous():
    p = _parsed(tool_use=True)
    outcome, delta = classify_outcome(1, p)
    assert outcome == "ambiguous"
    assert delta is None


def test_nonzero_no_tool_use_transient():
    p = _parsed()
    outcome, delta = classify_outcome(1, p)
    assert outcome == "transient_failure"
    assert delta is None


def test_unexpected_events_force_ambiguous_when_not_minus_two():
    p = _parsed(deltas=[{"a": 1}], unexpected=["web_search_request"])
    outcome, delta = classify_outcome(0, p)
    assert outcome == "ambiguous"
    assert delta == {"a": 1}


def test_codex_malformed_delta_alongside_valid_one_is_ambiguous():
    """Output discipline: a malformed fenced block + a valid one is NOT
    success under codex. Otherwise an agent that emits a partial garbage
    block plus any well-formed JSON fence passes the gate."""
    p = _parsed(deltas=[{"a": 1}], malformed=["{\"type\":"])
    outcome, delta = classify_outcome(0, p, require_delta_on_success=True)
    assert outcome == "ambiguous"
    # last valid delta is still returned so the worker can record it
    assert delta == {"a": 1}


def test_codex_multiple_valid_deltas_is_ambiguous():
    """Output discipline: more than one valid state_delta means the
    agent emitted multiple fenced blocks, violating the single-block
    contract. Treat as ambiguous so the run is reviewed, not silently
    accepted with whichever block happens to be last."""
    p = _parsed(deltas=[{"a": 1}, {"b": 2}])
    outcome, delta = classify_outcome(0, p, require_delta_on_success=True)
    assert outcome == "ambiguous"
    assert delta == {"b": 2}


def test_claude_tolerates_malformed_and_multi_deltas():
    """Claude has tighter wrappers via the SDK; we don't enforce the
    single-block gate when require_delta_on_success is False so existing
    Claude behavior is unchanged."""
    p = _parsed(deltas=[{"a": 1}, {"b": 2}], malformed=["partial"])
    outcome, delta = classify_outcome(0, p, require_delta_on_success=False)
    assert outcome == "success"
    assert delta == {"b": 2}
