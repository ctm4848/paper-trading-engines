import os, sys, json, tempfile, unittest, datetime as dt, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kcore as C, engine as E, fees as F, model as M, kpi as KPI

T0 = 1_800_000_000 - (1_800_000_000 % 900)   # a window boundary
def iso(t): return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class FakeKalshi:
    def __init__(self): self.mk = {}; self.books = {}; self.results = {}
    def add(self, close, strike=80000.0, yes_ask=0.60, no_ask=0.42, depth=1000):
        tk = f"KXBTC15M-T{close}"; self.mk[tk] = dict(ticker=tk, open_time=iso(close - 900), close_time=iso(close), floor_strike=strike)
        self.books[tk] = dict(yes=[(yes_ask, depth), (yes_ask + 0.01, depth)], no=[(no_ask, depth)]); return tk
    def open_markets(self): return [m for tk, m in self.mk.items() if tk not in self.results]
    def market(self, tk):
        r = self.results.get(tk); return dict(self.mk[tk], status="finalized" if r else "active", result=r or "", expiration_value="1")
    def orderbook(self, tk): return self.books[tk]

class FakeSpot:
    def __init__(self, px=80100.0, vol=0.0005): self.px = px; self.vol = vol
    def snapshot(self): return self.px, {"coinbase": self.px, "kraken": self.px}
    def minute_closes(self):
        out = [80000.0]
        for i in range(60): out.append(out[-1] * math.exp(self.vol * (1 if i % 2 else -1)))
        return out

def files():
    d = tempfile.mkdtemp(); return {k: os.path.join(d, k) for k in ("state_path", "events_path", "hb_path", "alert_path")}
def run(k, s, f, now): return E.run_once(kalshi=k, spot=s, now=now, **f)
def ev(f): return [json.loads(l) for l in open(f["events_path"])] if os.path.exists(f["events_path"]) else []
def st(f): return json.load(open(f["state_path"]))

class Fees(unittest.TestCase):
    def test_formula_and_rounding(self):
        n, fee, tot = F.order_cost([(0.5, 100)]); self.assertAlmostEqual(fee, 1.75); self.assertAlmostEqual(tot, 51.75)
        n, fee, tot = F.order_cost([(0.95, 1)]); self.assertAlmostEqual(tot, 0.96)          # 0.95 + 0.003325 -> 0.96
        n, fee, tot = F.order_cost([(0.05, 100)]); self.assertAlmostEqual(fee, 0.34)         # matches schedule table
        n, fee, tot = F.order_cost([(0.10, 100)]); self.assertAlmostEqual(fee, 0.63)
        self.assertAlmostEqual(F.fee_per_contract(0.5), 0.0175)

class Model(unittest.TestCase):
    def test_monotone_and_symmetry(self):
        self.assertAlmostEqual(M.p_up(80000, 80000, 300, 0.0005), 0.5, places=6)
        self.assertGreater(M.p_up(80100, 80000, 300, 0.0005), M.p_up(80100, 80000, 600, 0.0005))   # less time -> more certain
        self.assertGreater(M.p_up(80100, 80000, 300, 0.0005), M.p_up(80100, 80000, 300, 0.001))   # more vol -> less certain
        self.assertAlmostEqual(M.p_up(80100, 80000, 300, 5e-4) + M.p_up(80000, 80100, 300, 5e-4), 1, places=2)
    def test_settlement_average_variance(self):
        # tau=120 -> variance uses tau-40 = 80 s, not 120 s
        p = M.p_up(80010, 80000, 120, 0.0006); sig = 0.0006 / math.sqrt(60)
        z = math.log(80010 / 80000) / math.sqrt(sig ** 2 * 80 + M.PROXY_ERR ** 2); self.assertAlmostEqual(p, M.phi(z), places=9)

class LiveCapitalMode(unittest.TestCase):
    """Capital path kept for when the gate passes; these tests run with LOG_ONLY switched off."""
    def setUp(self): self._lo = C.LOG_ONLY; C.LOG_ONLY = False
    def tearDown(self): C.LOG_ONLY = self._lo

class Rules(LiveCapitalMode):
    def test_trade_in_window_size_and_one_per_market(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); tk = k.add(T0 + 900, yes_ask=0.60)
        run(k, s, f, T0 + 900 - 700)                     # 11.7 min left: outside window
        self.assertEqual(st(f)["positions"], {})
        run(k, s, f, T0 + 900 - 500)                     # inside window -> trade
        p = st(f)["positions"][tk]; self.assertEqual(p["side"], "yes"); self.assertLessEqual(p["cost"], 25.0)
        self.assertGreaterEqual(p["p_model"] - p["all_in"], 0.04 - 1e-9)
        run(k, s, f, T0 + 900 - 440); run(k, s, f, T0 + 900 - 380)
        self.assertEqual(sum(1 for e in ev(f) if e["type"] == "fill"), 1)
        run(k, s, f, T0 + 900 - 60)                      # inside last 2 min: never trade
        reasons = [e.get("reason") for e in ev(f) if e["type"] == "check"]
        self.assertIn("outside_10_to_2_min_window", reasons); self.assertIn("already_traded_market", reasons)
    def test_no_trade_without_edge(self):
        k, s, f = FakeKalshi(), FakeSpot(80000), files(); tk = k.add(T0 + 900, yes_ask=0.52, no_ask=0.50)
        run(k, s, f, T0 + 500); self.assertEqual(st(f)["positions"], {})
        self.assertEqual([e["reason"] for e in ev(f) if e["type"] == "check"], ["edge_below_4c"])
    def test_depth_limits_size(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); tk = k.add(T0 + 900, yes_ask=0.60, depth=7)
        k.books[tk]["yes"] = [(0.60, 7), (0.90, 1000)]     # second level has no edge
        run(k, s, f, T0 + 500); self.assertEqual(st(f)["positions"][tk]["contracts"], 7)
    def test_daily_cap(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); run(k, s, f, T0)
        x = st(f); x["trades_by_day"][C.utc_day(T0 + 500)] = 40; json.dump(x, open(f["state_path"], "w"))
        tk = k.add(T0 + 900, yes_ask=0.60); run(k, s, f, T0 + 500)
        self.assertEqual(st(f)["positions"], {}); self.assertIn("daily_cap_40", [e.get("reason") for e in ev(f)])
    def test_size_cap_with_cash_constraint(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); tk = k.add(T0 + 900, yes_ask=0.10)
        run(k, s, f, T0 + 500); p = st(f)["positions"][tk]
        self.assertLessEqual(p["cost"], 25.0); self.assertGreater(p["cost"], 24.0)

class Idempotency(LiveCapitalMode):
    def test_restart_no_duplicate_fill_or_settlement(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); tk = k.add(T0 + 900, yes_ask=0.60)
        run(k, s, f, T0 + 400); run(k, s, f, T0 + 400); cost = st(f)["positions"][tk]["cost"]
        k.results[tk] = "yes"
        run(k, s, f, T0 + 960); cash = st(f)["cash"]
        run(k, s, f, T0 + 960); run(k, s, f, T0 + 1020)
        self.assertAlmostEqual(st(f)["cash"], cash); n = st(f)["closed"][tk]["contracts"]
        self.assertAlmostEqual(cash, 5000 - cost + n, places=6)
        e = ev(f); self.assertEqual(sum(1 for x in e if x["type"] == "fill"), 1); self.assertEqual(sum(1 for x in e if x["type"] == "settle"), 1)
        self.assertEqual(sum(1 for x in e if x["type"] == "outcome"), 1); self.assertEqual(len(e), len({x["id"] for x in e}))
    def test_catch_up_settles_after_downtime(self):
        k, s, f = FakeKalshi(), FakeSpot(79850), files(); tk = k.add(T0 + 900, yes_ask=0.60, no_ask=0.45)
        run(k, s, f, T0 + 400); self.assertEqual(st(f)["positions"][tk]["side"], "no")
        k.results[tk] = "yes"; run(k, s, f, T0 + 6 * 3600)       # back after 6h
        c = st(f)["closed"][tk]; self.assertEqual(c["payout"], 0.0); self.assertAlmostEqual(c["pnl"], -c["cost"])

class KillSwitch(LiveCapitalMode):
    def test_drawdown(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); run(k, s, f, T0)
        x = st(f); x["cash"] = 4200.0; json.dump(x, open(f["state_path"], "w"))
        tk = k.add(T0 + 900); run(k, s, f, T0 + 500)
        y = st(f); self.assertTrue(y["paused"]); self.assertEqual(y["positions"], {}); self.assertTrue(os.path.exists(f["alert_path"]))
    def test_avg_pnl_after_150(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); run(k, s, f, T0)
        x = st(f); x["closed"] = {f"t{i}": dict(pnl=-0.5, contracts=40) for i in range(150)}; x["peak_equity"] = 5000; x["cash"] = 5000
        json.dump(x, open(f["state_path"], "w")); run(k, s, f, T0 + 60)
        y = st(f); self.assertTrue(y["paused"]); self.assertIn("avg P&L", y["pause_reason"])
        a = json.load(open(f["alert_path"]))["alerts"]; self.assertEqual(a[-1]["type"], "kill_switch")
    def test_149_trades_no_kill(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); run(k, s, f, T0)
        x = st(f); x["closed"] = {f"t{i}": dict(pnl=-0.5, contracts=40) for i in range(149)}; json.dump(x, open(f["state_path"], "w"))
        run(k, s, f, T0 + 60); self.assertFalse(st(f)["paused"])

class LogOnly(unittest.TestCase):
    def setUp(self): self.assertTrue(C.LOG_ONLY, "production default must be LOG_ONLY")
    def test_no_capital_but_hypo_logged_both_variants(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); tk = k.add(T0 + 900, yes_ask=0.60)
        run(k, s, f, T0 + 500); x = st(f)
        self.assertEqual(x["positions"], {}); self.assertEqual(x["cash"], 5000.0)
        e = ev(f); chk = [c for c in e if c["type"] == "check"][0]
        self.assertEqual(chk["reason"], "log_only_no_capital"); self.assertAlmostEqual(chk["mid"], (0.60 + 0.42 * 0 + (1 - 0.42)) / 2 if False else (0.60 + 0.58) / 2)
        hf = [c for c in e if c["type"] == "hypo_fill"]; self.assertEqual(sorted(c["variant"] for c in hf), ["ban15", "base"])
        for c in hf: self.assertLessEqual(c["cost"], 25.0); self.assertGreaterEqual(c["edge_all_in"], 0.04 - 1e-9)
        run(k, s, f, T0 + 560); self.assertEqual(sum(1 for c in ev(f) if c["type"] == "hypo_fill"), 2)   # 1 per market per variant
        k.results[tk] = "yes"; run(k, s, f, T0 + 960); run(k, s, f, T0 + 960); run(k, s, f, T0 + 1020)
        hs = [c for c in ev(f) if c["type"] == "hypo_settle"]; self.assertEqual(len(hs), 2)
        self.assertTrue(all(c["pnl"] > 0 for c in hs)); self.assertEqual(st(f)["cash"], 5000.0)
    def test_ban15_skips_cheap_contracts(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); tk = k.add(T0 + 900, yes_ask=0.10, no_ask=0.95)
        run(k, s, f, T0 + 500)
        hf = {c["variant"]: c for c in ev(f) if c["type"] == "hypo_fill"}
        self.assertIn("base", hf); self.assertNotIn("ban15", hf); self.assertEqual(hf["base"]["vwap"], 0.10)
        chk = [c for c in ev(f) if c["type"] == "check"][0]; self.assertEqual(chk["hypo"]["ban15"], "no_edge_or_banned")
    def test_existing_open_position_still_settles(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); tk = k.add(T0 + 900, yes_ask=0.60)
        C.LOG_ONLY = False
        try: run(k, s, f, T0 + 500)
        finally: C.LOG_ONLY = True
        cost = st(f)["positions"][tk]["cost"]; k.results[tk] = "no"; run(k, s, f, T0 + 960)
        self.assertEqual(st(f)["positions"], {}); self.assertAlmostEqual(st(f)["cash"], 5000 - cost)
    def test_kpi_and_gate_files(self):
        k, s, f = FakeKalshi(), FakeSpot(80150), files(); tk = k.add(T0 + 900, yes_ask=0.60)
        run(k, s, f, T0 + 500); k.results[tk] = "yes"; run(k, s, f, T0 + 960)
        kp = json.load(open(f["state_path"] + ".kpi.json")); g = json.load(open(f["state_path"] + ".gate.json"))
        for key in ("n_checks", "brier_model", "brier_market_mid", "gate", "hypothetical", "resolved_trades", "realized_c_per_contract", "fee_drag", "max_dd"):
            self.assertIn(key, kp)
        self.assertEqual(kp["mode"], "LOG_ONLY"); self.assertFalse(g["passed"]); self.assertEqual(g["checks_with_outcome"], 1)
    def test_gate_logic(self):
        ev_ = []
        for i in range(2000):
            tk = f"t{i}"; y = i % 2
            ev_.append(dict(type="check", ticker=tk, p_up=0.9 if y else 0.1, mid=0.8 if y else 0.2, ts=i))
            ev_.append(dict(type="outcome", ticker=tk, result="yes" if y else "no", ts=i))
        for i in range(150):
            ev_.append(dict(type="hypo_fill", variant="ban15", ticker=f"t{i}", fee=0.4, cost=25, ts=i))
            ev_.append(dict(type="hypo_settle", variant="ban15", ticker=f"t{i}", pnl=0.1, contracts=40, cost=25, ts=i))
        stt = {"cash": 5000.0, "positions": {}}
        k, g = KPI.compute(ev_, stt, 3000)
        self.assertTrue(g["passed"]); self.assertEqual(g["eligible_variants"], ["ban15"])
        k, g = KPI.compute(ev_[:-2], stt, 3000); self.assertFalse(g["passed"])          # 149 trades
        bad = [dict(e, mid=0.95 if e.get("mid") == 0.8 else e.get("mid")) if e["type"] == "check" else e for e in ev_]
        bad = [dict(e, mid=0.05) if e["type"] == "check" and e["mid"] == 0.2 else e for e in bad]
        k, g = KPI.compute(bad, stt, 3000); self.assertFalse(g["brier_ok"]); self.assertFalse(g["passed"])
    def test_old_rows_without_mid(self):
        self.assertAlmostEqual(KPI.mid_of(dict(yes_ask=0.6, no_ask=0.42)), 0.59)

if __name__ == "__main__": unittest.main()
