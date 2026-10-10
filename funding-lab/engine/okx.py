"""Read-only OKX public market data (no keys, no trading endpoints)."""
import time, requests
BASE = "https://www.okx.com"
UA = {"User-Agent": "FundingLab-paper-engine/1.0"}

class DataError(Exception): pass

def get(path, params, tries=5):
    last=None
    for k in range(tries):
        try:
            r = requests.get(BASE+path, params=params, headers=UA, timeout=20)
            j = r.json()
            if j.get("code") == "0": return j["data"]
            last = f"{r.status_code} {j.get('code')} {j.get('msg')}"
        except Exception as e:
            last = repr(e)
        time.sleep(1.0 + k)
    raise DataError(f"OKX {path} {params} failed: {last}")

def funding_history(inst, until_ms, pages=2):
    """Settled funding prints with fundingTime <= until_ms, newest pages (100/page)."""
    out = {}; after = None
    for _ in range(pages + 3):
        p = {"instId": inst, "limit": 100}
        if after: p["after"] = after
        d = get("/api/v5/public/funding-rate-history", p)
        if not d: break
        for x in d:
            T = int(x["fundingTime"])
            if T <= until_ms: out[T] = float(x.get("realizedRate") or x["fundingRate"])
        after = d[-1]["fundingTime"]
        if len(out) >= pages*100: break
        time.sleep(0.15)
    return sorted(out.items())

def candles(inst, bar, start_ms, end_ms, index=False):
    """Bars with open ts in [start_ms, end_ms], oldest->newest: (ts,o,h,l,c,confirm)."""
    path = "/api/v5/market/history-index-candles" if index else "/api/v5/market/history-candles"
    rows = {}; after = end_ms + 1
    while True:
        d = get(path, {"instId": inst, "bar": bar, "after": after, "limit": 100})
        if not d: break
        for x in d:
            ts = int(x[0])
            if start_ms <= ts <= end_ms:
                conf = x[-1] if not index else x[5]
                rows[ts] = (ts, float(x[1]), float(x[2]), float(x[3]), float(x[4]), str(conf))
        oldest = int(d[-1][0])
        if oldest <= start_ms or oldest >= after: break
        after = oldest; time.sleep(0.11)
    return [rows[k] for k in sorted(rows)]

def usdt_usd(at_ms):
    """USDT price in USD from OKX. Primary: OKX index USDT-USD (5m index candle close at/before at_ms).
    Secondary: 1 / USDC-USDT spot close (assumes USDC=$1). Returns dict."""
    out = {}
    try:
        idx = candles("USDT-USD", "5m", at_ms - 30*60_000, at_ms, index=True)
        if idx: out["index_usdt_usd"] = idx[-1][4]; out["index_ts"] = idx[-1][0]
    except DataError as e: out["index_error"] = str(e)
    try:
        c = candles("USDC-USDT", "5m", at_ms - 30*60_000, at_ms)
        if c: out["usdc_usdt"] = c[-1][4]; out["implied_usdt_usd"] = 1.0 / c[-1][4]
    except DataError as e: out["cross_error"] = str(e)
    out["usdt_usd"] = out.get("index_usdt_usd", out.get("implied_usdt_usd"))
    return out
