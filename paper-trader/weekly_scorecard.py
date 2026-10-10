#!/usr/bin/env python3
"""Weekly scorecard for all PAPER sleeves (fake money only; reads files, never trades).
Writes /workspace/paper-trader/WEEKLY.md (~12 lines, KPI plan section 4) and WEEKLY.json.

  /workspace/paper-trader/.venv/bin/python /workspace/paper-trader/weekly_scorecard.py            # build scorecard
  /workspace/paper-trader/.venv/bin/python /workspace/paper-trader/weekly_scorecard.py --escalation-check
        -> prints JSON {flags, new_flags}; exit 2 if there is a NEW flag (kill switch fired, sleeve <= -10%,
           or uptime < 80% over the last 48h), else 0. Add --any to exit 2 on any current flag.
        --state PATH uses another escalation state file (for tests/demos; default escalation_state.json).
  Python: escalation_flags(now) -> current flags; escalation_check() -> (flags, new_flags).

Uptime definitions (honest given the box pauses when no agent is active; see the Oct 9 diagnosis):
  * uptime %  = clock-hours with >=1 engine pass / clock-hours in the window (the hourly :07 routine guarantees at most
                one pass per hour, so 100% is achievable). A sleeve under 80% for the week is marked RESULTS INVALID.
  * design %  = engine-cadence slots with a pass / slots in the window (Funding 15 min, Polymarket 5 min, Kalshi 1 min).
                Reported for transparency; hourly wakes cap it at ~25% / ~8% / ~1.7%.
Run evidence: runs.log (if any), loop.log 'ok' lines, events, heartbeat and kpi.json timestamps.
Other engines' kpi.json is used when present (keys documented in KPI_KEYS); otherwise values are computed from events."""
import json, os, re, sys, time, math, subprocess, datetime as dt
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_MD = os.path.join(HERE, "WEEKLY.md"); OUT_JSON = os.path.join(HERE, "WEEKLY.json")
ESC_STATE = os.path.join(HERE, "escalation_state.json")
RULE_CHANGES = os.path.join(HERE, "rule_changes.jsonl")
DECISION = os.path.join(HERE, "decision.txt")          # optional one-line manual decision for the owner
_ROOT = os.environ.get("PAPER_ENGINES_ROOT")   # set on the GitHub runner (repo checkout); unset on the box
ONEMIN = os.environ.get("ONEMIN_BIN", "/home/box/tools/onemin")
ONEMIN_LOG = os.path.join(_ROOT, "usage", "onemin-usage.tsv") if _ROOT else "/home/box/tools/onemin-usage.log"
PATHS = {"funding": "/workspace/funding-lab/engine", "polymarket": "/workspace/polymarket/copy-engine",
         "kalshi": "/workspace/kalshi-btc/engine"}
if _ROOT: PATHS = {"funding": os.path.join(_ROOT, "funding-lab/engine"), "polymarket": os.path.join(_ROOT, "polymarket/copy-engine"),
                   "kalshi": os.path.join(_ROOT, "kalshi-btc/engine")}
CADENCE_S = {"funding": 900, "polymarket": 300, "kalshi": 60}
CASH_APY = 0.04; WEEK_S = 7*86400; H = 3600
UPTIME_MIN = 80.0; DOWN_ESC = -10.0
REQUIRED_N = {"A": (8, "Book A trades (90-day rule)"), "B_0.006": (None, "90-day review vs cash"),
              "polymarket": (40, "resolved lots"), "kalshi": (150, "settled trades")}
KPI_KEYS = {"kalshi": ["brier_model", "brier_market_mid", "brier_diff_model_minus_market", "realized_c_per_contract",
                       "resolved_trades", "equity", "paused", "mode", "gate"],
            "polymarket": ["fills", "capture_rate", "avg_lag_min", "avg_slippage_c", "avg_clv_c", "clv_n", "resolved_lots",
                           "realized_pnl", "equity", "vs_cash", "mode", "copy_window_min"]}

# ---------------------------------------------------------------- helpers
def jload(p, default=None):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return default

def jlines(p):
    out = []
    if not os.path.exists(p): return out
    with open(p) as f:
        for line in f:
            try: out.append(json.loads(line))
            except Exception: pass
    return out

def to_s(ts):
    """Normalize ms or s timestamps to seconds."""
    if ts is None: return None
    ts = float(ts); return ts/1000 if ts > 1e11 else ts

def iso_s(s): return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
def ct(ts, fmt="%a %b %-d %-I:%M %p"): return dt.datetime.fromtimestamp(ts, CT).strftime(fmt) + " CT"
def money(x, sign=False): return "n/a" if x is None else (f"{'+' if sign and x >= 0 else '-' if x < 0 else ''}${abs(x):,.2f}")
def pctf(x, nd=1, sign=True): return "n/a" if x is None else f"{x:+.{nd}f}%" if sign else f"{x:.{nd}f}%"

def cash_value(start_cap, start_ts, now): return start_cap * (1 + CASH_APY * max(now - start_ts, 0) / (365*86400))

# ---------------------------------------------------------------- run evidence + uptime
def run_times(name, d):
    ts = set()
    rl = os.path.join(d, "runs.log")
    if os.path.exists(rl):
        for line in open(rl):
            p = line.split()
            try: ts.add(to_s(int(p[1])) if len(p) > 1 and p[1].isdigit() else iso_s(p[0]))
            except Exception: pass
    ll = os.path.join(d, "loop.log")
    if os.path.exists(ll):
        for line in open(ll, errors="ignore"):
            m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) ok", line)
            if m: ts.add(iso_s(m.group(1)))
    for e in jlines(os.path.join(d, "events.jsonl")):
        if name == "funding" and e.get("type") == "snapshot":   # snapshot ts = last processed 5m bar; pass ran ~6 min later
            ts.add(to_s(e["ts"]) + 360)
        elif name == "kalshi" and e.get("type") == "check":
            ts.add(to_s(e["ts"]))
    for f in ("heartbeat.txt", "kpi.json"):
        j = jload(os.path.join(d, f), {}) or {}
        if j.get("ts"): ts.add(to_s(j["ts"]))
    st = jload(os.path.join(d, "state.json"), {}) or {}
    if st.get("last_run"): ts.add(to_s(st["last_run"]))
    return sorted(ts)

def uptime(runs, start, end, cadence_s):
    """Hourly coverage and design-cadence coverage over [start, end). Only COMPLETE slots count as expected."""
    def cov(slot):
        a = int(math.ceil(start / slot)); b = int(end // slot)
        if b <= a: return None, 0, 0
        have = {int(r // slot) for r in runs if start <= r < end}
        n_have = sum(1 for k in range(a, b) if k in have)
        return round(100 * n_have / (b - a), 1), n_have, b - a
    hp, hh, he = cov(H); dp, dh, de = cov(cadence_s)
    return {"uptime_pct": hp, "hours_covered": hh, "hours_expected": he,
            "design_pct": dp, "design_runs": dh, "design_expected": de, "cadence_s": cadence_s}

# ---------------------------------------------------------------- sleeves
def funding_sleeves(now, wk0):
    d = PATHS["funding"]; st = jload(os.path.join(d, "state.json")); kpi = jload(os.path.join(d, "kpi.json"), {}) or {}
    if not st: return {}, {"error": "no state.json"}
    import calendar
    start = (kpi.get("start_ts") and to_s(kpi["start_ts"])) or calendar.timegm(time.strptime(st["created_utc"], "%Y-%m-%d %H:%M:%S"))
    snaps = [e for e in jlines(os.path.join(d, "events.jsonl")) if e.get("type") == "snapshot"]
    out = {}
    for n, b in st["books"].items():
        closed = b.get("closed", [])
        wk_tr = [t for t in closed if to_s(t.get("exit_ts", 0)) >= wk0]
        eq0 = b["start"] if start >= wk0 else next((s["books"][n]["equity"] for s in reversed(snaps)
                                                     if to_s(s["ts"]) <= wk0 and n in s["books"]), None)
        is_open = bool(b.get("pos") or b.get("legs"))
        status = ("paused (kill switch)" if b.get("paused") else "live, in a trade" if is_open else "live, idle by design")
        out[n] = {"sleeve": f"Funding {n}", "start_cap": b["start"], "start_ts": start, "equity": round(b["equity"], 2),
                  "week_pnl": round(b["equity"] - eq0, 2) if eq0 is not None else None,
                  "trades_week": len(wk_tr), "trades_total": len(closed), "open": sorted(b.get("pos", b.get("legs", {}))),
                  "status": status, "kill_switch": bool(b.get("paused")),
                  "variant": n in ("A_shadow", "B_0.008")}
    extra = {"signal_line": kpi.get("signal_line") or st.get("signal_line"), "kpi": kpi}
    return out, extra

def kalshi_metrics(d):
    kpi = jload(os.path.join(d, "kpi.json"))
    ev = jlines(os.path.join(d, "events.jsonl"))
    outcomes = {e["ticker"]: e["result"] for e in ev if e.get("type") == "outcome"}
    checks = [e for e in ev if e.get("type") == "check" and e.get("ticker") in outcomes and e.get("p_up") is not None]
    def mid(c):
        if c.get("mid") is not None: return c["mid"]
        if c.get("yes_ask") is not None and c.get("no_ask") is not None: return (c["yes_ask"] + 1 - c["no_ask"]) / 2
        return None
    bm = [((c["p_up"] - (outcomes[c["ticker"]] == "yes"))**2, (mid(c) - (outcomes[c["ticker"]] == "yes"))**2)
          for c in checks if mid(c) is not None]
    calc = {"n_checks_with_outcome": len(bm),
            "brier_model": round(sum(a for a, _ in bm)/len(bm), 5) if bm else None,
            "brier_market_mid": round(sum(b for _, b in bm)/len(bm), 5) if bm else None}
    # CLV: last logged mid of our side before close minus our all-in-ex-fee VWAP, per contract (cents)
    fills = [e for e in ev if e.get("type") == "fill"]
    clv = []
    for f in fills:
        cs = [c for c in ev if c.get("type") == "check" and c.get("ticker") == f["ticker"] and to_s(c["ts"]) > to_s(f["ts"])]
        if not cs or mid(cs[-1]) is None: continue
        m = mid(cs[-1]); side_px = m if f["side"] == "yes" else 1 - m
        clv.append(((side_px - f["vwap"]) * 100, f["contracts"]))
    calc["clv_c"] = round(sum(a*w for a, w in clv)/sum(w for _, w in clv), 2) if clv else None
    calc["clv_n"] = len(clv)
    settles = [e for e in ev if e.get("type") == "settle"]
    nc = sum(s["contracts"] for s in settles)
    calc["realized_c_per_contract"] = round(100*sum(s["pnl"] for s in settles)/nc, 2) if nc else None
    calc["resolved_trades"] = len(settles)
    m = dict(calc); src = "events"
    if kpi:
        src = "kpi.json (+events for CLV)"
        for k in KPI_KEYS["kalshi"]:
            if k in kpi and kpi[k] is not None: m[k] = kpi[k]
    m["source"] = src
    return m, settles, fills

def kalshi_sleeve(now, wk0):
    d = PATHS["kalshi"]; st = jload(os.path.join(d, "state.json"), {}) or {}
    m, settles, fills = kalshi_metrics(d)
    start = to_s(st.get("start_ts")) or now
    eq = (st.get("last_summary") or {}).get("equity", m.get("equity"))
    if m.get("equity") is not None: eq = m["equity"]
    wk_pnl = sum(s["pnl"] for s in settles if to_s(s["ts"]) >= wk0) if start < wk0 else (eq - 5000.0 if eq is not None else None)
    mode = m.get("mode") or "LIVE"
    paused = bool(st.get("paused") or m.get("paused"))
    status = "paused (kill switch)" if paused else ("shadow / log-only (no new paper trades)" if "LOG" in str(mode).upper() else "live")
    return {"sleeve": "Kalshi BTC 15m", "start_cap": 5000.0, "start_ts": start, "equity": eq,
            "week_pnl": round(wk_pnl, 2) if wk_pnl is not None else None,
            "trades_week": sum(1 for f in fills if to_s(f["ts"]) >= wk0), "trades_total": m.get("resolved_trades", len(settles)),
            "status": status, "kill_switch": paused, "variant": False}, m

def polymarket_sleeve(now, wk0):
    d = PATHS["polymarket"]; st = jload(os.path.join(d, "state.json"), {}) or {}; kpi = jload(os.path.join(d, "kpi.json"))
    ev = jlines(os.path.join(d, "events.jsonl"))
    fills = [e for e in ev if e.get("type") == "fill"]; settles = [e for e in ev if e.get("type") == "settle"]
    skips = [e for e in ev if e.get("type") == "skip"]
    late = sum(1 for e in skips if e.get("reason") == "seen_too_late")
    m = {"fills": len(fills), "resolved_lots": len(settles),
         "capture_rate": round(len(fills)/(len(fills)+late), 3) if (fills or late) else None,
         "avg_clv_c": None, "clv_n": 0, "source": "events"}
    if kpi:
        m["source"] = "kpi.json"
        for k in KPI_KEYS["polymarket"]:
            if k in kpi: m[k] = kpi[k]
    start = to_s(st.get("start_ts")) or now
    eq = m.get("equity", (st.get("last_summary") or {}).get("equity"))
    wk_pnl = (eq - 5000.0) if (eq is not None and start >= wk0) else None
    status = "live" + (f", {m['mode'].lower().replace('_', ' ')} ({m.get('copy_window_min')} min window)" if m.get("mode") else "")
    if m.get("capture_rate") is not None and m["capture_rate"] < 0.5 and (m.get("fills", 0) + late) >= 5:
        status += "; capture <50%: not yet testable"
    return {"sleeve": "Polymarket copy", "start_cap": 5000.0, "start_ts": start, "equity": eq,
            "week_pnl": round(wk_pnl, 2) if wk_pnl is not None else None,
            "trades_week": sum(1 for f in fills if to_s(f["ts"]) >= wk0), "trades_total": m.get("resolved_lots", 0),
            "status": status, "kill_switch": bool(st.get("paused")), "variant": False}, m

# ---------------------------------------------------------------- dividend basket (research / tracking only)
def dividend_update(refresh=True):
    sys.path.insert(0, HERE)
    try:
        import update_scoreboard as U
        board = json.load(open(U.SCOREBOARD_JSON))
        if refresh:
            u = U.refresh(board); board["last_update"] = u
            json.dump(board, open(U.SCOREBOARD_JSON, "w"), indent=2); U.write_markdown(board, u)
            return {"ok": True, "refreshed": True, **u}
        return {"ok": True, "refreshed": False, **(board.get("last_update") or {})}
    except Exception as e:
        board = jload(os.path.join(HERE, "scoreboard.json"), {}) or {}
        return {"ok": False, "refreshed": False, "error": repr(e)[:200], **(board.get("last_update") or {})}

# ---------------------------------------------------------------- credits, rule changes, decision
def credits(now, wk0):
    out = {"agent": "paper-trader", "week": None, "month": None, "month_team": None, "week_by_model": {}}
    try:
        p = subprocess.run([ONEMIN, "--usage"], capture_output=True, text=True, timeout=60)
        m = re.search(r"agent paper-trader:\s*(\d+)", p.stdout); out["month"] = int(m.group(1)) if m else None
        m = re.search(r"total:\s*(\d+) of ([\d,]+)", p.stdout)
        if m: out["month_team"] = f"{int(m.group(1)):,} of {m.group(2)}"
    except Exception as e: out["usage_error"] = repr(e)[:120]
    if os.path.exists(ONEMIN_LOG):
        tot = 0
        for line in open(ONEMIN_LOG):
            p = line.rstrip("\n").split("\t")
            if len(p) < 4 or p[1] != "paper-trader": continue
            try: t = dt.datetime.fromisoformat(p[0]).replace(tzinfo=CT).timestamp()
            except Exception: continue
            if t >= wk0:
                c = int(p[3]); tot += c; out["week_by_model"][p[2]] = out["week_by_model"].get(p[2], 0) + c
        out["week"] = tot
    return out

def rule_changes(wk0):
    return [r for r in jlines(RULE_CHANGES) if to_s(iso_s(r["utc"])) >= wk0]

# ---------------------------------------------------------------- escalation
def escalation_flags(now=None, data=None):
    """Flags that must reach the owner immediately (plan section 4): kill switch fired, sleeve down >=10%,
    uptime < 80% over the last 48h. Returns a list of {key, msg}. Cheap: reads files only, no network."""
    now = now or time.time(); data = data or collect(now, refresh_dividend=False, with_credits=False)
    flags = []
    for s in data["sleeves"]:
        if s.get("kill_switch"): flags.append({"key": f"kill:{s['sleeve']}", "msg": f"{s['sleeve']}: kill switch fired / paused"})
        if s.get("equity") is not None and s.get("start_cap"):
            r = 100*(s["equity"]/s["start_cap"] - 1)
            if r <= DOWN_ESC: flags.append({"key": f"down10:{s['sleeve']}", "msg": f"{s['sleeve']} is {r:.1f}% vs start"})
    for n, u in data["uptime_48h"].items():
        if u["uptime_pct"] is not None and u["uptime_pct"] < UPTIME_MIN:
            flags.append({"key": f"uptime48:{n}", "msg": f"{n} uptime {u['uptime_pct']}% over the last 48h "
                          f"({u['hours_covered']}/{u['hours_expected']} hours)"})
    return flags

def escalation_check(now=None, state_path=None):
    """Returns (flags, new_flags). A flag is 'new' if it was not active at the previous check, so an hourly routine
    escalates once per incident instead of every hour. State: escalation_state.json."""
    state_path = state_path or ESC_STATE
    flags = escalation_flags(now)
    prev = set((jload(state_path, {}) or {}).get("active", []))
    new = [f for f in flags if f["key"] not in prev]
    with open(state_path + ".tmp", "w") as f:
        json.dump({"active": [x["key"] for x in flags], "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
    os.replace(state_path + ".tmp", state_path)
    return flags, new

# ---------------------------------------------------------------- collect + render
def collect(now=None, refresh_dividend=True, with_credits=True):
    now = now or time.time(); wk0 = now - WEEK_S
    fb, fx = funding_sleeves(now, wk0)
    ks, km = kalshi_sleeve(now, wk0)
    ps, pm = polymarket_sleeve(now, wk0)
    sleeves = [s for n, s in fb.items() if n in ("A", "B_0.006")] + [ps, ks]
    variants = [s for n, s in fb.items() if n not in ("A", "B_0.006")]
    starts = {"funding": fb.get("A", {}).get("start_ts", now), "polymarket": ps["start_ts"], "kalshi": ks["start_ts"]}
    runs = {n: run_times(n, PATHS[n]) for n in PATHS}
    up_wk = {n: uptime(runs[n], max(wk0, starts[n]), now, CADENCE_S[n]) for n in PATHS}
    up_48 = {n: uptime(runs[n], max(now - 48*H, starts[n]), now, CADENCE_S[n]) for n in PATHS}
    eng_of = lambda s: "funding" if s["sleeve"].startswith("Funding") else "polymarket" if s["sleeve"].startswith("Poly") else "kalshi"
    for s in sleeves + variants:
        u = up_wk[eng_of(s)]["uptime_pct"]
        s["results_valid"] = u is not None and u >= UPTIME_MIN
    tot_start = sum(s["start_cap"] for s in sleeves)
    tot_eq = sum(s["equity"] for s in sleeves if s["equity"] is not None)
    tot_cash = sum(cash_value(s["start_cap"], s["start_ts"], now) for s in sleeves)
    data = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "generated_ct": ct(now),
            "window": {"start_ct": ct(wk0), "end_ct": ct(now)}, "paper_only": True,
            "total": {"start": tot_start, "equity": round(tot_eq, 2), "pnl": round(tot_eq - tot_start, 2),
                      "cash_benchmark": round(tot_cash, 2), "vs_cash": round(tot_eq - tot_cash, 2),
                      "sleeves_counted": [s["sleeve"] for s in sleeves]},
            "sleeves": sleeves, "variants": variants, "kalshi": km, "polymarket": pm,
            "funding": {"signal_line": fx.get("signal_line"),
                        "classifier_consistency": (fx.get("kpi") or {}).get("classifier_consistency"),
                        "bookA_vs_btc_buy_hold": (fx.get("kpi") or {}).get("bookA_vs_btc_buy_hold"),
                        "bookB_net_apy": (fx.get("kpi") or {}).get("bookB_net_apy")},
            "uptime_week": up_wk, "uptime_48h": up_48, "rule_changes": rule_changes(wk0)}
    data["dividend"] = dividend_update(refresh_dividend)
    if with_credits: data["credits"] = credits(now, wk0)
    return data

def decision(data, flags):
    if flags:
        f = flags[0]
        if f["key"].startswith("kill:"): return f"{f['msg']}: keep it paused, or resume after review?"
        if f["key"].startswith("down10:"): return f"{f['msg']}: keep, shrink or stop this sleeve?"
    if os.path.exists(DECISION):
        t = open(DECISION).read().strip()
        if t: return t
    return "none"

def render(data, flags):
    T = data["total"]; L = []
    L.append(f"# Paper Trader weekly scorecard (PAPER ONLY, fake money): {data['window']['start_ct']} to {data['window']['end_ct']}")
    L.append("")
    L.append(f"1. **Total paper equity** {money(T['equity'])} vs {money(T['start'])} start ({money(T['pnl'], True)}); "
             f"cash at 4%/yr would be {money(T['cash_benchmark'])} ({money(T['vs_cash'], True)} vs cash). "
             f"Counts {', '.join(T['sleeves_counted'])}; variants and the dividend basket are excluded.")
    for s in data["sleeves"]:
        key = "A" if s["sleeve"] == "Funding A" else "B_0.006" if s["sleeve"] == "Funding B_0.006" else \
              "polymarket" if s["sleeve"].startswith("Poly") else "kalshi"
        req, what = REQUIRED_N[key]
        n_txt = f"{s['trades_total']}/{req} {what}" if req else f"{s['trades_total']} trades ({what})"
        L.append(f"2. **{s['sleeve']}** {money(s['equity'])}, week {money(s['week_pnl'], True)}, trades {s['trades_week']} this week, "
                 f"N {n_txt}; {s['status']}{'' if s['results_valid'] else '; **RESULTS INVALID (uptime <80%)**'}")
    v = data["variants"]
    if v: L.append("   Variants (not counted): " + "; ".join(f"{x['sleeve']} {money(x['equity'])}, N {x['trades_total']}" for x in v))
    k = data["kalshi"]; p = data["polymarket"]
    bd = (k["brier_model"] - k["brier_market_mid"]) if k.get("brier_model") is not None and k.get("brier_market_mid") is not None else None
    L.append(f"3. **Prediction markets:** Kalshi Brier model {k.get('brier_model')} vs market {k.get('brier_market_mid')} "
             f"({'model worse' if bd and bd > 0 else 'model at least as good' if bd is not None else 'n/a'} by {abs(bd):.4f}, "
             f"{k.get('n_checks_with_outcome')} checks; gate needs 2,000)" if bd is not None else
             "3. **Prediction markets:** Kalshi Brier n/a (no checks with outcomes yet)")
    L[-1] += (f"; CLV {k['clv_c']:+.2f}c (n={k['clv_n']})" if k.get("clv_c") is not None else "; CLV n/a (no post-fill quotes logged)")
    L[-1] += (f"; realized {k['realized_c_per_contract']:+.2f}c/contract on {k.get('resolved_trades')} trades." if k.get("realized_c_per_contract") is not None else "; realized n/a.")
    L[-1] += (f" Polymarket: {p.get('fills', 0)} fills, capture {('n/a' if p.get('capture_rate') is None else f'{100*p['capture_rate']:.0f}%')}, "
              f"CLV {('n/a' if p.get('avg_clv_c') is None else f'{p['avg_clv_c']:+.2f}c')}, {p.get('resolved_lots', 0)} resolved lots.")
    L.append(f"4. **Funding distance to trigger:** {data['funding']['signal_line'] or 'n/a (no kpi.json yet)'}")
    U = data["uptime_week"]
    L.append("5. **Uptime this week** (hours with a pass / hours; design cadence in brackets): " + "; ".join(
        f"{n} {pctf(u['uptime_pct'], sign=False)} ({u['hours_covered']}/{u['hours_expected']} h) [{pctf(u['design_pct'], sign=False)} of "
        f"{u['cadence_s']//60}-min slots]" for n, u in U.items()) + ". Under 80% = results invalid.")
    rc = data["rule_changes"]; cr = data.get("credits", {})
    rc_txt = "; ".join(f"{r['sleeve']}: {r['change']} (why: {r['reason']})" for r in rc) if rc else "none"
    fmt = lambda x: "n/a" if x is None else f"{x:,}"
    top = sorted((cr.get("week_by_model") or {}).items(), key=lambda kv: -kv[1])[:3]
    L.append(f"6. **Rule changes:** {rc_txt}. **1min credits:** paper-trader {fmt(cr.get('week'))} this week"
             f"{' (' + ', '.join(f'{m} {c:,}' for m, c in top) + ')' if top else ''}, {fmt(cr.get('month'))} this month "
             f"(team {cr.get('month_team', 'n/a')}).")
    L.append(f"7. **Decision for the owner:** {decision(data, flags)}")
    dv = data["dividend"]
    if dv.get("total_value") is not None:
        L.append(f"Research and tracking only (the owner never committed the $10k): dividend growers {money(dv['total_value'])} ({pctf(dv['return_pct'], 2)}) "
                 f"vs SPY {money(dv['spy_total_value'])} ({pctf(dv['spy_return_pct'], 2)}), prices as of {dv.get('as_of')}"
                 f"{'' if dv.get('refreshed') else ' (STALE: refresh failed: ' + str(dv.get('error')) + ')'}.")
    if flags: L.append("Escalations: " + "; ".join(f["msg"] for f in flags))
    return "\n".join(L) + "\n"

def main(argv):
    if "--escalation-check" in argv:
        sp = argv[argv.index("--state")+1] if "--state" in argv else None
        flags, new = escalation_check(state_path=sp)
        print(json.dumps({"flags": flags, "new_flags": new}, indent=1))
        sys.exit(2 if (new or ("--any" in argv and flags)) else 0)
    now = time.time()
    data = collect(now, refresh_dividend="--no-refresh" not in argv)
    flags = escalation_flags(now, data)
    data["escalations"] = flags; data["decision"] = decision(data, flags)
    md = render(data, flags)
    for path, text in ((OUT_MD, md), (OUT_JSON, json.dumps(data, indent=1, default=str))):
        with open(path + ".tmp", "w") as f: f.write(text)
        os.replace(path + ".tmp", path)
    print(md)

if __name__ == "__main__":
    main(sys.argv[1:])
