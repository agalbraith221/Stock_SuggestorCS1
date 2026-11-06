import os
import re
import sys
import time
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
 
import gradio as gr
import pandas as pd
from huggingface_hub import HfApi, InferenceClient
from transformers import pipeline
from prometheus_client import start_http_server, Counter, Summary, Info
 
try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance is not installed. Run: pip install yfinance --upgrade")
 
 
# --- Prometheus metrics ---
REQUEST_COUNTER = Counter('app_requests_total', 'Total number of requests')
SUCCESSFUL_REQUESTS = Counter('app_successful_requests_total', 'Total number of successful requests')
FAILED_REQUESTS = Counter('app_failed_requests_total', 'Total number of failed requests')
REQUEST_DURATION = Summary('app_request_duration_seconds', 'Time spent processing request')
APP_INFO = Info('app', 'Which product/container is producing these metrics')
 
LOCAL_MODEL = "Qwen/Qwen3-0.6B"
REMOTE_MODEL = "openai/gpt-oss-20b"
 
# --- Container / product name ---
# G17_StockSuggestor  = container 1 (API-based product)
# G17_StockSuggestor2 = container 2 (locally executed product)
# Same file for both: run container 2 with -e PRODUCT_NAME=G17_StockSuggestor2
PRODUCT_NAME = os.environ.get("PRODUCT_NAME", "g17_stocksuggestor")
APP_INFO.info({"product": PRODUCT_NAME})
 
pipe = pipeline(
    "text-generation",
    model=LOCAL_MODEL,
    dtype="auto",
    device="cpu",
)
 
 
DATA_DIR = Path(__file__).parent / "data"
TICKER_CSVS = [
    DATA_DIR / "nasdaq_full_tickers.csv",
    DATA_DIR / "nyse_full_tickers.csv",
]
 
DEFAULT_PICKS = ["AAPL", "GOOGL", "NVDA", "TSLA"]
DEFAULT_BENCHMARK = "^GSPC"
 
NUM_SUGGESTIONS = 5
FUND_SLOTS = 1
COMPANY_SLOTS = NUM_SUGGESTIONS - FUND_SLOTS
 
MAX_PER_GROUP = 2
CANDIDATES_PER_GROUP = 15
BETA_WINDOW = 0.2
NEUTRAL_BETA = 1.0
LIVE_FETCH_DELAY = 0.2  # polite to yfinance
 
FUND_QUOTE_TYPES = {"ETF", "MUTUALFUND", "INDEX"}
 
DEFAULT_SYSTEM_MESSAGE = (
    "You are a cautious financial-education assistant, not a licensed financial "
    "advisor. Do not give definitive buy/sell instructions."
)
 
ALIAS_MAP = {
    "google": ["GOOGL", "GOOG"], "alphabet": ["GOOGL", "GOOG"],
    "facebook": ["META"], "fb": ["META"], "meta": ["META"],
    "berkshire": ["BRK.A", "BRK.B"], "berkshire hathaway": ["BRK.A", "BRK.B"],
    "jpmorgan": ["JPM"], "jp morgan": ["JPM"], "chase": ["JPM"],
    "coca cola": ["KO"], "coke": ["KO"],
    "disney": ["DIS"], "walmart": ["WMT"],
    "exxon": ["XOM"], "exxonmobil": ["XOM"],
    "visa": ["V"], "mastercard": ["MA"],
    "amazon": ["AMZN"], "apple": ["AAPL"], "microsoft": ["MSFT"],
    "nvidia": ["NVDA"], "tesla": ["TSLA"], "netflix": ["NFLX"],
    "att": ["T"], "at&t": ["T"], "verizon": ["VZ"], "boeing": ["BA"],
    "goldman": ["GS"], "goldman sachs": ["GS"],
    "starbucks": ["SBUX"], "mcdonalds": ["MCD"], "mcdonald's": ["MCD"],
    "nike": ["NKE"], "intel": ["INTC"], "ibm": ["IBM"], "oracle": ["ORCL"],
    "salesforce": ["CRM"], "paypal": ["PYPL"], "adobe": ["ADBE"],
    "costco": ["COST"], "home depot": ["HD"], "target": ["TGT"],
    "chevron": ["CVX"], "pfizer": ["PFE"],
    "johnson & johnson": ["JNJ"], "johnson and johnson": ["JNJ"], "j&j": ["JNJ"],
    "united airlines": ["UAL"], "american airlines": ["AAL"],
    "delta": ["DAL"], "ford": ["F"], "general motors": ["GM"], "gm": ["GM"],
}
 
FUND_UNIVERSE = {
    "Broad Market": [
        ("SPY", "SPDR S&P 500 ETF Trust"), ("VOO", "Vanguard S&P 500 ETF"),
        ("IVV", "iShares Core S&P 500 ETF"), ("VTI", "Vanguard Total Stock Market ETF"),
        ("QQQ", "Invesco QQQ Trust (Nasdaq-100)"), ("DIA", "SPDR Dow Jones Industrial Average ETF"),
        ("IWM", "iShares Russell 2000 ETF"),
    ],
    "Technology Sector": [
        ("XLK", "Technology Select Sector SPDR Fund"), ("VGT", "Vanguard Information Technology ETF"),
        ("SMH", "VanEck Semiconductor ETF"), ("SOXX", "iShares Semiconductor ETF"),
    ],
    "Financials Sector": [
        ("XLF", "Financial Select Sector SPDR Fund"), ("VFH", "Vanguard Financials ETF"),
        ("KBE", "SPDR S&P Bank ETF"),
    ],
    "Energy Sector": [
        ("XLE", "Energy Select Sector SPDR Fund"), ("VDE", "Vanguard Energy ETF"),
        ("XOP", "SPDR S&P Oil & Gas Exploration ETF"),
    ],
    "Healthcare Sector": [
        ("XLV", "Health Care Select Sector SPDR Fund"), ("VHT", "Vanguard Health Care ETF"),
        ("IBB", "iShares Biotechnology ETF"),
    ],
    "Consumer/Discretionary Sector": [
        ("XLY", "Consumer Discretionary Select Sector SPDR Fund"),
        ("XLP", "Consumer Staples Select Sector SPDR Fund"), ("VCR", "Vanguard Consumer Discretionary ETF"),
    ],
    "Bonds/Fixed Income": [
        ("BND", "Vanguard Total Bond Market ETF"), ("AGG", "iShares Core U.S. Aggregate Bond ETF"),
        ("TLT", "iShares 20+ Year Treasury Bond ETF"), ("SHY", "iShares 1-3 Year Treasury Bond ETF"),
    ],
    "Commodities": [
        ("GLD", "SPDR Gold Shares"), ("SLV", "iShares Silver Trust"), ("USO", "United States Oil Fund"),
    ],
    "International": [
        ("VEA", "Vanguard FTSE Developed Markets ETF"), ("VWO", "Vanguard FTSE Emerging Markets ETF"),
        ("EFA", "iShares MSCI EAFE ETF"),
    ],
    "Dividend/Value": [
        ("VYM", "Vanguard High Dividend Yield ETF"), ("SCHD", "Schwab US Dividend Equity ETF"),
        ("VTV", "Vanguard Value ETF"),
    ],
    "Growth/Innovation": [
        ("VUG", "Vanguard Growth ETF"), ("ARKK", "ARK Innovation ETF"), ("QQQM", "Invesco NASDAQ 100 ETF"),
    ],
}
 
FUND_SYMBOL_CATEGORY = {
    "^GSPC": "Broad Market", "^DJI": "Broad Market", "^IXIC": "Broad Market", "^RUT": "Broad Market",
    "SPY": "Broad Market", "VOO": "Broad Market", "IVV": "Broad Market", "VTI": "Broad Market",
    "QQQ": "Broad Market", "QQQM": "Broad Market", "DIA": "Broad Market", "IWM": "Broad Market",
    "XLK": "Technology Sector", "VGT": "Technology Sector", "SMH": "Technology Sector", "SOXX": "Technology Sector",
    "XLF": "Financials Sector", "VFH": "Financials Sector", "KBE": "Financials Sector",
    "XLE": "Energy Sector", "VDE": "Energy Sector", "XOP": "Energy Sector",
    "XLV": "Healthcare Sector", "VHT": "Healthcare Sector", "IBB": "Healthcare Sector",
    "XLY": "Consumer/Discretionary Sector", "XLP": "Consumer/Discretionary Sector", "VCR": "Consumer/Discretionary Sector",
    "BND": "Bonds/Fixed Income", "AGG": "Bonds/Fixed Income", "TLT": "Bonds/Fixed Income", "SHY": "Bonds/Fixed Income",
    "GLD": "Commodities", "SLV": "Commodities", "USO": "Commodities",
    "VEA": "International", "VWO": "International", "EFA": "International",
    "VYM": "Dividend/Value", "SCHD": "Dividend/Value", "VTV": "Dividend/Value",
    "VUG": "Growth/Innovation", "ARKK": "Growth/Innovation",
}
 
SECTOR_TO_FUND_CATEGORY = {
    "Technology": "Technology Sector",
    "Finance": "Financials Sector", "Financials": "Financials Sector",
    "Energy": "Energy Sector",
    "Health Care": "Healthcare Sector", "Healthcare": "Healthcare Sector",
    "Consumer Discretionary": "Consumer/Discretionary Sector",
    "Consumer Staples": "Consumer/Discretionary Sector",
    "Real Estate": "Broad Market", "Industrials": "Broad Market", "Utilities": "Broad Market",
    "Basic Materials": "Broad Market", "Telecommunications": "Technology Sector",
}
 
 
fancy_css = """
.gradio-container {
    width: 96% !important;
    max-width: none !important;
}
#app-title {
    text-align: center;
    margin-bottom: 4px;
}
#app-subtitle {
    text-align: center;
    color: var(--body-text-color-subdued);
    margin-bottom: 24px;
}
#chat-container {
    width: 100%;
    border: 1px solid var(--border-color-primary);
    border-radius: 12px;
    padding: 16px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}
#model-note {
    font-size: 0.9em;
    color: var(--body-text-color-subdued);
    margin-top: 8px;
}
@media (max-width: 768px) {
    .gradio-container {
        width: 98% !important;
    }
    #chat-container {
        padding: 8px;
    }
}
"""
 
 
# --- DATA LOADING ---
 
def safe_str(value, default="N/A") -> str:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    return str(value)
 
 
def load_universe(paths) -> pd.DataFrame:
    frames = []
    for path in paths:
        p = Path(path)
        if not p.exists():
            continue
        frames.append(pd.read_csv(p, dtype=str))
    if not frames:
        return pd.DataFrame(columns=["symbol", "name", "sector", "industry", "marketCap",
                                     "name_clean", "symbol_clean"])
 
    df = pd.concat(frames, ignore_index=True)
    df["marketCap"] = pd.to_numeric(df["marketCap"], errors="coerce").fillna(0)
    df["name"] = df["name"].fillna("")
    df["symbol"] = df["symbol"].fillna("")
    df["name_clean"] = df["name"].str.lower().str.strip()
    df["symbol_clean"] = df["symbol"].str.upper().str.strip()
    df = df[df["symbol_clean"] != ""]
    df = df.drop_duplicates(subset="symbol_clean")
    return df
 
 
UNIVERSE = load_universe(TICKER_CSVS)
DATA_LOAD_WARNING = (
    "No ticker data found under ./data/. Add nasdaq_full_tickers.csv and "
    "nyse_full_tickers.csv (columns: symbol, name, sector, industry, marketCap) "
    "to enable industry/sector matching. Fund/index picks like SPY or ^GSPC still work."
) if UNIVERSE.empty else ""
print(f"[startup] ticker universe: {len(UNIVERSE)} rows loaded "
      f"from {[str(p) for p in TICKER_CSVS if p.exists()]}")
 
 
# --- MATCHING ---
 
def name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()
 
 
def resolve_alias(query: str, universe: pd.DataFrame) -> pd.DataFrame:
    key = query.strip().lower()
    tickers = ALIAS_MAP.get(key)
    if not tickers:
        return pd.DataFrame()
    return universe[universe["symbol_clean"].isin([t.upper() for t in tickers])]
 
 
def search_ticker(universe: pd.DataFrame, query: str) -> pd.DataFrame:
    q = query.strip()
    q_upper = q.upper()
    q_lower = q.lower()
 
    exact = universe[universe["symbol_clean"] == q_upper]
    if not exact.empty:
        return exact
 
    alias_matches = resolve_alias(q, universe)
    if not alias_matches.empty:
        return alias_matches
 
    starts = universe[universe["symbol_clean"].str.startswith(q_upper, na=False)]
    contains = universe[universe["name_clean"].str.contains(q_lower, na=False, regex=False)]
    candidates = pd.concat([starts, contains]).drop_duplicates(subset="symbol_clean")
    if candidates.empty:
        return candidates
 
    candidates = candidates.copy()
    candidates["score"] = candidates["name_clean"].apply(lambda n: name_similarity(q_lower, n))
    return candidates.sort_values(["score", "marketCap"], ascending=[False, False])
 
 
def resolve_pick(universe: pd.DataFrame, query: str) -> tuple:
    """Resolve one user-entered pick to a ticker symbol. Returns (symbol, note)."""
    q = query.strip()
    if not q:
        return None, ""
    q_upper = q.upper()
 
    if q_upper.startswith("^") or q_upper in FUND_SYMBOL_CATEGORY:
        return q_upper, ""
 
    matches = search_ticker(universe, q)
    if matches.empty:
        # Might still be a valid ticker yfinance knows about even if it's
        # not in our local universe file (e.g. universe not loaded).
        return q_upper, "not found in local listings — trying as a raw ticker"
 
    chosen = matches.iloc[0]
    note = "" if len(matches) == 1 else "multiple matches — auto-picked best match"
    return chosen["symbol_clean"], note
 
 
# --- LIVE DATA ---
 
def fetch_live_info(ticker: str) -> dict:
    info = {}
    if yf is not None:
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception:
            info = {}
 
    quote_type = (info.get("quoteType") or "").upper()
    symbol_upper = ticker.upper()
    is_fund = (
        quote_type in FUND_QUOTE_TYPES
        or symbol_upper.startswith("^")
        or symbol_upper in FUND_SYMBOL_CATEGORY
    )
 
    sector = info.get("sector")
    industry = info.get("industry")
    if is_fund:
        category = FUND_SYMBOL_CATEGORY.get(symbol_upper, "Broad Market")
        industry = f"Fund: {category}"
        sector = "Fund/ETF"
 
    return {
        "symbol": symbol_upper,
        "name": info.get("longName") or info.get("shortName") or symbol_upper,
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "beta": info.get("beta"),
        "sector": sector,
        "industry": industry,
        "marketCap": info.get("marketCap"),
        "is_fund": is_fund,
        "type": "Fund" if is_fund else "Company",
    }
 
 
# --- SUGGESTION ENGINE ---
 
def allocate_slots_by_group(groups, total_slots=NUM_SUGGESTIONS) -> dict:
    groups = list(dict.fromkeys(groups))
    if not groups:
        return {}
    if len(groups) == 1:
        return {groups[0]: total_slots}
    if len(groups) >= total_slots:
        return {g: 1 for g in groups[:total_slots]}
 
    allocation = {g: 1 for g in groups}
    remaining = total_slots - len(groups)
    idx = 0
    stalled = 0
    cap = MAX_PER_GROUP
    while remaining > 0:
        g = groups[idx % len(groups)]
        if allocation[g] < cap:
            allocation[g] += 1
            remaining -= 1
            stalled = 0
        else:
            stalled += 1
        idx += 1
        if stalled >= len(groups):
            cap += 1
            stalled = 0
    return allocation
 
 
def rank_by_beta_window(candidates, target_beta, slots, window=BETA_WINDOW):
    for c in candidates:
        b = c.get("beta") if c.get("beta") is not None else NEUTRAL_BETA
        c["beta_diff"] = abs(b - target_beta)
    candidates.sort(key=lambda c: c["beta_diff"])
    within = [c for c in candidates if c["beta_diff"] <= window]
    if len(within) >= slots:
        return within[:slots]
    remaining_needed = slots - len(within)
    outside = [c for c in candidates if c["beta_diff"] > window]
    return within + outside[:remaining_needed]
 
 
def pick_company_slots_for_group(universe, group_value, group_key, filled_symbols, slots, target_beta):
    if slots <= 0:
        return []
    pool = universe[
        (universe[group_key] == group_value) & (~universe["symbol_clean"].isin(filled_symbols))
    ].sort_values("marketCap", ascending=False).head(CANDIDATES_PER_GROUP)
 
    candidates = []
    for _, row in pool.iterrows():
        info = fetch_live_info(row["symbol_clean"])
        info["industry_used"] = group_value
        candidates.append(info)
        time.sleep(LIVE_FETCH_DELAY)
    return rank_by_beta_window(candidates, target_beta, slots)
 
 
def pick_generic_company_slots(universe, filled_symbols, slots, target_beta):
    if slots <= 0:
        return []
    pool = universe[~universe["symbol_clean"].isin(filled_symbols)] \
        .sort_values("marketCap", ascending=False).head(CANDIDATES_PER_GROUP * 2)
    candidates = []
    for _, row in pool.iterrows():
        info = fetch_live_info(row["symbol_clean"])
        candidates.append(info)
        time.sleep(LIVE_FETCH_DELAY)
    return rank_by_beta_window(candidates, target_beta, slots)
 
 
def _row_to_company_dict(row, group_value=None):
    return {
        "symbol": row["symbol_clean"], "name": row["name"],
        "sector": row["sector"] if pd.notna(row.get("sector")) else None,
        "industry": row["industry"] if pd.notna(row.get("industry")) else None,
        "industry_used": group_value, "marketCap": row["marketCap"],
        "type": "Company", "is_fund": False, "beta": None,
    }
 
 
def pick_company_slots_for_group_by_marketcap(universe, group_value, group_key, filled_symbols, slots):
    if slots <= 0:
        return []
    pool = universe[
        (universe[group_key] == group_value) & (~universe["symbol_clean"].isin(filled_symbols))
    ].sort_values("marketCap", ascending=False).head(slots)
    return [_row_to_company_dict(row, group_value) for _, row in pool.iterrows()]
 
 
def pick_generic_company_slots_by_marketcap(universe, filled_symbols, slots):
    if slots <= 0:
        return []
    pool = universe[~universe["symbol_clean"].isin(filled_symbols)] \
        .sort_values("marketCap", ascending=False).head(slots)
    return [_row_to_company_dict(row) for _, row in pool.iterrows()]
 
 
def relevant_fund_categories(picks_info, broad=False):
    if broad:
        return list(FUND_UNIVERSE.keys())
    categories = []
    for p in picks_info:
        if p.get("is_fund"):
            industry = p.get("industry") or ""
            cat = industry.replace("Fund: ", "", 1)
            if cat in FUND_UNIVERSE:
                categories.append(cat)
        else:
            mapped = SECTOR_TO_FUND_CATEGORY.get(p.get("sector"), "Broad Market")
            categories.append(mapped)
    categories.append("Broad Market")
    return list(dict.fromkeys(categories))
 
 
def pick_fund_slots(picks_info, filled_symbols, slots, target_beta, broad=False):
    if slots <= 0:
        return []
    categories = relevant_fund_categories(picks_info, broad=broad)
    pool = [
        {"symbol": sym, "name": name, "sector": "Fund/ETF", "industry": f"Fund: {cat}",
         "type": "Fund", "is_fund": True}
        for cat in categories for sym, name in FUND_UNIVERSE.get(cat, [])
        if sym not in filled_symbols
    ]
    seen, deduped = set(), []
    for f in pool:
        if f["symbol"] not in seen:
            seen.add(f["symbol"])
            deduped.append(f)
 
    candidates = []
    for f in deduped[: slots * 4]:
        info = fetch_live_info(f["symbol"])
        f["beta"] = info["beta"]
        f["name"] = info["name"] or f["name"]
        candidates.append(f)
        time.sleep(LIVE_FETCH_DELAY)
    return rank_by_beta_window(candidates, target_beta, slots)
 
 
def pick_fund_slots_no_beta(picks_info, filled_symbols, slots, broad=False):
    if slots <= 0:
        return []
    categories = relevant_fund_categories(picks_info, broad=broad)
    pool = [
        {"symbol": sym, "name": name, "sector": "Fund/ETF", "industry": f"Fund: {cat}",
         "type": "Fund", "is_fund": True, "beta": None}
        for cat in categories for sym, name in FUND_UNIVERSE.get(cat, [])
        if sym not in filled_symbols
    ]
    seen, deduped = set(), []
    for f in pool:
        if f["symbol"] not in seen:
            seen.add(f["symbol"])
            deduped.append(f)
    return deduped[:slots]
 
 
def enrich_with_live_info(suggestions):
    enriched = []
    for s in suggestions:
        info = fetch_live_info(s["symbol"])
        merged = dict(s)
        merged["beta"] = info.get("beta")
        merged["price"] = info.get("price")
        enriched.append(merged)
        time.sleep(LIVE_FETCH_DELAY)
    return enriched
 
 
def build_suggestions(universe, picks_info, exclude_symbols, group_key,
                      broad_funds=False, use_beta_filter=False):
    filled = set(exclude_symbols)
    target_beta = None
 
    if use_beta_filter:
        target_betas = [p["beta"] if p.get("beta") is not None else NEUTRAL_BETA for p in picks_info]
        target_beta = sum(target_betas) / len(target_betas)
 
    # group_key can be None ("pure" mode with no sector/industry grouping):
    # candidates are ranked across the whole universe instead.
    company_groups = [
        p[group_key] for p in picks_info
        if group_key and p.get(group_key)
        and not str(p[group_key]).startswith("Fund: ") and p.get(group_key) != "Fund/ETF"
    ]
 
    companies = []
    if company_groups:
        allocation = allocate_slots_by_group(company_groups, total_slots=COMPANY_SLOTS)
        for group_value, slots in allocation.items():
            if use_beta_filter:
                picked = pick_company_slots_for_group(universe, group_value, group_key, filled, slots, target_beta)
            else:
                picked = pick_company_slots_for_group_by_marketcap(universe, group_value, group_key, filled, slots)
            companies.extend(picked)
            filled.update(c["symbol"] for c in picked)
    else:
        if use_beta_filter:
            companies = pick_generic_company_slots(universe, filled, COMPANY_SLOTS, target_beta)
        else:
            companies = pick_generic_company_slots_by_marketcap(universe, filled, COMPANY_SLOTS)
        filled.update(c["symbol"] for c in companies)
 
    shortfall = COMPANY_SLOTS - len(companies)
    if shortfall > 0:
        if use_beta_filter:
            top_up = pick_generic_company_slots(universe, filled, shortfall, target_beta)
        else:
            top_up = pick_generic_company_slots_by_marketcap(universe, filled, shortfall)
        companies.extend(top_up)
        filled.update(c["symbol"] for c in top_up)
 
    if use_beta_filter:
        funds = pick_fund_slots(picks_info, filled, FUND_SLOTS, target_beta, broad=broad_funds)
    else:
        funds = pick_fund_slots_no_beta(picks_info, filled, FUND_SLOTS, broad=broad_funds)
    filled.update(f["symbol"] for f in funds)
 
    fund_shortfall = FUND_SLOTS - len(funds)
    if fund_shortfall > 0:
        if use_beta_filter:
            extra = pick_fund_slots(picks_info, filled, fund_shortfall, target_beta, broad=True)
        else:
            extra = pick_fund_slots_no_beta(picks_info, filled, fund_shortfall, broad=True)
        funds.extend(extra)
 
    combined = companies + funds
    if not use_beta_filter:
        combined = enrich_with_live_info(combined)
    return combined[:NUM_SUGGESTIONS]
 
 
# (group_key, broad_funds, use_beta_filter): order here is the order the mode
# buttons are drawn in, left to right (least to most specific)
MODE_LABELS = {
    "Beta (closest risk level)": (None, False, True),
    "Sector (overall)": ("sector", True, False),
    "Sector (overall + by beta)": ("sector", True, True),
    "Industry": ("industry", False, False),
    "Industry (+ by beta)": ("industry", False, True),
}
 
 
def generate_suggestions(mode_choice, picks_info, exclude_symbols):
    group_key, broad_funds, use_beta_filter = MODE_LABELS[mode_choice]
    return build_suggestions(UNIVERSE, picks_info, exclude_symbols, group_key,
                             broad_funds=broad_funds, use_beta_filter=use_beta_filter)
 
 
# --- FORMATTING ---
 
def picks_info_to_df(picks_info) -> pd.DataFrame:
    rows = []
    for p in picks_info:
        beta = p.get("beta")
        rows.append({
            "Symbol": safe_str(p.get("symbol")),
            "Name": safe_str(p.get("name")),
            "Price": safe_str(p.get("price")),
            "Beta": f"{beta:.2f}" if beta is not None else "N/A",
            "Sector": safe_str(p.get("sector")),
            "Industry": safe_str(p.get("industry")),
            "Match note": p.get("_note", ""),
        })
    return pd.DataFrame(rows)
 
 
def suggestions_to_df(suggestions) -> pd.DataFrame:
    rows = []
    for s in suggestions:
        beta = s.get("beta")
        industry = s.get("industry_used") or s.get("industry")
        rows.append({
            "Symbol": safe_str(s.get("symbol")),
            "Name": safe_str(s.get("name")),
            "Type": safe_str(s.get("type"), default="Company"),
            "Sector": safe_str(s.get("sector")),
            "Industry": safe_str(industry),
            "Beta": f"{beta:.2f}" if beta is not None else "N/A",
        })
    return pd.DataFrame(rows)
 
 
# --- LLM EVALUATION (remote HF API call / local model) ---
 
def format_stock_block(items, title) -> str:
    """Deterministically formatted financial-data listing (ticker, company name,
    price, beta, sector, industry, market cap) built straight from the data we
    already pulled from yfinance — not something we ask the LLM to reproduce,
    since models are unreliable at echoing exact numbers."""
    lines = [f"=== {title} ==="]
    if not items:
        lines.append("  (none)")
        return "\n".join(lines)
    for it in items:
        symbol = str(it.get("symbol", "?"))
        name = str(it.get("name", "?"))
        price = it.get("price")
        price_str = f"{price:.3f}" if isinstance(price, (int, float)) else "N/A"
        beta = it.get("beta")
        beta_str = f"{beta:.2f}" if isinstance(beta, (int, float)) else "N/A"
        sector = it.get("sector") or "N/A"
        industry = it.get("industry_used") or it.get("industry") or "N/A"
        mcap = it.get("marketCap")
        mcap_str = f"${mcap:,.0f}" if isinstance(mcap, (int, float)) and mcap else "N/A"
        lines.append(
            f"  {symbol:<8} {name:<38} Price: {price_str:<10} Beta: {beta_str:<6} "
            f"Sector: {sector:<20} Industry: {industry:<28} MktCap: {mcap_str}"
        )
    return "\n".join(lines)
 
 
def build_evaluation_prompt(picks_info, all_suggestions, mode_choice) -> str:
    picks_block = format_stock_block(picks_info, "Your Selected Stocks/Funds")
    suggestions_block = format_stock_block(all_suggestions, "Suggested Stocks/Funds")
    all_symbols = [it.get("symbol") for it in (picks_info + all_suggestions) if it.get("symbol")]
    symbols_line = ", ".join(all_symbols)
    return (
        f"{picks_block}\n\n"
        f"(Suggestions were generated using the '{mode_choice}' matching mode.)\n"
        f"{suggestions_block}\n\n"
        f"For EACH of the following symbols, in this exact order, write one short "
        f"paragraph (2-4 sentences):\n{symbols_line}\n\n"
        "Start each paragraph on its own line with the symbol followed by a colon, "
        "for example 'AAPL: ...' — use the symbol exactly as given, nothing before it "
        "on that line. State plainly whether it looks like a good or not-so-good pick "
        "for a long-term portfolio and why, considering its sector/industry, its risk "
        "level (beta) relative to the other symbols listed, and how much it overlaps "
        "with (or diversifies) the rest of the list."
    )
 
 
def parse_llm_reasoning(text: str, symbols) -> dict:
    """Pull each 'SYMBOL: ...' paragraph out of the model's free-form reply so it
    can be lined up with the data block for that symbol."""
    reasoning = {}
    if not text or not symbols:
        return reasoning
    pattern = re.compile(
        r"^\s*\**\s*(" + "|".join(re.escape(s) for s in symbols) + r")\s*\**\s*[:—-]\s*(.*)$",
        re.IGNORECASE,
    )
    current, buffer = None, []
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            if current:
                reasoning[current] = " ".join(buffer).strip()
            current = m.group(1).upper()
            buffer = [m.group(2).strip()]
        elif current:
            buffer.append(line.strip())
    if current:
        reasoning[current] = " ".join(buffer).strip()
    return reasoning
 
 
def build_final_report(picks_info, all_suggestions, reasoning_by_symbol, raw_text, backend_choice) -> str:
    """Combine the deterministic financial-data listing with the LLM's per-symbol
    reasoning, in the style: data line, then why it's a good/bad pick."""
    parts = [f"**Model used:** {backend_choice}", ""]
 
    def render_section(items, title):
        section = [f"```\n{format_stock_block(items, title)}\n```"]
        for it in items:
            sym = str(it.get("symbol", "")).upper()
            reason = reasoning_by_symbol.get(sym)
            if reason:
                section.append(f"**{sym}** — {reason}")
        return "\n\n".join(section)
 
    if picks_info:
        parts.append(render_section(picks_info, "Your Selected Stocks/Funds"))
    if all_suggestions:
        parts.append(render_section(all_suggestions, "Suggested Stocks/Funds"))
 
    if not reasoning_by_symbol:
        # Parsing found nothing usable: fall back to showing the raw reply so nothing is silently lost.
        parts.append("---\n" + raw_text)
 
    parts.append("Please remember that these are just suggestions and generalizations, "
                 "not certified financial advice")
 
    return "\n\n".join(parts)
 
 
def local_generate(
    prompt,
    system_message,
    max_tokens,
    temperature,
    top_p,
):
    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": prompt},
    ]
 
    # Qwen3 models "think" by default (<think>...</think>), which can eat the
    # whole token budget and leave the visible answer empty. Turn that off.
    try:
        templated = pipe.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False,
        )
    except TypeError:
        templated = pipe.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
 
    outputs = pipe(
        templated,
        max_new_tokens=int(max_tokens),
        do_sample=True,
        temperature=temperature,
        top_p=top_p,
        return_full_text=False,
    )
    text = outputs[0]["generated_text"]
 
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    text = text.strip()
 
    if not text:
        raise RuntimeError(
            "The local model returned an empty response — it may have used its "
            "entire token budget on internal reasoning. Try again."
        )
    return text
 
 
def remote_generate(
    prompt,
    system_message,
    max_tokens,
    temperature,
    top_p,
    hf_token,
):
    token = (hf_token or "").strip()
    if not token:
        raise RuntimeError("⚠️ Please enter your Hugging Face token first.")
 
    # Short timeout so an unresponsive/overloaded API is a detectable failure
    # (Auto mode then falls back to the local model) instead of hanging.
    client = InferenceClient(
        token=token,
        model=REMOTE_MODEL,
        timeout=30,
    )
    completion = client.chat_completion(
        [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        max_tokens=int(max_tokens),
        temperature=temperature,
        top_p=top_p,
    )
    text = (completion.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError(
            "The API model returned an empty response (a reasoning model can use "
            "its whole token budget thinking). Raise 'Max new tokens' and try again."
        )
    return text
 
 
def run_evaluation(
    state,
    backend_choice,
    simulate_remote_failure,
    hf_token,
    system_message,
    max_tokens,
    temperature,
    top_p,
):
    """
    ADAPTIVE LLM FAILOVER
    "Auto" mode always tries the remote Hugging Face API model first and, if that
    call fails for ANY reason (no token, timeout, rate limit, API unavailable),
    automatically falls back to the local model instead of just showing an error.
    "Local only" / "Hugging Face API only" are manual overrides that do NOT fail over.
    """
    REQUEST_COUNTER.inc()
    start_t = time.perf_counter()
    succeeded = False
    try:
        picks_info = state.get("picks_info", [])
        all_suggestions = state.get("all_suggestions", [])
        mode_choice = state.get("mode", "")
 
        if not picks_info or not all_suggestions:
            FAILED_REQUESTS.inc()
            return ("Get some suggestions first, then evaluate them.",
                    gr.update(), gr.update(), gr.update())
 
        prompt = build_evaluation_prompt(picks_info, all_suggestions, mode_choice)
        all_symbols = [it.get("symbol") for it in (picks_info + all_suggestions) if it.get("symbol")]
 
        def try_remote():
            if simulate_remote_failure:
                # Lets you demo the failover on demand instead of waiting for a real outage.
                raise RuntimeError("simulated failure (demo mode) — pretending the remote API is unavailable")
            print("[MODE] api")
            return remote_generate(prompt, system_message, max_tokens, temperature, top_p, hf_token)
 
        def try_local():
            print("[MODE] local")
            return local_generate(prompt, system_message, max_tokens, temperature, top_p)
 
        if backend_choice.startswith("Local only"):
            raw_text = try_local()
            model_label = f"Local ({LOCAL_MODEL})"
 
        elif backend_choice.startswith("Hugging Face API only"):
            raw_text = try_remote()
            model_label = f"Hugging Face API ({REMOTE_MODEL})"
 
        else:  # Auto: automatic failover, no manual model selection required
            try:
                raw_text = try_remote()
                model_label = f"Hugging Face API ({REMOTE_MODEL}) — primary, no failover needed"
            except Exception as remote_error:
                print(f"[failover] remote model failed, switching to local automatically: {remote_error}")
                raw_text = try_local()
                model_label = (
                    f"Local ({LOCAL_MODEL}) — AUTOMATIC FALLBACK "
                    f"(remote Hugging Face API failed: {remote_error})"
                )
 
        reasoning_by_symbol = parse_llm_reasoning(raw_text, all_symbols)
        evaluation = build_final_report(picks_info, all_suggestions, reasoning_by_symbol, raw_text, model_label)
        SUCCESSFUL_REQUESTS.inc()
        succeeded = True
    except Exception as e:
        FAILED_REQUESTS.inc()
        evaluation = (
            f"Error: {e}\n\n"
            f"Evaluation failed using {backend_choice}. If you picked the Hugging Face "
            "API option, enter and validate your token above. If you picked Local, "
            "make sure there's enough memory available."
        )
    finally:
        REQUEST_DURATION.observe(time.perf_counter() - start_t)
 
    if not succeeded:
        # A FAILED evaluation must not end the loop: leave every control as it is so
        # the user can fix the problem (e.g. enter a token) and retry, or keep
        # pulling suggestions.
        return (evaluation, gr.update(), gr.update(), gr.update())
 
    # The user chose to evaluate (with the API model, the local model, or Auto):
    # that choice is what ENDS the suggestion loop. Hide the mode buttons and lock
    # the evaluation controls until they start a new round with "Get My Picks".
    return (
        evaluation,
        gr.update(visible=False),      # mode_buttons_row
        gr.update(interactive=False),  # eval_button
        gr.update(interactive=False),  # eval_backend
    )
 
 
def validate_hf_token(hf_token):
    if not hf_token or not hf_token.strip():
        return "⚠️ Enter a Hugging Face token."
 
    try:
        account = HfApi(token=hf_token.strip()).whoami()
        username = account.get("name", "unknown user")
        return f"Valid Hugging Face token for **{username}**."
    except Exception:
        return (
            "The token could not be validated. "
        )
 
 
# --- GRADIO CALLBACKS ---
 
def resolve_picks(picks_text, progress=gr.Progress()):
    """Step 1: resolve the user's typed picks to real tickers and show them.
    The mode buttons (and, later, the AI evaluation button) only appear once
    picks have actually been resolved."""
    empty = pd.DataFrame()
 
    if not picks_text or not picks_text.strip():
        state = {"picks_info": [], "exclude": [], "mode": None, "all_suggestions": []}
        return (
            empty, state, DATA_LOAD_WARNING,
            gr.update(visible=False),        # mode_buttons_row
            empty,                           # suggestions_table reset
            gr.update(interactive=False),    # eval_backend: locked until suggestions exist
            gr.update(interactive=False),    # eval_button: locked until suggestions exist
            "",                              # eval_output reset
        )
 
    if picks_text.strip().lower() == "default":
        queries = DEFAULT_PICKS + [DEFAULT_BENCHMARK]
    else:
        queries = [q.strip() for q in picks_text.split(",") if q.strip()][:5]
 
    progress(0, desc="Resolving picks...")
    picks_info = []
    for i, q in enumerate(queries):
        symbol, note = resolve_pick(UNIVERSE, q)
        if symbol is None:
            continue
        info = fetch_live_info(symbol)
        info["_note"] = note
        picks_info.append(info)
        time.sleep(LIVE_FETCH_DELAY)
        progress((i + 1) / (len(queries) + 1), desc=f"Fetched {symbol}")
 
    exclude = {p["symbol"].upper() for p in picks_info}
    state = {"picks_info": picks_info, "exclude": list(exclude), "mode": None, "all_suggestions": []}
    warning = DATA_LOAD_WARNING if UNIVERSE.empty else ""
    has_picks = bool(picks_info)
 
    return (
        picks_info_to_df(picks_info), state, warning,
        gr.update(visible=has_picks),   # mode_buttons_row: show once picks exist
        empty,                          # suggestions_table reset
        gr.update(interactive=False),   # eval_backend: locked until suggestions exist
        gr.update(interactive=False),   # eval_button: locked until suggestions exist
        "",                             # eval_output reset
    )
 
 
def add_suggestions(mode_choice, state, current_suggestions_df):
    """Step 2: clicking one of the mode buttons (under 'Your Picks') fetches
    5 suggestions using that mode. Clicking again (same or a different mode
    button) fetches 5 more, so the buttons double as the 'rotation' control."""
    picks_info = state.get("picks_info", [])
    if not picks_info:
        return (current_suggestions_df, state,
                gr.update(interactive=False), gr.update(interactive=False))
 
    exclude = set(state.get("exclude", []))
    more = generate_suggestions(mode_choice, picks_info, exclude)
 
    if more:
        exclude.update(s["symbol"].upper() for s in more if s.get("symbol"))
        state["exclude"] = list(exclude)
        state["mode"] = mode_choice
        state["all_suggestions"] = state.get("all_suggestions", []) + list(more)
        new_df = suggestions_to_df(more)
        if current_suggestions_df is None or current_suggestions_df.empty:
            combined = new_df
        else:
            combined = pd.concat([current_suggestions_df, new_df], ignore_index=True)
    else:
        combined = current_suggestions_df
 
    has_suggestions = bool(state.get("all_suggestions"))
    return (
        combined, state,
        gr.update(interactive=has_suggestions),                 # eval_backend
        gr.update(interactive=has_suggestions),                 # eval_button
    )
 
 
# --- UI LAYOUT ---
 
with gr.Blocks(css=fancy_css, title=f"Stock Suggestor — {PRODUCT_NAME}") as demo:
    gr.Markdown(
        "# 📈 Stock Suggestor",
        elem_id="app-title",
    )
 
    gr.Markdown(
        "Pick up to 5 stocks/funds, pull suggestions, then evaluate them with the "
        "Hugging Face API model or the local model.",
        elem_id="app-subtitle",
    )
 
    # Token entry immediately below the header
    with gr.Row():
        hf_token = gr.Textbox(
            label="Hugging Face Token",
            placeholder="hf_...",
            type="password",
            scale=4,
        )
 
        validate_button = gr.Button(
            "Validate Token",
            scale=1,
        )
 
    token_status = gr.Markdown()
 
    with gr.Accordion("Additional inputs", open=False):
        system_message = gr.Textbox(
            value=DEFAULT_SYSTEM_MESSAGE,
            label="System message",
        )
 
        max_tokens = gr.Slider(
            minimum=1,
            maximum=2048,
            value=1200,
            step=1,
            label="Max new tokens",
        )
 
        temperature = gr.Slider(
            minimum=0.1,
            maximum=2.0,
            value=0.7,
            step=0.1,
            label="Temperature",
        )
 
        top_p = gr.Slider(
            minimum=0.1,
            maximum=1.0,
            value=0.95,
            step=0.05,
            label="Top-p (nucleus sampling)",
        )
 
    if DATA_LOAD_WARNING:
        gr.Markdown(f"⚠️ {DATA_LOAD_WARNING}")
 
    with gr.Column(elem_id="chat-container"):
        gr.Markdown(
            "1. Enter up to 5 stocks/funds — tickers, company names, or common nicknames "
            "(e.g. `AAPL, google, tesla, SPY`) — or type **default** for a starter set.\n"
            "2. Once your picks are resolved, choose a matching mode below them to pull "
            "5 suggestions at a time — click any mode button again for 5 more.\n"
            "3. When you're ready, evaluate everything with AI (this ends the round)."
        )
 
        picks_input = gr.Textbox(
            label="Your picks (comma-separated)",
            placeholder="AAPL, google, tesla, SPY, default...",
        )
        resolve_button = gr.Button("Get My Picks", variant="primary")
        status = gr.Markdown()
 
        gr.Markdown("### Your Picks")
        picks_table = gr.Dataframe(
            headers=["Symbol", "Name", "Price", "Beta", "Sector", "Industry", "Match note"],
            max_height=220,  # fixed height -> internally scrollable once it overflows
            wrap=True,
        )
 
        gr.Markdown("### Suggest by (click a mode to add 5 suggestions; click again for 5 more)")
        with gr.Row(visible=False) as mode_buttons_row:
            mode_buttons = {label: gr.Button(label) for label in MODE_LABELS}
 
        gr.Markdown("### Suggestions")
        suggestions_table = gr.Dataframe(
            headers=["Symbol", "Name", "Type", "Sector", "Industry", "Beta"],
            max_height=420,  # scrollable results panel
            wrap=True,
        )
 
        gr.Markdown(
            "### AI Portfolio Evaluation\n"
            "**Auto** (default): tries the Hugging Face API model first and automatically "
            "switches to the local model if the remote call fails for any reason (no token, "
            "timeout, rate limit, API unavailable). It retries the remote model fresh on "
            "every click.\n\n"
            "**Local only / Hugging Face API only**: manual overrides — these do NOT fail over."
        )
        # The evaluation controls are always on the page and just LOCKED (interactive=False)
        # until there is at least one suggestion. (Show/hide toggling of these controls
        # was unreliable in Gradio 6: they only appeared after a later click, or the
        # Evaluate button never appeared at all.) Pulling suggestions repeatedly is the
        # loop; clicking Evaluate -- with whichever model you pick -- is the choice that
        # ends it.
        eval_backend = gr.Radio(
            choices=[
                "Auto (recommended — automatic failover)",
                f"Local only ({LOCAL_MODEL})",
                f"Hugging Face API only ({REMOTE_MODEL})",
            ],
            value="Auto (recommended — automatic failover)",
            label="Run evaluation using",
            interactive=False,
        )
        simulate_remote_failure = gr.Checkbox(
            label="🧪 Simulate a remote-API failure (demo/testing only, forces Auto mode to fail over)",
            value=False,
        )
        eval_button = gr.Button(
            "🤖 Evaluate picks with AI (ends this round)",
            variant="primary",
            interactive=False,
        )
        eval_output = gr.Markdown()
 
        gr.Markdown(
            "Use **Additional inputs** to tune the system message and generation settings "
            "for both the API model and the locally executed model.",
            elem_id="model-note",
        )
 
    session_state = gr.State({})
 
    validate_button.click(
        fn=validate_hf_token,
        inputs=hf_token,
        outputs=token_status,
        api_visibility="private",
    )
 
    resolve_button.click(
        fn=resolve_picks,
        inputs=[picks_input],
        outputs=[picks_table, session_state, status,
                 mode_buttons_row, suggestions_table,
                 eval_backend, eval_button, eval_output],
    )
 
    for label, btn in mode_buttons.items():
        btn.click(
            fn=partial(add_suggestions, label),
            inputs=[session_state, suggestions_table],
            outputs=[suggestions_table, session_state, eval_backend, eval_button],
        )
 
    eval_button.click(
        fn=run_evaluation,
        inputs=[session_state, eval_backend, simulate_remote_failure,
                hf_token, system_message, max_tokens, temperature, top_p],
        outputs=[eval_output, mode_buttons_row, eval_button, eval_backend],
    )
 
 
if __name__ == "__main__":
    start_http_server(8000)
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
    )
 
