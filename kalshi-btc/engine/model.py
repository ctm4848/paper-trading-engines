"""P(Up) model for Kalshi KXBTC15M ('BTC price up in next 15 mins?').
Settlement: YES if the BRTI 60-second average just before close >= floor_strike (the BRTI 60s average just before open).
Model: log-price is a driftless Brownian motion with per-second vol sigma. With tau seconds left (tau >= 60) the
settlement average A = mean of the path over the final 60 s, so ln(A/S) ~ N(0, sigma^2 * (tau - 60 + 60/3)) =
N(0, sigma^2 * (tau - 40)). We add proxy error b (our 4-exchange median is not BRTI). P(Up) = Phi(ln(S/K) / sd)."""
import math
VOL_FLOOR_PER_MIN = 0.0003      # never assume less than 3 bp/min realized vol (avoids overconfidence in quiet minutes)
PROXY_ERR = 0.00015             # ~1.5 bp (~$12 at $82k) one-sigma proxy-vs-BRTI error
P_CLAMP = (0.01, 0.99)

def phi(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def realized_vol_per_min(closes):
    r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(r) < 10: return VOL_FLOOR_PER_MIN
    m = sum(r) / len(r); sd = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
    return max(sd, VOL_FLOOR_PER_MIN)

def p_up(spot, strike, tau_s, vol_per_min):
    sig_s = vol_per_min / math.sqrt(60)
    eff = max(tau_s - 40, 1.0) if tau_s >= 60 else tau_s ** 2 / 180.0   # (tau<60 never traded; partial-average approx)
    sd = math.sqrt(sig_s ** 2 * eff + PROXY_ERR ** 2)
    p = phi(math.log(spot / strike) / sd)
    return min(max(p, P_CLAMP[0]), P_CLAMP[1])
