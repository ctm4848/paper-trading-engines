"""Polymarket copy-sleeve KPIs (plan section 3), overall and per trader. Recomputed from state + events every pass.
capture rate = fills / (fills + signals that passed the market filters but were not copied: seen_too_late, slippage
over 3c, price over 97c, no depth, cap reached, book failure). Only leader trades made after the engine started count."""
import json, os, time
import copycore as C

def load_events(path):
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try: out.append(json.loads(line))
                except Exception: pass
    return out

def _avg(xs): return round(sum(xs) / len(xs), 4) if xs else None

def block(lots, skips, start_ts):
    filled_keys = {l.get("trade_key") for l in lots}
    missed = {}
    for e in skips:
        if (e.get("their_ts") or 0) < start_ts: continue
        k = e.get("trade_key") or e["id"][5:]
        if k in filled_keys: continue
        missed[k] = e["reason"]                     # last reason for that trade wins (retries)
    reasons = {}
    for e in skips:
        if (e.get("their_ts") or 0) >= start_ts: reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
    cap_miss = sum(1 for r in missed.values() if r in C.CAPTURABLE_MISS)
    fills = len(lots)
    res = [l for l in lots if l["status"] in ("settled", "closed")]
    settled = [l for l in lots if l["status"] == "settled"]
    cost = sum(l["cost0"] for l in res); pnl = sum(l.get("realized_total", 0.0) for l in res)
    clv = [l["clv"] for l in settled if l.get("clv") is not None]
    return dict(fills=fills, skips_by_reason=reasons, capturable_missed=cap_miss,
                capture_rate=round(fills / (fills + cap_miss), 3) if fills + cap_miss else None,
                avg_lag_min=None if not lots else round(sum(l["lag_s"] for l in lots) / len(lots) / 60, 1),
                avg_slippage_c=None if not lots else round(100 * sum(l["slippage"] for l in lots) / len(lots), 2),
                avg_leader_price=_avg([l["their_price"] for l in lots]), avg_our_price=_avg([l["vwap"] for l in lots]),
                resolved_lots=len(settled), closed_by_mirrored_sell=len(res) - len(settled), open_lots=fills - len(res),
                avg_clv_c=None if not clv else round(100 * sum(clv) / len(clv), 2), clv_n=len(clv),
                hit_rate=round(sum(1 for l in settled if l.get("settle_price") == 1.0) / len(settled), 3) if settled else None,
                realized_pnl=round(pnl, 2), roi_realized=round(pnl / cost, 4) if cost else None)

def compute(events, state, now, marks=None):
    lots = list(state["lots"].values()); skips = [e for e in events if e["type"] == "skip"]
    start = state["start_ts"]
    summ = C.summary(state, now, marks)
    per = {}
    for w, name in C.TRADERS.items():
        per[name] = block([l for l in lots if l["wallet"] == w], [e for e in skips if e.get("wallet") == w or e.get("trader") == name], start)
    allb = block(lots, skips, start)
    return dict(engine="polymarket-copy", mode="DELAYED_COPY", copy_window_min=C.MAX_LAG_S // 60, ts=int(now),
                utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                **allb, resolved_lots_needed=C.RESOLVED_LOTS_NEEDED, verdict_ready=allb["resolved_lots"] >= C.RESOLVED_LOTS_NEEDED,
                equity=summ["equity"], cash=summ["cash"], deployed=summ["deployed"], cash_benchmark=summ["cash_benchmark"],
                vs_cash=summ["vs_benchmark"], per_trader=per)
