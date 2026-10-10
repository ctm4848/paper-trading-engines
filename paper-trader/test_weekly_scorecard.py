"""Tests for weekly_scorecard.py (no network, no 1min calls; fixtures in temp dirs). Run: python3 test_weekly_scorecard.py"""
import json, os, sys, tempfile, time, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weekly_scorecard as W
W.RESET_TS = 0   # fixtures use their own fixed clock; the live reset baseline is tested separately

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""))
    if not cond: fails.append(name)

NOW = 1_791_600_000.0          # fixed clock (s)
def w(p, obj): os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(json.dumps(obj))
def wl(p, rows): os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write("".join(json.dumps(r)+"\n" for r in rows))

def book(name, typ, eq=5000.0, paused=False, closed=()):
    b = {"type": typ, "name": name, "start": 5000.0, "equity": eq, "paused": paused, "closed": list(closed)}
    b["pos" if typ == "A" else "legs"] = {}
    return b

def fixture(root, kalshi_kpi=None, poly_kpi=None, a_eq=5000.0, a_paused=False, hourly_runs=48):
    f = os.path.join(root, "funding"); p = os.path.join(root, "poly"); k = os.path.join(root, "kalshi")
    start = NOW - 3*86400
    w(f"{f}/state.json", {"created_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(start)), "coins": ["BTC", "ETH"],
                          "books": {"A": book("A", "A", a_eq, a_paused), "A_shadow": book("A_shadow", "A"),
                                    "B_0.006": book("B_0.006", "B"), "B_0.008": book("B_0.008", "B")}})
    # one pass per hour for the last `hourly_runs` hours
    open(f"{f}/runs.log", "w").write("".join(f"x {int((NOW - h*3600 - 600)*1000)} 0\n" for h in range(hourly_runs)))
    w(f"{f}/kpi.json", {"start_ts": int(start*1000), "signal_line": "BTC 24h 0.0017% vs 0.008% floor"})
    w(f"{p}/state.json", {"start_ts": start, "last_summary": {"equity": 5000.0}})
    wl(f"{p}/events.jsonl", [{"type": "skip", "reason": "seen_too_late", "ts": NOW-100}]*6)
    if poly_kpi: w(f"{p}/kpi.json", poly_kpi)
    w(f"{k}/state.json", {"start_ts": start, "paused": False, "last_summary": {"equity": 4975.03}})
    ev = [{"type": "check", "ticker": "T1", "ts": NOW-1000, "p_up": 0.8, "mid": 0.6, "yes_ask": 0.61, "no_ask": 0.41},
          {"type": "check", "ticker": "T1", "ts": NOW-900, "p_up": 0.7, "yes_ask": 0.5, "no_ask": 0.52},
          {"type": "fill", "ticker": "T1", "ts": NOW-950, "side": "yes", "vwap": 0.55, "contracts": 10},
          {"type": "outcome", "ticker": "T1", "ts": NOW-500, "result": "yes"},
          {"type": "settle", "ticker": "T1", "ts": NOW-500, "contracts": 10, "pnl": 4.5, "side": "yes"}]
    wl(f"{k}/events.jsonl", ev)
    if kalshi_kpi: w(f"{k}/kpi.json", kalshi_kpi)
    W.PATHS = {"funding": f, "polymarket": p, "kalshi": k}

def test_uptime_math():
    runs = [10*3600 + 60, 11*3600 + 5, 11*3600 + 50*60, 13*3600 + 1]
    u = W.uptime(runs, 10*3600, 14*3600 + 1800, 900)
    check("hourly uptime counts hours with >=1 pass (3 of 4 complete hours)", u["uptime_pct"] == 75.0 and u["hours_expected"] == 4, str(u))
    check("design coverage uses 15-min slots", u["design_expected"] == 18 and u["design_runs"] == 4, str(u))
    check("no complete hour -> None, not 0 or 100", W.uptime([], 100, 200, 900)["uptime_pct"] is None)

def test_graceful_without_kpi(root):
    fixture(root)
    d = W.collect(NOW, refresh_dividend=False, with_credits=False)
    k = d["kalshi"]
    # Brier: model (0.8-1)^2=0.04, (0.7-1)^2=0.09 -> 0.065; market mid 0.6 and (0.5+1-0.52)/2=0.49 -> (0.16+0.2601)/2
    check("Kalshi Brier computed from events when kpi.json absent", k["source"] == "events" and abs(k["brier_model"]-0.065) < 1e-9
          and abs(k["brier_market_mid"]-0.21005) < 1e-9, str(k))
    check("Kalshi CLV from the last post-fill mid (0.49 - 0.55 = -6c)", abs(k["clv_c"] - (-6.0)) < 1e-9, str(k["clv_c"]))
    check("realized c/contract from settles (+45c)", k["realized_c_per_contract"] == 45.0)
    check("Polymarket capture from skips when kpi.json absent", d["polymarket"]["capture_rate"] == 0.0 and d["polymarket"]["source"] == "events")
    md = W.render(d, [])
    check("WEEKLY.md is ~12 lines", 10 <= len(md.strip().splitlines()) <= 14, str(len(md.strip().splitlines())))
    check("labels PAPER ONLY and dividend as research only", "PAPER ONLY" in md and "Research and tracking only" in md)

def test_kpi_json_preferred(root):
    fixture(root, kalshi_kpi={"brier_model": 0.13665, "brier_market_mid": 0.12598, "mode": "LOG_ONLY", "equity": 4925.19,
                              "realized_c_per_contract": -14.67, "resolved_trades": 3},
            poly_kpi={"fills": 2, "capture_rate": 0.5, "avg_clv_c": 1.25, "mode": "DELAYED_COPY", "copy_window_min": 120, "equity": 5010.0})
    d = W.collect(NOW, refresh_dividend=False, with_credits=False)
    check("Kalshi kpi.json values used", d["kalshi"]["brier_model"] == 0.13665 and d["kalshi"]["realized_c_per_contract"] == -14.67)
    ks = [s for s in d["sleeves"] if s["sleeve"].startswith("Kalshi")][0]
    check("Kalshi LOG_ONLY shown as shadow", "shadow" in ks["status"] and ks["equity"] == 4925.19, ks["status"])
    check("Polymarket kpi.json values used", d["polymarket"]["avg_clv_c"] == 1.25 and "delayed copy" in d["sleeves"][2]["status"])

def test_validity_and_escalation(root):
    fixture(root, hourly_runs=72)
    d = W.collect(NOW, refresh_dividend=False, with_credits=False)
    fa = d["sleeves"][0]
    check("funding with a pass every hour is VALID", fa["results_valid"] and d["uptime_week"]["funding"]["uptime_pct"] == 100.0,
          str(d["uptime_week"]["funding"]))
    check("engines with few passes are INVALID", not d["sleeves"][3]["results_valid"])
    st = os.path.join(root, "esc.json")
    fixture(root, a_eq=4400.0, a_paused=True, hourly_runs=72)
    flags, new = W.escalation_check(NOW, state_path=st)
    keys = {f["key"] for f in flags}
    check("kill switch flagged", "kill:Funding A" in keys)
    check("sleeve down >=10% flagged (-12%)", "down10:Funding A" in keys)
    check("uptime <80% over 48h flagged for kalshi", "uptime48:kalshi" in keys and "uptime48:funding" not in keys, str(keys))
    check("first check: all flags are new", len(new) == len(flags))
    flags2, new2 = W.escalation_check(NOW, state_path=st)
    check("repeat check: no new flags (escalate once per incident)", flags2 and not new2)
    d2 = W.collect(NOW, refresh_dividend=False, with_credits=False)
    check("decision asks the owner about the kill switch", "kill switch" in W.decision(d2, W.escalation_flags(NOW, d2)))

def test_reset_baseline(root):
    fixture(root, hourly_runs=72)
    W.RESET_TS = NOW - 7200
    try:
        d = W.collect(NOW, refresh_dividend=False, with_credits=False)
        check("reset: windows start at the reset (1 complete hour in 2h window ending :40)", d["uptime_week"]["funding"]["hours_expected"] == 1, str(d["uptime_week"]["funding"]))
        check("reset: sleeve start = reset time, total start $20,000", all(s["start_ts"] >= NOW - 7200 for s in d["sleeves"]) and d["total"]["start"] == 20000.0)
    finally:
        W.RESET_TS = 0

def test_missing_engine_dirs(root):
    W.PATHS = {"funding": os.path.join(root, "nope1"), "polymarket": os.path.join(root, "nope2"), "kalshi": os.path.join(root, "nope3")}
    try:
        d = W.collect(NOW, refresh_dividend=False, with_credits=False); W.render(d, []); ok = True
    except Exception as e: ok = False; d = repr(e)
    check("missing engine files degrade gracefully (no crash)", ok, "" if ok else d)

if __name__ == "__main__":
    real = dict(W.PATHS)
    test_uptime_math()
    for t in (test_graceful_without_kpi, test_kpi_json_preferred, test_validity_and_escalation, test_reset_baseline, test_missing_engine_dirs):
        r = tempfile.mkdtemp(); 
        try: t(r)
        finally: shutil.rmtree(r)
    W.PATHS = real
    print(f"\n{len(fails)} FAILED" if fails else "\nALL PASSED"); sys.exit(1 if fails else 0)
