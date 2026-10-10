"""Read-only public Polymarket endpoints. No keys, no order endpoints."""
import requests, time, json
S = requests.Session(); S.headers["User-Agent"] = "paper-copy-engine/1.0 (read-only research)"
DATA = "https://data-api.polymarket.com"; GAMMA = "https://gamma-api.polymarket.com"; CLOB = "https://clob.polymarket.com"

def _get(url, params=None, tries=4):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200: return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code in (429, 500, 502, 503, 504): time.sleep(1.5 * (i + 1)); continue
            break
        except Exception as e:
            last = str(e); time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")

class PolymarketAPI:
    def trades(self, user, limit=500):
        return _get(f"{DATA}/trades", dict(user=user, limit=limit, takerOnly="false"))
    def positions(self, user):
        return _get(f"{DATA}/positions", dict(user=user, limit=500, sizeThreshold=0))
    def market(self, condition_id):
        """Gamma market by condition id. Open markets come back by default; closed ones need closed=true."""
        for closed in (None, "true"):
            p = dict(condition_ids=condition_id)
            if closed: p["closed"] = closed
            j = _get(f"{GAMMA}/markets", p)
            if j: return j[0]
        return None
    def event_tags(self, event_slug):
        j = _get(f"{GAMMA}/events", dict(slug=event_slug))
        if not j: return []
        return [t.get("slug") for t in (j[0].get("tags") or []) if t.get("slug")]
    def book(self, token_id):
        j = _get(f"{CLOB}/book", dict(token_id=token_id))
        asks = sorted(((float(a["price"]), float(a["size"])) for a in j.get("asks", [])), key=lambda x: x[0])
        bids = sorted(((float(b["price"]), float(b["size"])) for b in j.get("bids", [])), key=lambda x: -x[0])
        return dict(asks=asks, bids=bids)

    def price_history(self, token_id, start_ts, end_ts, fidelity=60):
        """CLOB trade-price history [(t, p)]; the API rejects windows over ~7 days, so callers keep it <= 7d."""
        j = _get(f"{CLOB}/prices-history", dict(market=token_id, startTs=int(start_ts), endTs=int(end_ts), fidelity=fidelity))
        return [(int(h["t"]), float(h["p"])) for h in j.get("history", [])]
