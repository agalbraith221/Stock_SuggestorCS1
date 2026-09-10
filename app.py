import os
import sys
import time
from difflib import SequenceMatcher
 
import pandas as pd
from pathlib import Path
import gradio as gr
import spaces
 
try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance is not installed. Run: pip install yfinance --upgrade")
 
from huggingface_hub import InferenceClient
 
 
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
 
# ---------------------------------------------------------------------------
# LLM CONFIG
# One model runs locally on the Space's own compute (transformers pipeline),
# the other is called remotely through the Hugging Face Inference API.
# ---------------------------------------------------------------------------
LOCAL_MODEL = "Qwen/Qwen3-0.6B"
REMOTE_MODEL = "openai/gpt-oss-20b"
 
_local_pipe = None  # lazy-loaded on first use so app startup stays fast
 
 
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
 
 
@spaces.GPU
def _gpu_startup_stub():
    return True
 
 
# ------------------------------ DATA LOADING -------------------------------
 
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
 
 
# MATCHING
 
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
    note = "" if len(matches) == 1 else f"multiple matches — auto-picked best match"
    return chosen["symbol_clean"], note
 
 
# LIVE DATA:
 
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
 
 
# SUGGESTION ENGINE:
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
 
    # group_key can be None -> "pure" mode with no sector/industry grouping,
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
 
 
# (group_key, broad_funds, use_beta_filter)
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
 
 
# FORMATTING
 
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
 
 
# ---------------------------------------------------------------------------
# LLM EVALUATION (deliverable 1: remote HF API call / deliverable 2: local model)
# ---------------------------------------------------------------------------
 
def _format_symbol_list(items):
    lines = []
    for it in items:
        beta = it.get("beta")
        beta_str = f"{beta:.2f}" if beta is not None else "N/A"
        kind = it.get("type", "Company")
        sector = it.get("sector") or "N/A"
        industry = it.get("industry_used") or it.get("industry") or "N/A"
        lines.append(
            f"- {it.get('symbol', '?')} ({it.get('name', '?')}) | {kind} | "
            f"sector={sector} | industry={industry} | beta={beta_str}"
        )
    return "\n".join(lines) if lines else "(none)"
 
 
def build_evaluation_prompt(picks_info, all_suggestions, mode_choice) -> str:
    picks_block = _format_symbol_list(picks_info)
    suggestions_block = _format_symbol_list(all_suggestions)
    return (
        "You are a cautious financial-education assistant, not a licensed financial "
        "advisor. Do not give definitive buy/sell instructions.\n\n"
        "A user picked the following stocks/funds:\n"
        f"{picks_block}\n\n"
        f"Using the '{mode_choice}' matching mode, the app suggested these additional "
        f"stocks/funds:\n{suggestions_block}\n\n"
        "In 4-6 short bullet points, evaluate the suggestions as potential additions to "
        "a long-term investment portfolio. Comment on diversification, sector/industry "
        "concentration, and relative risk (beta) versus the original picks. Call out which "
        "suggestions look strongest and which look weakest, and why. End with one sentence "
        "reminding the user this is general educational information, not financial advice."
    )
 
 
def get_local_pipeline():
    """Lazily load the local transformers pipeline the first time it's used."""
    global _local_pipe
    if _local_pipe is None:
        from transformers import pipeline
        import torch
        _local_pipe = pipeline(
            "text-generation",
            model=LOCAL_MODEL,
            dtype="auto",
            device="cuda" if torch.cuda.is_available() else "cpu",
        )
    return _local_pipe
 
 
@spaces.GPU(duration=60)
def evaluate_locally(prompt: str) -> str:
    """Run the evaluation on the Space's own (ZeroGPU) compute — no remote API call."""
    pipe = get_local_pipeline()
    messages = [{"role": "user", "content": prompt}]
    output = pipe(messages, max_new_tokens=500, do_sample=True, temperature=0.7)
    generated = output[0]["generated_text"]
    if isinstance(generated, list) and generated:
        return generated[-1].get("content", "").strip()
    return str(generated).strip()
 
 
def evaluate_remotely(prompt: str) -> str:
    """Call a hosted LLM through the Hugging Face Inference API."""
    token = os.environ.get("HF_TOKEN")
    client = InferenceClient(model=REMOTE_MODEL, token=token)
    completion = client.chat_completion(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.7,
    )
    return completion.choices[0].message.content.strip()
 
 
def run_evaluation(state, backend_choice):
    picks_info = state.get("picks_info", [])
    all_suggestions = state.get("all_suggestions", [])
    mode_choice = state.get("mode", "")
 
    if not picks_info or not all_suggestions:
        return "Get some suggestions first, then evaluate them.", gr.update(), gr.update(), gr.update()
 
    prompt = build_evaluation_prompt(picks_info, all_suggestions, mode_choice)
 
    try:
        if backend_choice.startswith("Local"):
            evaluation = evaluate_locally(prompt)
        else:
            evaluation = evaluate_remotely(prompt)
        evaluation = f"**Model used:** {backend_choice}\n\n{evaluation}"
    except Exception as e:
        evaluation = (
            f"⚠️ Evaluation failed using {backend_choice}: {e}\n\n"
            "If you picked the Hugging Face API option, make sure this Space has an "
            "`HF_TOKEN` secret configured with Inference Providers access. If you picked "
            "Local, make sure `transformers`/`torch` are installed and there's enough "
            "memory/GPU available."
        )
 
    # Evaluation ends the suggestion loop: lock further "show more" / re-evaluation.
    return (
        evaluation,
        gr.update(interactive=False),  # more_button
        gr.update(interactive=False),  # eval_button
        gr.update(interactive=False),  # eval_backend
    )
 
 
# GRADIO CALLBACKS -----------------------------
 
def run_search(picks_text, mode_choice, progress=gr.Progress()):
    if not picks_text or not picks_text.strip():
        empty = pd.DataFrame()
        state = {"picks_info": [], "exclude": [], "mode": mode_choice, "all_suggestions": []}
        return (
            empty, empty, state, DATA_LOAD_WARNING,
            gr.update(visible=False),                       # eval_backend
            gr.update(visible=False, interactive=True),      # eval_button
            gr.update(interactive=True),                     # more_button
            "",                                              # eval_output
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
 
    progress(0.6, desc="Finding suggestions...")
    suggestions = generate_suggestions(mode_choice, picks_info, exclude)
    exclude.update(s["symbol"].upper() for s in suggestions if s.get("symbol"))
 
    state = {
        "picks_info": picks_info,
        "exclude": list(exclude),
        "mode": mode_choice,
        "all_suggestions": list(suggestions),
    }
    warning = DATA_LOAD_WARNING if UNIVERSE.empty else ""
    has_suggestions = bool(suggestions)
    return (
        picks_info_to_df(picks_info), suggestions_to_df(suggestions), state, warning,
        gr.update(visible=has_suggestions),                     # eval_backend
        gr.update(visible=has_suggestions, interactive=True),   # eval_button
        gr.update(interactive=True),                            # more_button
        "",                                                     # eval_output
    )
 
 
def run_more(state, mode_choice, current_suggestions_df):
    """Fetch another 5 suggestions. Reads the mode radio fresh each click, so the
    matching criteria can be changed on every rotation."""
    picks_info = state.get("picks_info", [])
    exclude = set(state.get("exclude", []))
 
    if not picks_info:
        return current_suggestions_df, state
 
    more = generate_suggestions(mode_choice, picks_info, exclude)
    if not more:
        return current_suggestions_df, state
 
    exclude.update(s["symbol"].upper() for s in more if s.get("symbol"))
    state["exclude"] = list(exclude)
    state["mode"] = mode_choice  # remember most recent mode for the AI evaluation prompt
    state["all_suggestions"] = state.get("all_suggestions", []) + list(more)
 
    new_df = suggestions_to_df(more)
    combined = pd.concat([current_suggestions_df, new_df], ignore_index=True)
    return combined, state
 
 
# UI LAYOUT ---------------------
 
with gr.Blocks(title="Stock Suggestor") as demo:
    gr.Markdown(
        "# 📈 Stock Suggestor\n"
        "Enter up to 5 stocks/funds — tickers, company names, or common nicknames "
        "(e.g. `AAPL, google, tesla, SPY`) — or type **default** for a starter set "
        "(Apple, Google, Nvidia, Tesla + S&P 500). Pick a matching mode below — you "
        "can change it before each new batch of suggestions."
    )
    if DATA_LOAD_WARNING:
        gr.Markdown(f"⚠️ {DATA_LOAD_WARNING}")
 
    with gr.Row():
        picks_input = gr.Textbox(
            label="Your picks (comma-separated)",
            placeholder="AAPL, google, tesla, SPY, default...",
            scale=3,
        )
        mode_input = gr.Radio(
            choices=list(MODE_LABELS.keys()),
            value="Industry",
            label="Suggest by",
            scale=2,
        )
 
    search_button = gr.Button("Get Suggestions", variant="primary")
    status = gr.Markdown()
 
    gr.Markdown("### Your Picks")
    picks_table = gr.Dataframe(
        headers=["Symbol", "Name", "Price", "Beta", "Sector", "Industry", "Match note"],
        max_height=220,  # fixed height -> internally scrollable once it overflows
        wrap=True,
    )
 
    gr.Markdown("### Suggestions (scroll for more)")
    suggestions_table = gr.Dataframe(
        headers=["Symbol", "Name", "Type", "Sector", "Industry", "Beta"],
        max_height=420,  # scrollable results panel
        wrap=True,
    )
 
    more_button = gr.Button("Show 5 More Suggestions")
 
    gr.Markdown("### AI Portfolio Evaluation")
    eval_backend = gr.Radio(
        choices=[f"Local ({LOCAL_MODEL})", f"Hugging Face API ({REMOTE_MODEL})"],
        value=f"Local ({LOCAL_MODEL})",
        label="Run evaluation using",
        visible=False,
    )
    eval_button = gr.Button("🤖 Evaluate picks with AI (ends suggestion loop)", visible=False)
    eval_output = gr.Markdown()
 
    session_state = gr.State({})
 
    search_button.click(
        fn=run_search,
        inputs=[picks_input, mode_input],
        outputs=[picks_table, suggestions_table, session_state, status,
                 eval_backend, eval_button, more_button, eval_output],
    )
    more_button.click(
        fn=run_more,
        inputs=[session_state, mode_input, suggestions_table],
        outputs=[suggestions_table, session_state],
    )
    eval_button.click(
        fn=run_evaluation,
        inputs=[session_state, eval_backend],
        outputs=[eval_output, more_button, eval_button, eval_backend],
    )
 
if __name__ == "__main__":
    demo.launch()
