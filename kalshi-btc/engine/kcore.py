"""Kalshi BTC 15-minute paper bot rules (PAPER ONLY). Pure logic; I/O via injected kalshi/spot objects."""
import time, math, datetime as dt
import model as M, fees as F

START_CASH = 5000.0
PER_TRADE = 25.0          # $ incl. fee, 0.5% of start
MIN_EDGE = 0.04           # model prob - (ask + taker fee per contract)
WIN_MAX_S, WIN_MIN_S = 600, 120   # trade only 10..2 minutes before close
MAX_TRADES_DAY = 40       # per UTC day
DD_KILL = 0.15
KILL_MIN_TRADES, KILL_AVG_PNL = 150, -0.01
LOG_WINDOW_S = 900        # log a check for the open market every minute (whole window) for calibration
# FIX 2 (Oct 9, 2026, owner-approved KPI plan): LOG-ONLY. No paper capital goes to new trades until the gate in
# README "Gate to restore capital" passes (see kpi.py). Open positions still settle normally.
LOG_ONLY = True
# Hypothetical trades under the current rules, priced at the real ask book + fee, $25 each, tracked two ways:
HYPO_VARIANTS = {"base": 0.0, "ban15": 0.15}   # minimum all-in side price (ban15 = no contracts under 15c)

def iso_ts(s): return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
def utc_day(t): return time.strftime("%Y-%m-%d", time.gmtime(t))

def new_state(now):
    return {"version": 1, "start_ts": now, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "cash": START_CASH, "peak_equity": START_CASH, "positions": {}, "closed": {}, "await": {}, "trades_by_day": {},
            "paused": False, "pause_reason": None, "runs": 0, "last_run": None, "spot_fail_streak": 0, "api_fail_streak": 0,
            "n_checks": 0, "hypo": {}}

def walk(asks, p_side, budget, mult=1.0):
    """Whole contracts from levels whose all-in per-contract price keeps the edge, within budget (cost+fee, rounded)."""
    fills = []
    for p, q in asks:
        if p + F.fee_per_contract(p, mult) > p_side - MIN_EDGE: break
        fills.append([p, math.floor(q)])
    # trim to budget and to the edge after rounding
    def total(fl): return F.order_cost([(p, c) for p, c in fl if c > 0], mult)
    n = sum(c for _, c in fills)
    cap = int(budget // max(fills[0][0], 0.01)) if fills else 0
    while fills and n > 0:
        take, left = [], min(n, cap)
        for p, c in fills:
            k = min(c, left); take.append((p, k)); left -= k
        take = [(p, c) for p, c in take if c > 0]
        notional, fee, tot = total(take); cn = sum(c for _, c in take)
        if tot <= budget + 1e-9 and tot / cn <= p_side - MIN_EDGE + 1e-12: return take, notional, fee, tot
        cap = min(cap, cn) - 1
        if cap <= 0: break
    return [], 0.0, 0.0, 0.0

class Bot:
    def __init__(self, state, kalshi, spot, now):
        self.s = state; self.k = kalshi; self.sp = spot; self.now = now; self.events = []; self.alerts = []

    def emit(self, eid, typ, **kw): self.events.append({"id": eid, "type": typ, "ts": self.now, **kw})
    def equity(self): return self.s["cash"] + sum(p["cost"] for p in self.s["positions"].values())

    def run(self):
        self.settle(); self.kill_check()
        if not self.s["paused"]: self.check_markets()
        else: self.emit(f"paused:{self.now//60}", "paused", reason=self.s["pause_reason"])
        self.s["runs"] += 1; self.s["last_run"] = self.now
        return self

    # ---------- settlement (official Kalshi result), also records outcomes for every checked market
    def settle(self):
        s = self.s
        for tk, close in list(s["await"].items()):
            if self.now < close: continue
            try: m = self.k.market(tk)
            except Exception: continue
            res = (m or {}).get("result")
            if res not in ("yes", "no") or (m or {}).get("status") not in ("settled", "finalized", "determined"):
                if self.now - close > 6 * 3600:
                    self.alerts.append({"type": "settlement_overdue", "key": tk, "ts": self.now})
                continue
            self.emit(f"outcome:{tk}", "outcome", ticker=tk, result=res, expiration_value=m.get("expiration_value"),
                      strike=m.get("floor_strike"))
            self.settle_hypo(tk, res)
            pos = s["positions"].pop(tk, None)
            if pos:
                payout = pos["contracts"] * (1.0 if res == pos["side"] else 0.0)
                pnl = payout - pos["cost"]
                s["cash"] += payout
                pos.update(result=res, payout=payout, pnl=pnl, settled_ts=self.now)
                s["closed"][tk] = pos
                self.emit(f"settle:{tk}", "settle", ticker=tk, side=pos["side"], contracts=pos["contracts"], result=res,
                          payout=round(payout, 4), pnl=round(pnl, 4), pnl_per_contract=round(pnl / pos["contracts"], 4))
                s["peak_equity"] = max(s["peak_equity"], self.equity())
            del s["await"][tk]

    def hypo_book(self, v):
        h = self.s.setdefault("hypo", {}).setdefault(v, {"open": {}, "closed": {}, "by_day": {}})
        return h

    def settle_hypo(self, tk, res):
        for v in HYPO_VARIANTS:
            h = self.hypo_book(v); pos = h["open"].pop(tk, None)
            if not pos: continue
            payout = pos["contracts"] * (1.0 if res == pos["side"] else 0.0); pnl = payout - pos["cost"]
            pos.update(result=res, payout=payout, pnl=pnl, settled_ts=self.now); h["closed"][tk] = pos
            self.emit(f"hypo_settle:{v}:{tk}", "hypo_settle", variant=v, ticker=tk, side=pos["side"], contracts=pos["contracts"],
                      price=pos["vwap"], cost=round(pos["cost"], 4), result=res, pnl=round(pnl, 4),
                      pnl_per_contract=round(pnl / pos["contracts"], 4))

    def hypo_trades(self, tk, book, p, ya, na, mult, tau, close, strike):
        """What the bot WOULD buy under the current rules (window/edge/1 per market/40 a day), per variant."""
        out = {}
        for v, floor_px in HYPO_VARIANTS.items():
            h = self.hypo_book(v)
            if tk in h["open"] or tk in h["closed"]: out[v] = "already"; continue
            d = utc_day(self.now)
            if h["by_day"].get(d, 0) >= MAX_TRADES_DAY: out[v] = "daily_cap_40"; continue
            best = None
            for side, ask in (("yes", ya), ("no", na)):
                if ask is None or ask < floor_px: continue
                ps = p if side == "yes" else 1 - p
                e = ps - (ask + F.fee_per_contract(ask, mult))
                if e >= MIN_EDGE and (best is None or e > best[2]): best = (side, ps, e)
            if not best: out[v] = "no_edge" if floor_px == 0 else "no_edge_or_banned"; continue
            side, ps, e = best
            fills, notional, fee, tot = walk([(a, q) for a, q in book[side] if a >= floor_px], ps, PER_TRADE, mult)
            if not fills: out[v] = "no_depth_at_edge"; continue
            n = sum(c for _, c in fills)
            h["open"][tk] = dict(ticker=tk, side=side, contracts=n, vwap=notional / n, fee=fee, cost=tot, p_model=ps, p_up=p,
                                 entry_ts=self.now, tau_s=round(tau), strike=strike, close_ts=close)
            h["by_day"][d] = h["by_day"].get(d, 0) + 1
            self.emit(f"hypo_fill:{v}:{tk}", "hypo_fill", variant=v, ticker=tk, side=side, contracts=n, vwap=round(notional / n, 4),
                      fee=fee, cost=tot, p_model=round(ps, 4), edge_all_in=round(ps - tot / n, 4), tau_s=round(tau))
            out[v] = "hypo_fill"
        return out

    def kill_check(self):
        s = self.s
        if s["paused"]: return
        eq = self.equity(); dd = 1 - eq / s["peak_equity"]
        cl = list(s["closed"].values()); n = len(cl)
        avg = (sum(p["pnl"] for p in cl) / sum(p["contracts"] for p in cl)) if n else 0.0
        reason = None
        if dd >= DD_KILL: reason = f"drawdown {dd:.1%} >= 15%"
        elif n >= KILL_MIN_TRADES and avg < KILL_AVG_PNL: reason = f"avg P&L {avg*100:.2f}c/contract after {n} trades < -1c"
        if reason:
            s["paused"] = True; s["pause_reason"] = reason
            self.alerts.append({"type": "kill_switch", "key": reason[:20], "ts": self.now, "reason": reason, "equity": eq})
            self.emit(f"kill:{self.now}", "kill_switch", reason=reason, equity=eq)

    # ---------- signal + paper fill
    def check_markets(self):
        s = self.s
        try: mkts = self.k.open_markets(); s["api_fail_streak"] = 0
        except Exception as e:
            s["api_fail_streak"] += 1
            if s["api_fail_streak"] >= 5: self.alerts.append({"type": "kalshi_api_down", "key": str(self.now // 3600), "ts": self.now, "error": str(e)[:200]})
            return
        spot = vol = None; srcs = {}
        for m in mkts:
            tk = m["ticker"]; close = iso_ts(m["close_time"]); tau = close - self.now
            if tau <= 0 or tau > LOG_WINDOW_S or m.get("floor_strike") is None: continue
            minute = int(self.now // 60)
            base = dict(ticker=tk, tau_s=round(tau), strike=m["floor_strike"])
            if spot is None:
                try:
                    spot, srcs = self.sp.snapshot(); vol = M.realized_vol_per_min(self.sp.minute_closes()); s["spot_fail_streak"] = 0
                except Exception as e:
                    s["spot_fail_streak"] += 1
                    if s["spot_fail_streak"] >= 5: self.alerts.append({"type": "spot_proxy_down", "key": str(self.now // 3600), "ts": self.now, "error": str(e)[:200]})
                    self.emit(f"check:{tk}:{minute}", "check", **base, decision="skip", reason="spot_unavailable"); s["await"][tk] = close; continue
            p = M.p_up(spot, m["floor_strike"], tau, vol)
            try: book = self.k.orderbook(tk)
            except Exception as e:
                self.emit(f"check:{tk}:{minute}", "check", **base, decision="skip", reason="book_unavailable", p_up=round(p, 4)); s["await"][tk] = close; continue
            mult = float(m.get("fee_multiplier") or 1.0)
            ya = book["yes"][0][0] if book["yes"] else None; na = book["no"][0][0] if book["no"] else None
            ey = p - (ya + F.fee_per_contract(ya, mult)) if ya is not None else None
            en = (1 - p) - (na + F.fee_per_contract(na, mult)) if na is not None else None
            side = "yes" if (ey or -9) >= (en or -9) else "no"; edge = ey if side == "yes" else en
            yb = round(1 - na, 4) if na is not None else None
            mid = round((ya + yb) / 2, 4) if (ya is not None and yb is not None) else None
            rec = dict(**base, mid=mid, yes_bid=yb, spot_proxy=round(spot, 2), proxy_vs_ref_bp=round(1e4 * math.log(spot / m["floor_strike"]), 2),
                       sources={k: round(v, 2) for k, v in srcs.items()}, vol_bp_min=round(vol * 1e4, 2), p_up=round(p, 4),
                       yes_ask=ya, no_ask=na, edge_yes=None if ey is None else round(ey, 4), edge_no=None if en is None else round(en, 4),
                       best_side=side, best_edge=None if edge is None else round(edge, 4))
            s["await"][tk] = close; s["n_checks"] += 1
            reason = None
            if WIN_MIN_S <= tau <= WIN_MAX_S:
                rec["hypo"] = self.hypo_trades(tk, book, p, ya, na, mult, tau, close, m["floor_strike"])
            if not (WIN_MIN_S <= tau <= WIN_MAX_S): reason = "outside_10_to_2_min_window"
            elif tk in s["positions"] or tk in s["closed"]: reason = "already_traded_market"
            elif s["trades_by_day"].get(utc_day(self.now), 0) >= MAX_TRADES_DAY: reason = "daily_cap_40"
            elif edge is None or edge < MIN_EDGE: reason = "edge_below_4c"
            elif LOG_ONLY: reason = "log_only_no_capital"
            if reason:
                self.emit(f"check:{tk}:{minute}", "check", **rec, decision="skip", reason=reason); continue
            p_side = p if side == "yes" else 1 - p
            budget = min(PER_TRADE, s["cash"])
            fills, notional, fee, tot = walk(book[side], p_side, budget, mult)
            if not fills:
                self.emit(f"check:{tk}:{minute}", "check", **rec, decision="skip", reason="no_depth_at_edge"); continue
            n = sum(c for _, c in fills)
            s["cash"] -= tot
            s["positions"][tk] = dict(ticker=tk, side=side, contracts=n, notional=notional, fee=fee, cost=tot, vwap=notional / n,
                                      all_in=tot / n, p_model=p_side, p_up=p, entry_ts=self.now, tau_s=round(tau), strike=m["floor_strike"],
                                      spot=spot, close_ts=close, fills=fills)
            d = utc_day(self.now); s["trades_by_day"][d] = s["trades_by_day"].get(d, 0) + 1
            self.emit(f"check:{tk}:{minute}", "check", **rec, decision="trade", side=side)
            self.emit(f"fill:{tk}", "fill", ticker=tk, side=side, contracts=n, vwap=round(notional / n, 4), fee=fee, cost=tot,
                      p_model=round(p_side, 4), edge_all_in=round(p_side - tot / n, 4), tau_s=round(tau))

def summary(s):
    cl = list(s["closed"].values()); n = len(cl)
    pnl = sum(p["pnl"] for p in cl); con = sum(p["contracts"] for p in cl)
    eq = s["cash"] + sum(p["cost"] for p in s["positions"].values())
    return dict(equity=round(eq, 2), cash=round(s["cash"], 2), open=len(s["positions"]), trades_settled=n,
                wins=sum(1 for p in cl if p["pnl"] > 0), realized_pnl=round(pnl, 2),
                avg_pnl_c_per_contract=round(100 * pnl / con, 2) if con else None, checks=s["n_checks"],
                paused=s["paused"], pause_reason=s["pause_reason"])
