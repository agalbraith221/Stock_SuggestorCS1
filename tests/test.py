"""
Tests app.py's pure logic...matching/allocation/parsing/formatting.
 
Avoid network (yfinance) and avoid loading any real model (local transformers pipeline or the remote Hugging Face API), so the
suite runs fast and reliably in CI.
"""
import sys
from pathlib import Path
 
import pandas as pd
import pytest
 
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
 
import app  #noqa: E402
 
 
def test_allocate_slots_single_group_gets_everything():
    result = app.allocate_slots_by_group(["Technology"], total_slots=5)
    assert result == {"Technology": 5}
 
 
def test_allocate_slots_multiple_groups_split_evenly():
    groups = ["Technology", "Energy", "Technology", "Financials"]
    result = app.allocate_slots_by_group(groups, total_slots=5)
    assert sum(result.values()) == 5
    assert set(result.keys()) == {"Technology", "Energy", "Financials"}
 
 
def test_allocate_slots_empty_groups_returns_empty():
    assert app.allocate_slots_by_group([], total_slots=5) == {}
 
 
def test_rank_by_beta_window_prefers_closest_beta():
    candidates = [
        {"symbol": "A", "beta": 1.0},
        {"symbol": "B", "beta": 2.5},
        {"symbol": "C", "beta": 1.05},
    ]
    ranked = app.rank_by_beta_window(candidates, target_beta=1.0, slots=2)
    symbols = [c["symbol"] for c in ranked]
    assert len(symbols) == 2
    assert "B" not in symbols  # farthest beta excluded when closer options exist
 
 
def test_mode_labels_cover_all_five_required_modes():
    expected = {
        "Beta (closest risk level)",
        "Sector (overall)",
        "Sector (overall + by beta)",
        "Industry",
        "Industry (+ by beta)",
    }
    assert set(app.MODE_LABELS.keys()) == expected
 
 
def test_format_stock_block_includes_key_financial_fields():
    items = [{
        "symbol": "AAPL", "name": "Apple Inc.", "price": 329.715, "beta": 1.09,
        "sector": "Technology", "industry": "Consumer Electronics",
        "marketCap": 4812139134976,
    }]
    block = app.format_stock_block(items, "Your Selected Stocks/Funds")
    assert "AAPL" in block
    assert "Apple Inc." in block
    assert "Beta: 1.09" in block
    assert "$4,812,139,134,976" in block
 
 
def test_format_stock_block_handles_empty_list():
    block = app.format_stock_block([], "Suggested Stocks/Funds")
    assert "(none)" in block
 
 
def test_parse_llm_reasoning_extracts_per_symbol_paragraphs():
    text = (
        "AAPL: This is a solid long-term holding with low volatility.\n"
        "NVDA: High beta makes this a riskier addition to the portfolio.\n"
    )
    result = app.parse_llm_reasoning(text, ["AAPL", "NVDA"])
    assert "solid long-term holding" in result["AAPL"]
    assert "riskier addition" in result["NVDA"]
 
 
def test_parse_llm_reasoning_handles_empty_text():
    assert app.parse_llm_reasoning("", ["AAPL"]) == {}
 
 
def test_resolve_pick_exact_symbol_match():
    universe = pd.DataFrame([
        {"symbol_clean": "AAPL", "name_clean": "apple inc.", "marketCap": 4_800_000_000_000},
    ])
    symbol, note = app.resolve_pick(universe, "AAPL")
    assert symbol == "AAPL"
    assert note == ""
 
 
def test_evaluate_remotely_requires_a_token():
    # Must fail fast with a clear error and no network call when nobody's
    # signed in via Sign in with Hugging Face.
    with pytest.raises(RuntimeError):
        app.evaluate_remotely("any prompt", None)
 
 
def test_build_evaluation_prompt_includes_all_symbols_in_order():
    picks = [{"symbol": "AAPL", "name": "Apple Inc.", "beta": 1.09, "sector": "Technology"}]
    suggestions = [{"symbol": "MSFT", "name": "Microsoft Corp.", "beta": 0.9, "sector": "Technology"}]
    prompt = app.build_evaluation_prompt(picks, suggestions, "Industry")
    assert "AAPL" in prompt
    assert "MSFT" in prompt
    assert prompt.index("AAPL") < prompt.index("MSFT")
