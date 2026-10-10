"""Shared Funding Lab rules (paper only). Used by the backtest and the live engine.
All funding rates are decimals per 8h (0.00008 = 0.008%/8h). Times are UTC.
"""
import numpy as np, pandas as pd

FUND_FLOOR = 0.00008          # 0.008%/8h
FUND_EXIT  = 0.00003          # 0.003%/8h
BOOKB_ENTRY = 0.00006         # 0.006%/8h
BOOKB_EXIT  = 0.00003
PERP_FEE = 0.0005             # 0.05% taker
SPOT_FEE = 0.0010             # 0.10% taker
SLIP = 0.0002                 # 2 bps
SHOCK_SLIP = 0.0030           # 30 bps upper end of 10-30 bps shock-day slippage
RISK_PCT = 0.01
ATR_MULT = 2.0
STOP_CAP = 0.05
LEV_PER_COIN = 1.0
LEV_TOTAL = 1.5
BOOKB_BUFFER = 0.5            # margin buffer = 50% of notional -> notional = equity/1.5
REHEDGE_BAND = 0.02
MAX_HOLD_BARS = 42            # 7 days of 4h bars
DD_KILL = 0.15

def trailing24(rate):
    return rate.rolling(3, min_periods=3).mean()

def p90_30d(tr24):
    """90th percentile of the PRIOR 90 prints of the trailing-24h series (no lookahead)."""
    return tr24.shift(1).rolling(90, min_periods=90).quantile(0.9)

def atr14_4h(high, low, close):
    prev = close.shift(1)
    tr = pd.concat([high-low, (high-prev).abs(), (low-prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()

def ema20(close):
    return close.ewm(span=20, adjust=False, min_periods=20).mean()

def funding_events_in(f_index, t0, t1):
    """funding timestamps strictly after t0 and up to t1 (inclusive)."""
    return f_index[(f_index > t0) & (f_index <= t1)]

# News shock: pause NEW Book B entries for 24h only. Book B fully unwinds ONLY on USDT < $0.99 (depeg rule).
BOOKB_UNWIND_ON_SHOCK = False
BOOKB_SHOCK_PAUSE_MS = 24*3600_000
DEPEG_LEVEL = 0.99
