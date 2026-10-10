"""Polymarket copy sleeve rules (PAPER ONLY). Pure logic; all I/O goes through an injected api object."""
import json, time, math
from filters_classify import classify

TRADERS = {
    "0x944e0472b99926f4f712fc97dc0a846a8c5d8c94": "mountaindewd",
    "0x0ffcad4963e25dc150774363f4061ebe840c8236": "perfectnostalgia",
    "0x2b9dbf4b6e0e11309a9d6d2a09b72f65f652adc0": "seal7",
}
START_CASH = 5000.0
PER_ENTRY = 75.0            # 1.5% of starting equity per new market per trader
PER_MARKET_CAP = 150.0      # across traders
MAX_DEPLOYED = 0.40         # of current equity (cost basis of open lots)
# FIX 3 (Oct 9, 2026, KPI plan): "DELAYED COPY" test. The box only runs during agent wakes (hourly :07 routine), so a
# trade can wait ~60-67 min before we first see it. 120 min = one full missed/late wake of margin. Fills still use the
# REAL book at our actual fill time with the 3c slippage cap and the taker fee.
MAX_LAG_S = 7200            # copy only BUYs we see within 120 min of their trade
MIN_TIME_LEFT_S = 1800      # market must have >= 30 min left
MAX_SLIP = 0.03             # our VWAP at most 3c worse than their price
MAX_PRICE = 0.97
MIN_FILL_USD = 10.0
CASH_APY = 0.04
CLV_LOOKBACK_S = 7 * 86400  # prices-history window before the resolution reference time (API max ~7 days)
CLV_MAX_TRIES = 24
RESOLVED_LOTS_NEEDED = 40
# skip reasons that mean "the signal passed our market filters but we failed to copy it" (capture-rate denominator)
CAPTURABLE_MISS = {"seen_too_late", "slippage_over_3c", "price_above_97c", "no_depth", "cap_reached", "book_failed"}
LOG_SKIPS_WITHIN_S = 86400  # older unseen trades are marked seen silently (first run / long outages)
# Gamma tag slugs treated as sports/esports (always skipped)
SPORT_TAGS = {"sports","soccer","nba","nfl","nhl","mlb","tennis","golf","esports","ufc","mma","cricket","f1","formula-1",
              "basketball","football","hockey","baseball","boxing","games","epl","ucl","world-cup","ncaab","ncaaf","cfb","cbb",
              "counter-strike","cs2","dota-2","league-of-legends","valorant","olympics","rugby","nascar","wnba","mls","racing","chess"}
# Verified fee rates (docs.polymarket.com/trading/fees, checked 2026-10-09) used only if a market lacks feeSchedule.
CATEGORY_FEE = {"crypto": .07, "sports": .05, "finance": .04, "politics": .04, "economics": .05, "culture": .05,
                "weather": .05, "mentions": .04, "tech": .04, "geopolitics": 0.0}
DEFAULT_FEE = 0.05

def fee_rate(market, tags=()):
    if market is not None:
        if market.get("feesEnabled") is False: return 0.0
        fs = market.get("feeSchedule") or {}
        if fs.get("rate") is not None: return float(fs["rate"])
    for t in tags:
        if t in CATEGORY_FEE: return CATEGORY_FEE[t]
    return DEFAULT_FEE

def taker_fee(shares, price, rate):
    """Polymarket taker fee in USDC: C x feeRate x p x (1-p), rounded to 5 dp."""
    return round(shares * rate * price * (1 - price), 5)

def walk_asks(asks, budget, rate):
    """Buy up to `budget` USDC (fees included) walking asks low->high. Returns shares, notional, fee."""
    sh = notional = fee = 0.0; left = budget
    for p, sz in asks:
        unit = p + rate * p * (1 - p)
        q = min(sz, left / unit)
        if q <= 1e-9: break
        sh += q; notional += q * p; fee += rate * q * p * (1 - p); left -= q * unit
        if left <= 1e-6: break
    return sh, notional, round(fee, 5)

def walk_bids(bids, shares, rate):
    """Sell `shares` walking bids high->low. Returns shares_sold, gross, fee."""
    sold = gross = fee = 0.0
    for p, sz in bids:
        q = min(sz, shares - sold)
        if q <= 1e-9: break
        sold += q; gross += q * p; fee += rate * q * p * (1 - p)
        if shares - sold <= 1e-9: break
    return sold, gross, round(fee, 5)

def is_sports(title, slug, tags):
    return classify(title or "", slug or "") == "sports" or bool(SPORT_TAGS & set(tags or []))

def trade_key(t):
    return f"{t.get('transactionHash')}:{t.get('asset')}:{t.get('side')}:{t.get('size')}:{t.get('price')}:{t.get('timestamp')}"

def iso_ts(s):
    import datetime as dt
    if not s: return None
    s = s.strip().replace(" ", "T").replace("Z", "+00:00")
    if s.endswith("+00"): s += ":00"
    try:
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except Exception: return None

def new_state(now):
    return {"version": 1, "start_ts": now, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "cash": START_CASH, "seen": {w: {} for w in TRADERS}, "entries": {}, "lots": {}, "runs": 0, "last_run": None,
            "tag_cache": {}}

def token_mark(market, asset):
    try:
        toks = json.loads(market["clobTokenIds"]); px = json.loads(market["outcomePrices"])
        return float(px[toks.index(asset)])
    except Exception:
        return None

class CopyEngine:
    def __init__(self, state, api, now):
        self.s = state; self.api = api; self.now = now; self.events = []; self.alerts = []
        self.mcache = {}

    def emit(self, eid, typ, **kw):
        self.events.append({"id": eid, "type": typ, "ts": self.now, **kw})

    def market(self, cid):
        if cid not in self.mcache: self.mcache[cid] = self.api.market(cid)
        return self.mcache[cid]

    def tags(self, event_slug):
        c = self.s.setdefault("tag_cache", {})
        if event_slug not in c: c[event_slug] = self.api.event_tags(event_slug)
        return c[event_slug]

    # ---- accounting
    def open_lots(self): return [l for l in self.s["lots"].values() if l["status"] == "open"]
    def deployed(self): return sum(l["cost"] for l in self.open_lots())
    def market_cost(self, cid): return sum(l["cost"] for l in self.open_lots() if l["cid"] == cid)
    def equity(self, marks=None):
        v = self.s["cash"]
        for l in self.open_lots():
            m = (marks or {}).get(l["asset"]); v += l["shares"] * (m if m is not None else l["cost"] / max(l["shares"], 1e-9))
        return v

    # ---- main
    def run(self):
        s = self.s
        for w, name in TRADERS.items():
            fails = s.setdefault("fail_streak", {})
            try: trades = self.api.trades(w); fails[w] = 0
            except Exception as e:
                fails[w] = fails.get(w, 0) + 1
                self.emit(f"fetchfail:{w}:{self.now}", "fetch_error", trader=name, error=str(e)[:200])
                if fails[w] >= 3:   # alert only on a persistent outage, not one transient blip
                    self.alerts.append({"type": "fetch_error", "key": f"trades:{w}:{self.now//3600}", "ts": self.now, "error": str(e)[:300]})
                continue
            self.recent_trades = trades
            seen = s["seen"].setdefault(w, {})
            new = [t for t in trades if trade_key(t) not in seen]
            new.sort(key=lambda t: t["timestamp"])
            sells = {}
            for t in new:
                k = trade_key(t); seen[k] = t["timestamp"]
                age = self.now - t["timestamp"]
                if age > LOG_SKIPS_WITHIN_S: continue      # history from before our window: mark seen, never act
                if t["side"] == "BUY":
                    if self.consider_buy(w, name, t, k, age) == "retry" and age <= MAX_LAG_S:
                        del seen[k]          # transient lookup failure: try again next poll while still inside 60 min
                elif t["side"] == "SELL":
                    lid = s["entries"].get(f"{w}:{t['conditionId']}")
                    if lid and s["lots"][lid]["status"] == "open" and s["lots"][lid]["asset"] == t["asset"] and t["timestamp"] >= s["lots"][lid]["their_ts"]:
                        sells.setdefault(lid, []).append(t)
            if sells: self.mirror_sells(w, sells)
            cut = self.now - 7 * 86400
            s["seen"][w] = {k: v for k, v in seen.items() if v >= cut}
        self.retry_pending_sells()
        self.settle()
        s["runs"] += 1; s["last_run"] = self.now
        return self

    def skip(self, k, w, name, t, reason, **kw):
        self.emit(f"skip:{k}", "skip", trade_key=trade_key(t), trader=name, wallet=w, cid=t.get("conditionId"), title=(t.get("title") or "")[:90],
                  their_price=t.get("price"), their_ts=t.get("timestamp"), reason=reason, **kw)

    def consider_buy(self, w, name, t, k, age):
        s = self.s; cid = t["conditionId"]
        if age > MAX_LAG_S: return self.skip(k, w, name, t, "seen_too_late", lag_s=age)
        if f"{w}:{cid}" in s["entries"]: return self.skip(k, w, name, t, "already_copied_this_trader_market")
        try:
            tags = self.tags(t.get("eventSlug") or t.get("slug"))
        except Exception as e:
            self.skip(k + ":" + str(self.now), w, name, t, "tag_lookup_failed", error=str(e)[:120]); return "retry"
        if is_sports(t.get("title"), t.get("slug"), tags): return self.skip(k, w, name, t, "sports_or_esports", tags=tags[:8])
        try: m = self.market(cid)
        except Exception as e: self.skip(k + ":" + str(self.now), w, name, t, "market_lookup_failed", error=str(e)[:120]); return "retry"
        if not m: return self.skip(k, w, name, t, "market_not_found")
        end = iso_ts(m.get("endDate"))
        if m.get("closed") or m.get("acceptingOrders") is False or not m.get("active", True):
            return self.skip(k, w, name, t, "market_closed")
        if end is None or end - self.now < MIN_TIME_LEFT_S:
            return self.skip(k, w, name, t, "under_30min_left", end=m.get("endDate"))
        rate = fee_rate(m, tags)
        eq = self.equity()
        budget = min(PER_ENTRY, PER_MARKET_CAP - self.market_cost(cid), MAX_DEPLOYED * eq - self.deployed(), s["cash"])
        if budget < MIN_FILL_USD: return self.skip(k, w, name, t, "cap_reached", budget=round(budget, 2))
        try: book = self.api.book(t["asset"])
        except Exception as e: self.skip(k + ":" + str(self.now), w, name, t, "book_failed", error=str(e)[:120]); return "retry"
        sh, notional, fee = walk_asks(book["asks"], budget, rate)
        if sh <= 0 or notional + fee < MIN_FILL_USD: return self.skip(k, w, name, t, "no_depth", best_ask=book["asks"][0][0] if book["asks"] else None)
        vwap = notional / sh
        if vwap > MAX_PRICE: return self.skip(k, w, name, t, "price_above_97c", vwap=round(vwap, 4))
        if vwap > t["price"] + MAX_SLIP: return self.skip(k, w, name, t, "slippage_over_3c", vwap=round(vwap, 4))
        cost = notional + fee
        lid = f"lot:{w}:{cid}"
        s["cash"] -= cost
        s["entries"][f"{w}:{cid}"] = lid
        s["lots"][lid] = dict(id=lid, trader=name, wallet=w, cid=cid, asset=t["asset"], outcome=t.get("outcome"), title=(t.get("title") or "")[:120],
                              slug=t.get("slug"), shares=sh, shares0=sh, cost=cost, cost0=cost, fees=fee, fee_rate=rate, entry_ts=self.now,
                              their_price=t["price"], their_ts=t["timestamp"], their_size=t["size"], vwap=vwap, lag_s=self.now - t["timestamp"],
                              slippage=vwap - t["price"], trade_key=k, status="open", proceeds=0.0, pending_sell=0.0, end=m.get("endDate"))
        self.emit(f"fill:{lid}", "fill", lot=lid, trader=name, wallet=w, title=(t.get("title") or "")[:90], outcome=t.get("outcome"), shares=round(sh, 4),
                  vwap=round(vwap, 4), fee=fee, cost=round(cost, 4), their_price=t["price"], leader_price=t["price"], our_price=round(vwap, 4),
                  our_all_in=round(cost / sh, 4), lag_s=self.now - t["timestamp"], slippage=round(vwap - t["price"], 4), fee_rate=rate,
                  best_ask=book["asks"][0][0], their_ts=t["timestamp"], trade_key=k)

    def mirror_sells(self, w, sells):
        try: pos = {p["asset"]: float(p.get("size") or 0) for p in self.api.positions(w)}
        except Exception: pos = None
        hist = getattr(self, "recent_trades", []) or []
        for lid, ts in sells.items():
            l = self.s["lots"][lid]; sold = sum(float(t["size"]) for t in ts)
            if pos is not None: rem = pos.get(l["asset"], 0.0)
            else:   # fallback: their net position from the trades we can see
                rem = sum((1 if t["side"] == "BUY" else -1) * float(t["size"]) for t in hist if t["asset"] == l["asset"])
                rem = max(0.0, rem)
            frac = min(1.0, sold / (sold + rem)) if sold + rem > 0 else 1.0
            q = l["shares"] * frac
            l["pending_sell"] = min(l["shares"], l.get("pending_sell", 0.0) + q)
            self.emit(f"sellsignal:{lid}:{ts[-1]['timestamp']}:{len(ts)}", "sell_signal", lot=lid, their_sold=sold, frac=round(frac, 4), our_shares=round(q, 4))

    def retry_pending_sells(self):
        for l in self.open_lots():
            q = l.get("pending_sell", 0.0)
            if q <= 1e-9: continue
            try: book = self.api.book(l["asset"])
            except Exception as e:
                self.emit(f"sellfail:{l['id']}:{self.now//300}", "sell_deferred", lot=l["id"], error=str(e)[:120]); continue
            sold, gross, fee = walk_bids(book["bids"], q, l["fee_rate"])
            if sold <= 1e-9:
                self.emit(f"sellfail:{l['id']}:{self.now//300}", "sell_deferred", lot=l["id"], reason="no_bids"); continue
            basis = l["cost"] * sold / l["shares"]; proceeds = gross - fee
            self.s["cash"] += proceeds; l["proceeds"] += proceeds; l["cost"] -= basis; l["shares"] -= sold; l["pending_sell"] = max(0.0, q - sold)
            seq = l.setdefault("sell_seq", 0) + 1; l["sell_seq"] = seq
            self.emit(f"sell:{l['id']}:{seq}", "sell", lot=l["id"], shares=round(sold, 4), vwap=round(gross / sold, 4), fee=fee,
                      proceeds=round(proceeds, 4), realized=round(proceeds - basis, 4))
            if l["shares"] <= 1e-6:
                l["status"] = "closed"; l["closed_ts"] = self.now; l["close_reason"] = "mirrored_sell"
                l["realized_total"] = l["proceeds"] - l["cost0"]

    def settle(self):
        for l in self.open_lots():
            try: m = self.market(l["cid"])
            except Exception: continue
            if not m or not m.get("closed"): continue
            px = token_mark(m, l["asset"])
            if px is None or (m.get("umaResolutionStatus") not in (None, "resolved")) or px not in (0.0, 1.0):
                continue
            payout = l["shares"] * px
            self.s["cash"] += payout; l["proceeds"] += payout
            self.emit(f"settle:{l['id']}", "settle", lot=l["id"], title=l["title"][:90], price=px, shares=round(l["shares"], 4),
                      payout=round(payout, 4), pnl=round(l["proceeds"] - l["cost0"], 4))
            l["realized_total"] = l["proceeds"] - l["cost0"]; l["shares"] = 0.0; l["cost"] = 0.0
            l["status"] = "settled"; l["closed_ts"] = self.now; l["settle_price"] = px
            ends = [x for x in (iso_ts(m.get("endDate")), iso_ts(m.get("closedTime"))) if x]
            l["res_ref_ts"] = min(ends) if ends else self.now
            l["clv"] = None; l["clv_tries"] = 0
        self.compute_clv()

    def compute_clv(self):
        """CLV per resolved lot = last CLOB trade price before the resolution reference time (the earlier of Gamma
        endDate and closedTime) minus our entry VWAP, per share. Retried on later passes if history is unavailable."""
        for l in self.s["lots"].values():
            if l["status"] != "settled" or l.get("clv") is not None or l.get("clv_tries", 0) >= CLV_MAX_TRIES: continue
            l["clv_tries"] = l.get("clv_tries", 0) + 1
            ref = l.get("res_ref_ts") or l["closed_ts"]
            try: h = self.api.price_history(l["asset"], max(l["entry_ts"], ref - CLV_LOOKBACK_S), ref)
            except Exception: continue
            pre = [p for t, p in h if t < ref]
            if not pre: continue
            l["close_price"] = pre[-1]; l["clv"] = pre[-1] - l["vwap"]
            self.emit(f"clv:{l['id']}", "clv", lot=l["id"], trader=l["trader"], close_price=pre[-1], entry=round(l["vwap"], 4),
                      clv=round(l["clv"], 4), settle_price=l.get("settle_price"))

    def marks(self):
        out = {}
        for l in self.open_lots():
            try: m = self.market(l["cid"])
            except Exception: m = None
            if m: out[l["asset"]] = token_mark(m, l["asset"])
        return out

def summary(state, now, marks=None):
    e = CopyEngine(state, None, now)
    eq = e.equity(marks); lots = list(state["lots"].values())
    yrs = (now - state["start_ts"]) / (365.25 * 86400)
    bench = START_CASH * (1 + CASH_APY) ** yrs
    filled = [l for l in lots]
    return dict(equity=round(eq, 2), cash=round(state["cash"], 2), deployed=round(e.deployed(), 2), open_lots=len(e.open_lots()),
                lots_total=len(lots), settled=sum(1 for l in lots if l["status"] == "settled"),
                avg_lag_min=round(sum(l["lag_s"] for l in filled) / len(filled) / 60, 1) if filled else None,
                avg_slippage_c=round(100 * sum(l["slippage"] for l in filled) / len(filled), 2) if filled else None,
                cash_benchmark=round(bench, 2), vs_benchmark=round(eq - bench, 2))
