def average(values):
    return sum(values) / len(values)
 
 
def test_addition():
    assert 2 + 2 == 4
 
 
def test_average_of_betas():
    # e.g. averaging the beta (risk) of a few sample stocks
    betas = [1.09, 0.90, 1.25]
    assert round(average(betas), 2) == 1.08
 
 
def test_percentage_change():
    old_price = 150.00
    new_price = 165.00
    percent_change = ((new_price - old_price) / old_price) * 100
    assert round(percent_change, 2) == 10.0
 
 
def test_sorting_tickers_alphabetically():
    tickers = ["TSLA", "AAPL", "NVDA", "GOOGL"]
    assert sorted(tickers) == ["AAPL", "GOOGL", "NVDA", "TSLA"]
 
 
def test_max_market_cap():
    market_caps = {"AAPL": 4_812_139_134_976, "MSFT": 3_100_000_000_000, "NVDA": 4_500_000_000_000}
    assert max(market_caps, key=market_caps.get) == "AAPL"
    prompt = app.build_evaluation_prompt(picks, suggestions, "Industry")
    assert "AAPL" in prompt
    assert "MSFT" in prompt
    assert prompt.index("AAPL") < prompt.index("MSFT")
