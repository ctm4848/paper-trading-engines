"""Kalshi taker fee. Fee schedule (eff. 2026-07-07, verified 2026-10-09): fee = round up(M x 0.07 x C x P x (1-P)),
M = series fee_multiplier (KXBTC15M: 1, fee_type quadratic). Rounding (docs.kalshi.com/getting_started/fee_rounding):
cost + fee is rounded UP to the member's balance precision, $0.01 for non-direct members (a retail account like
the owner's), so we round cost+fee up to the cent per order. Conservative vs the $0.0001 direct-member rule."""
import math
TAKER = 0.07
def raw_fee(fills, mult=1.0): return sum(mult * TAKER * c * p * (1 - p) for p, c in fills)
def order_cost(fills, mult=1.0, precision=0.01):
    """fills [(price, contracts)] -> (notional, fee, total) with total rounded up to `precision`."""
    notional = sum(p * c for p, c in fills)
    total = math.ceil(round((notional + raw_fee(fills, mult)) / precision, 6)) * precision
    return notional, round(total - notional, 6), round(total, 6)
def fee_per_contract(price, mult=1.0): return mult * TAKER * price * (1 - price)
