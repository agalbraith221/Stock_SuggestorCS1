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
