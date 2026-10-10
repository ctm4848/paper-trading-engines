"""Read-only public data: Kalshi trade-api v2 (no auth, no order endpoints) and public spot tickers."""
import requests, time, statistics, math
S = requests.Session(); S.headers["User-Agent"] = "paper-kalshi-btc/1.0 (read-only research)"
K = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"

def _get(url, params=None, tries=3, timeout=10):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 200: return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code in (429, 500, 502, 503, 504): time.sleep(1 + i); continue
            break
        except Exception as e: last = str(e); time.sleep(1 + i)
    raise RuntimeError(f"GET {url} failed: {last}")

class KalshiAPI:
    def open_markets(self):
        return _get(f"{K}/markets", dict(series_ticker=SERIES, status="open", limit=20)).get("markets", [])
    def market(self, ticker):
        return _get(f"{K}/markets/{ticker}").get("market")
    def orderbook(self, ticker):
        """Kalshi books list BIDS only. YES asks are implied by NO bids: ask_yes = 1 - no_bid (same size)."""
        ob = _get(f"{K}/markets/{ticker}/orderbook").get("orderbook_fp") or {}
        yes_b = [(float(p), float(q)) for p, q in (ob.get("yes_dollars") or [])]
        no_b = [(float(p), float(q)) for p, q in (ob.get("no_dollars") or [])]
        asks_yes = sorted(((round(1 - p, 4), q) for p, q in no_b), key=lambda x: x[0])
        asks_no = sorted(((round(1 - p, 4), q) for p, q in yes_b), key=lambda x: x[0])
        return dict(yes=asks_yes, no=asks_no)

# --- spot proxy for CF Benchmarks BRTI (constituent exchanges with free public tickers)
def _coinbase():
    return float(_get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", tries=1, timeout=5)["price"])
def _kraken():
    return float(_get("https://api.kraken.com/0/public/Ticker", dict(pair="XBTUSD"), tries=1, timeout=5)["result"]["XXBTZUSD"]["c"][0])
def _bitstamp():
    return float(_get("https://www.bitstamp.net/api/v2/ticker/btcusd/", tries=1, timeout=5)["last"])
def _gemini():
    return float(_get("https://api.gemini.com/v1/pubticker/btcusd", tries=1, timeout=5)["last"])
SOURCES = {"coinbase": _coinbase, "kraken": _kraken, "bitstamp": _bitstamp, "gemini": _gemini}

class SpotAPI:
    def snapshot(self):
        """One proxy reading: median of available constituent last prices (needs >= 2, must include Coinbase or Kraken)."""
        px = {}
        for k, f in SOURCES.items():
            try: px[k] = f()
            except Exception: pass
        if len(px) < 2 or not ({"coinbase", "kraken"} & set(px)): raise RuntimeError(f"spot proxy unavailable: {sorted(px)}")
        return statistics.median(px.values()), px
    def minute_closes(self, n=61):
        """Coinbase BTC-USD 1-minute closes, oldest first (for realized vol)."""
        j = _get("https://api.exchange.coinbase.com/products/BTC-USD/candles", dict(granularity=60), timeout=8)
        rows = sorted(j)[-n:]
        return [r[4] for r in rows]
