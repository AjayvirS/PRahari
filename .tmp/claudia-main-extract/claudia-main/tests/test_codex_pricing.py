"""Tests for CodexBackend pricing (per §4)."""
import json
from pathlib import Path

from backends.base import RunContext
from backends.codex import CodexBackend

FIX = Path(__file__).parent / "fixtures" / "codex_jsonl"


def _ctx(model):
    return RunContext(prompt_path="/tmp/x", cwd="/r", model=model, effort_or_turns="xhigh")


def test_known_model_computes_cost(tmp_path):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({
        "gpt-5.5": {
            "input_per_mtok": 1.0,
            "output_per_mtok": 10.0,
            "cache_read_per_mtok": 0.1,
        }
    }))
    b = CodexBackend(prices_path=prices)
    p = b.parse_output(_ctx("gpt-5.5"), str(FIX / "success.jsonl"))
    # tokens_in=15769, tokens_out=238, cached_in=39040
    expected = (15769 * 1.0 + 238 * 10.0 + 39040 * 0.1) / 1_000_000
    assert p.cost_usd == expected


def test_zero_priced_model(tmp_path):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({
        "gpt-5.5": {
            "input_per_mtok": 0.0,
            "output_per_mtok": 0.0,
            "cache_read_per_mtok": 0.0,
        }
    }))
    b = CodexBackend(prices_path=prices)
    p = b.parse_output(_ctx("gpt-5.5"), str(FIX / "success.jsonl"))
    assert p.cost_usd == 0.0


def test_unknown_model_returns_none_and_warns(tmp_path, caplog):
    prices = tmp_path / "prices.json"
    prices.write_text("{}")
    b = CodexBackend(prices_path=prices)
    p = b.parse_output(_ctx("gpt-5.5"), str(FIX / "success.jsonl"))
    assert p.cost_usd is None
    assert any("No codex price entry" in r.message for r in caplog.records)


def test_missing_prices_file_returns_none(tmp_path):
    b = CodexBackend(prices_path=tmp_path / "missing.json")
    p = b.parse_output(_ctx("gpt-5.5"), str(FIX / "success.jsonl"))
    assert p.cost_usd is None


def test_invariant_violation_returns_none_even_with_prices(tmp_path):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"gpt-5.5": {
        "input_per_mtok": 1, "output_per_mtok": 1, "cache_read_per_mtok": 1,
    }}))
    b = CodexBackend(prices_path=prices)
    p = b.parse_output(_ctx("gpt-5.5"), str(FIX / "invariant_violation.jsonl"))
    assert p.cost_usd is None
