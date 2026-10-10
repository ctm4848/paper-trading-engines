"""Shared helpers + basket definition for the dividend-growers paper trader (FAKE money only)."""
import time
import pandas as pd
import yfinance as yf

# Streaks = consecutive years of annual dividend increases, as reported by the cited source (checked Oct 3, 2026).
BASKET = [
    {"ticker": "PG",   "name": "Procter & Gamble",        "sector": "Consumer Staples",       "streak": "70",  "source": "Motley Fool Dividend Kings list (updated Oct 2, 2026)"},
    {"ticker": "KO",   "name": "Coca-Cola",               "sector": "Consumer Staples",       "streak": "64",  "source": "Motley Fool Dividend Kings list (updated Oct 2, 2026)"},
    {"ticker": "JNJ",  "name": "Johnson & Johnson",       "sector": "Health Care",            "streak": "64",  "source": "Motley Fool Dividend Kings list (updated Oct 2, 2026)"},
    {"ticker": "LOW",  "name": "Lowe's Companies",        "sector": "Consumer Discretionary (home improvement)", "streak": "55 (lists vary 55-65; Lowe's itself says 'more than 25')", "source": "Motley Fool Kings list; Lowe's May 29, 2026 dividend press release"},
    {"ticker": "SHW",  "name": "Sherwin-Williams",        "sector": "Materials (paints/coatings)", "streak": "48 (Feb 2026 raise 'follows 47 consecutive years')", "source": "Sherwin-Williams press release, Feb 18, 2026"},
    {"ticker": "XOM",  "name": "Exxon Mobil",             "sector": "Energy",                 "streak": "43",  "source": "ExxonMobil Q1-2026 dividend declaration / Motley Fool Aug 25, 2026"},
    {"ticker": "DOV",  "name": "Dover Corp.",             "sector": "Industrials",            "streak": "71",  "source": "Motley Fool Dividend Kings list (updated Oct 2, 2026)"},
    {"ticker": "ED",   "name": "Consolidated Edison",     "sector": "Utilities",              "streak": "52",  "source": "Motley Fool Dividend Kings list (updated Oct 2, 2026)"},
    {"ticker": "FRT",  "name": "Federal Realty Inv. Trust","sector": "Real Estate",           "streak": "59",  "source": "Motley Fool Dividend Kings list (updated Oct 2, 2026)"},
    {"ticker": "CINF", "name": "Cincinnati Financial",    "sector": "Financials",             "streak": "66",  "source": "Motley Fool Dividend Kings list (updated Oct 2, 2026)"},
]
TICKERS = [b["ticker"] for b in BASKET]
BENCH = "SPY"


def _download_one(ticker, start, tries=4):
    for i in range(tries):
        try:
            t = yf.Ticker(ticker)
            h = t.history(start=start, auto_adjust=False, actions=True)
            if h is not None and not h.empty:
                h.index = h.index.tz_localize(None).normalize()
                return h
        except Exception as e:  # network / cache-lock hiccups
            err = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"Could not download {ticker} from Yahoo Finance")


def get_history(tickers, start):
    """Return dict of DataFrames (Close, Adj Close, Dividends, ...) from Yahoo Finance via yfinance."""
    return {t: _download_one(t, start) for t in tickers}


def panel(hist, col):
    return pd.DataFrame({t: h[col] for t, h in hist.items()}).sort_index()


def ttm_yield(h, asof=None):
    """Trailing-12-month cash dividends / latest close."""
    asof = asof or h.index[-1]
    divs = h["Dividends"]
    ttm = divs[(divs.index > asof - pd.Timedelta(days=365)) & (divs.index <= asof)].sum()
    return float(ttm / h["Close"].loc[:asof].iloc[-1])
