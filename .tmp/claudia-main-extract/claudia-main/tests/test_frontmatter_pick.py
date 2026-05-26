"""Unit tests for backends.frontmatter.pick and parse_frontmatter."""
import pytest

from backends.frontmatter import PromptBuildError, parse_frontmatter, pick


def test_parse_frontmatter_basic():
    text = "---\nname: x\nmodel: opus\n---\nbody here\n"
    fm, body = parse_frontmatter(text)
    assert fm == {"name": "x", "model": "opus"}
    assert body == "body here\n"


def test_parse_frontmatter_no_frontmatter():
    fm, body = parse_frontmatter("just body\n")
    assert fm == {}
    assert body == "just body\n"


def test_parse_frontmatter_unterminated():
    fm, body = parse_frontmatter("---\nname: x\n")
    assert fm == {}
    assert body == "---\nname: x\n"


def test_pick_codex_happy_path():
    fm = {"codex_model": "gpt-5.5", "codex_effort": "xhigh", "model": "opus"}
    model, effort = pick("codex", fm, agent_name="a", agent_file="agents/a.md")
    assert model == "gpt-5.5"
    assert effort == "xhigh"


def test_pick_codex_missing_model_raises():
    fm = {"codex_effort": "xhigh"}
    with pytest.raises(PromptBuildError) as exc_info:
        pick("codex", fm, agent_name="pr-reviewer", agent_file="agents/pr-reviewer.md")
    assert exc_info.value.missing == "codex_model"
    assert "pr-reviewer" in str(exc_info.value)
    assert "agents/pr-reviewer.md" in str(exc_info.value)


def test_pick_codex_missing_effort_raises():
    fm = {"codex_model": "gpt-5.5"}
    with pytest.raises(PromptBuildError) as exc_info:
        pick("codex", fm, agent_name="a", agent_file="agents/a.md")
    assert exc_info.value.missing == "codex_effort"


def test_pick_claude_happy_path():
    fm = {"model": "opus", "max_turns": "1000"}
    model, turns = pick("claude", fm, agent_name="a", agent_file="agents/a.md")
    assert model == "opus"
    assert turns == 1000


def test_pick_claude_max_turns_optional():
    fm = {"model": "opus"}
    model, turns = pick("claude", fm, agent_name="a", agent_file="agents/a.md")
    assert model == "opus"
    assert turns is None


def test_pick_claude_missing_model_raises():
    fm = {"max_turns": "1000"}
    with pytest.raises(PromptBuildError) as exc_info:
        pick("claude", fm, agent_name="a", agent_file="agents/a.md")
    assert exc_info.value.missing == "model"
