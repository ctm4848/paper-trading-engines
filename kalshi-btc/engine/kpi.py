"""Kalshi KPIs (plan section 3) + the gate to restore paper capital. Recomputed from events.jsonl every pass.
GATE (all must hold): >= 2,000 `check` rows with a settled `outcome` and a market mid; Brier(model) <= Brier(market mid)
on those rows; and the forward hypothetical-trade log for a variant has >= 150 settled trades with net P&L > 0 after fees.
Capital is restored only with the rules of a variant that passed, and only after the owner is told."""
import json, os, time, math
GATE_MIN_CHECKS, GATE_MIN_TRADES = 2000, 150

def load_events(path):
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try: out.append(json.loads(line))
                except Exception: pass
    return out

def mid_of(c):
    if c.get("mid") is not None: return c["mid"]
    ya, na = c.get("yes_ask"), c.get("no_ask")          # rows logged before the mid field existed
    return round((ya + 1 - na) / 2, 4) if ya is not None and na is not None else None

def trade_stats(rows):
    if not rows: return dict(n=0, wins=0, pnl=0.0, c_per_contract=None, roi=None, fees=0.0)
    pnl = sum(r["pnl"] for r in rows); con = sum(r["contracts"] for r in rows); cost = sum(r["cost"] for r in rows)
    return dict(n=len(rows), wins=sum(1 for r in rows if r["pnl"] > 0), pnl=round(pnl, 2), c_per_contract=round(100 * pnl / con, 2),
                roi=round(pnl / cost, 4), fees=round(sum(r.get("fee", 0) for r in rows), 2))

def compute(events, state, now=None):
    now = now or time.time()
    outc = {e["ticker"]: (1 if e["result"] == "yes" else 0) for e in events if e["type"] == "outcome"}
    checks = [e for e in events if e["type"] == "check" and e.get("p_up") is not None]
    joined = [(c["p_up"], mid_of(c), outc[c["ticker"]]) for c in checks if c["ticker"] in outc and mid_of(c) is not None]
    n = len(joined)
    bm = sum((p - y) ** 2 for p, _, y in joined) / n if n else None
    bk = sum((m - y) ** 2 for _, m, y in joined) / n if n else None
    cal = {}
    for p, m, y in joined:
        b = min(int(p * 10), 9); cal.setdefault(b, []).append((p, m, y))
    calib = {f"{b/10:.1f}-{(b+1)/10:.1f}": dict(n=len(v), model=round(sum(x[0] for x in v) / len(v), 3), market=round(sum(x[1] for x in v) / len(v), 3),
             actual=round(sum(x[2] for x in v) / len(v), 3)) for b, v in sorted(cal.items())}
    # real (paper-capital) trades
    fills = {e["ticker"]: e for e in events if e["type"] == "fill"}
    real = [dict(pnl=e["pnl"], contracts=e["contracts"], cost=fills.get(e["ticker"], {}).get("cost", e["payout"] - e["pnl"]),
                 fee=fills.get(e["ticker"], {}).get("fee", 0)) for e in events if e["type"] == "settle"]
    rs = trade_stats(real)
    eq = 5000.0; peak = eq; mdd = 0.0
    for e in sorted((e for e in events if e["type"] == "settle"), key=lambda e: e["ts"]):
        eq += e["pnl"]; peak = max(peak, eq); mdd = max(mdd, 1 - eq / peak)
    claimed = [e["edge_all_in"] for e in fills.values() if e.get("edge_all_in") is not None]
    gross = sum(r["pnl"] + r["fee"] for r in real)
    # hypothetical trades
    hypo = {}
    for v in ("base", "ban15"):
        rows = [dict(pnl=e["pnl"], contracts=e["contracts"], cost=e["cost"]) for e in events if e["type"] == "hypo_settle" and e["variant"] == v]
        hf = {e["ticker"]: e for e in events if e["type"] == "hypo_fill" and e["variant"] == v}
        st = trade_stats(rows); st["fees"] = round(sum(hf[e["ticker"]]["fee"] for e in events if e["type"] == "hypo_settle" and e["variant"] == v and e["ticker"] in hf), 2)
        st["open"] = sum(1 for t in hf if t not in {e["ticker"] for e in events if e["type"] == "hypo_settle" and e["variant"] == v})
        hypo[v] = st
    checks_ok = n >= GATE_MIN_CHECKS; brier_ok = (bm is not None and bm <= bk)
    variants_ok = {v: (h["n"] >= GATE_MIN_TRADES and h["pnl"] > 0) for v, h in hypo.items()}
    # check rate since log-only start, for the time-to-gate estimate
    lo = [c for c in checks if c.get("reason") == "log_only_no_capital" or "hypo" in c or "mid" in c]
    t0 = min((c["ts"] for c in lo), default=None)
    days = (now - t0) / 86400 if t0 else 0
    rate = len(lo) / days if days >= 0.5 else None
    gate = dict(passed=bool(checks_ok and brier_ok and any(variants_ok.values())),
                checks_with_outcome=n, checks_needed=GATE_MIN_CHECKS, checks_ok=checks_ok,
                brier_model=None if bm is None else round(bm, 5), brier_market_mid=None if bk is None else round(bk, 5),
                brier_ok=brier_ok, replay_trades_needed=GATE_MIN_TRADES,
                replay={v: dict(n=h["n"], pnl=h["pnl"], ok=variants_ok[v]) for v, h in hypo.items()},
                eligible_variants=[v for v, ok in variants_ok.items() if ok and checks_ok and brier_ok],
                checks_per_day_since_log_only=None if rate is None else round(rate, 1),
                est_days_to_2000_checks=None if not rate else round(max(0, GATE_MIN_CHECKS - n) / rate, 1),
                note="~24 checks/day if only the hourly :07 wake runs (~3 months to 2,000); more while agents keep the box awake.")
    eqnow = state["cash"] + sum(p["cost"] for p in state["positions"].values())
    kpi = dict(engine="kalshi-btc-15m", mode="LOG_ONLY" if state.get("log_only", True) else "LIVE_PAPER", ts=int(now),
               utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
               n_checks=len(checks), n_checks_with_outcome=n,
               brier_model=gate["brier_model"], brier_market_mid=gate["brier_market_mid"],
               brier_diff_model_minus_market=None if bm is None else round(bm - bk, 5), calibration=calib,
               resolved_trades=rs["n"], realized_c_per_contract=rs["c_per_contract"], roi_on_cost=rs["roi"], real_pnl=rs["pnl"],
               edge_at_entry_claimed_c=round(100 * sum(claimed) / len(claimed), 2) if claimed else None,
               fee_drag=round(rs["fees"] / gross, 3) if gross > 0 else None, fees_paid=rs["fees"], max_dd=round(mdd, 4),
               equity=round(eqnow, 2), paused=state.get("paused"), hypothetical=hypo, gate=gate)
    return kpi, gate
