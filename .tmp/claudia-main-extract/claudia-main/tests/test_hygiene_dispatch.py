"""Hygiene path integration: validate_agents gate, ambiguous counting,
PromptBuildError propagation."""
import json
from pathlib import Path

import pytest

from backends.frontmatter import PromptBuildError


def test_validate_agents_fails_with_missing_codex_field(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    bad = agents_dir / "broken.md"
    bad.write_text(
        "---\n"
        "name: broken\n"
        "model: opus\n"
        "codex_model: gpt-5.5\n"   # missing codex_effort
        "---\n"
        "body\n"
    )
    import backends
    b = backends.get_backend("codex")
    with pytest.raises(PromptBuildError) as exc_info:
        b.validate_agents(agents_dir)
    assert exc_info.value.missing == "codex_effort"


def test_classify_outcome_ambiguous_propagates(monkeypatch):
    """Force unexpected_events into the parsed result; hygiene must count it
    as ambiguous, not success."""
    from backends.base import ParsedRun
    from worker import classify_outcome

    p = ParsedRun(
        tokens_in=10, tokens_out=5, cached_in=0, model_used="gpt-5.5", cost_usd=0.01,
        state_deltas=[{"type": "hygiene", "fixed": True}],
        malformed_state_deltas=[],
        has_tool_use=True,
        unexpected_events=["web_search_request"],
    )
    outcome, delta = classify_outcome(0, p, require_delta_on_success=True)
    assert outcome == "ambiguous"
