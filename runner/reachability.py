#!/usr/bin/env python3
"""Check that the public data endpoints the engines use are reachable from this machine. Writes runner/reachability.json."""
import json, os, time, requests
U = {"User-Agent": "paper-engines-reachability/1.0"}
CHECKS = {
  "okx_funding": "https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=1",
  "okx_candles": "https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT&bar=5m&limit=1",
  "okx_index": "https://www.okx.com/api/v5/market/history-index-candles?instId=USDT-USD&bar=5m&limit=1",
  "okx_announcements": "https://www.okx.com/api/v5/support/announcements",
  "kalshi_markets": "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=1",
  "polymarket_data": "https://data-api.polymarket.com/trades?limit=1",
  "polymarket_gamma": "https://gamma-api.polymarket.com/markets?limit=1",
  "polymarket_clob": "https://clob.polymarket.com/time",
  "coinbase": "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
  "kraken": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
  "bitstamp": "https://www.bitstamp.net/api/v2/ticker/btcusd/",
  "gemini": "https://api.gemini.com/v1/pubticker/btcusd",
  "rss_coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss",
  "rss_theblock": "https://www.theblock.co/rss.xml",
  "rss_cointelegraph": "https://cointelegraph.com/rss",
  "rss_sec": "https://www.sec.gov/news/pressreleases.rss",
  "binance_info_only": "https://api.binance.com/api/v3/ping",
  "bybit_info_only": "https://api.bybit.com/v5/market/time",
}
out = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": {}}
for k, u in CHECKS.items():
    try:
        r = requests.get(u, headers=U, timeout=15); body = r.text[:120].replace("\n", " ")
        ok = r.status_code == 200
        if k.startswith("okx") and ok:
            try: ok = r.json().get("code") == "0"
            except Exception: ok = False
        out["results"][k] = {"ok": ok, "status": r.status_code, "snippet": body if not ok else ""}
    except Exception as e:
        out["results"][k] = {"ok": False, "error": type(e).__name__ + ": " + str(e)[:120]}
    print(f"{k:22s} {'OK ' if out['results'][k]['ok'] else 'FAIL'} {out['results'][k].get('status', out['results'][k].get('error'))}")
json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "reachability.json"), "w"), indent=1)
