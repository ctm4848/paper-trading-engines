import os, sys, json, tempfile, unittest, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import copycore as C, engine as E, paperlib as P

W1, W2, W3 = list(C.TRADERS)
NOW = 1_800_000_000
def iso(t):
    import datetime as dt; return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class FakeAPI:
    def __init__(self):
        self.tr = {w: [] for w in C.TRADERS}; self.mk = {}; self.tg = {}; self.bk = {}; self.pos = {w: [] for w in C.TRADERS}
    def trades(self, u): return list(self.tr[u])
    def positions(self, u): return self.pos[u]
    def market(self, cid): return copy.deepcopy(self.mk.get(cid))
    def event_tags(self, slug): return self.tg.get(slug, ["politics"])
    def book(self, tok): return self.bk[tok]
    hist = {}
    def price_history(self, tok, a, b): return [(t, p) for t, p in self.hist.get(tok, []) if a <= t <= b]
    def add_market(self, cid, tok, title="Will X win the election?", slug="x-election", end=NOW + 86400, ask=0.40, fees=0.04, depth=10000):
        self.mk[cid] = dict(question=title, endDate=iso(end), closed=False, active=True, acceptingOrders=True, feesEnabled=fees > 0,
                            feeSchedule={"rate": fees}, clobTokenIds=json.dumps([tok, tok + "n"]), outcomePrices=json.dumps([ask, 1 - ask]), umaResolutionStatus=None)
        self.bk[tok] = dict(asks=[(ask, depth), (ask + 0.01, depth)], bids=[(ask - 0.01, depth)])
    def buy(self, w, cid, tok, price=0.40, ts=NOW - 600, size=100, slug="x-election", title="Will X win the election?", tx=None, side="BUY"):
        self.tr[w].append(dict(side=side, asset=tok, conditionId=cid, size=size, price=price, timestamp=ts, slug=slug, eventSlug=slug,
                               title=title, outcome="Yes", transactionHash=tx or f"0x{cid}{ts}{side}{w[:6]}"))

def tmpfiles():
    d = tempfile.mkdtemp(); return {k: os.path.join(d, k) for k in ("state_path", "events_path", "hb_path", "alert_path")}

def run(api, f, now=NOW): return E.run_once(api=api, now=now, **f)
def events(f): return [json.loads(l) for l in open(f["events_path"])] if os.path.exists(f["events_path"]) else []
def state(f): return json.load(open(f["state_path"]))

class FeeTests(unittest.TestCase):
    def test_fee_formula(self):
        self.assertAlmostEqual(C.taker_fee(100, 0.5, 0.04), 1.0)          # 100 * .04 * .5 * .5
        self.assertAlmostEqual(C.taker_fee(100, 0.9, 0.07), 0.63)
        self.assertEqual(C.taker_fee(100, 0.5, 0.0), 0.0)
    def test_fee_rate_source(self):
        self.assertEqual(C.fee_rate({"feesEnabled": False, "feeSchedule": {"rate": .04}}), 0.0)
        self.assertEqual(C.fee_rate({"feesEnabled": True, "feeSchedule": {"rate": .05}}), 0.05)
        self.assertEqual(C.fee_rate({}, ["geopolitics"]), 0.0)
        self.assertEqual(C.fee_rate({}, ["crypto"]), 0.07)
    def test_walk_asks_budget_includes_fee(self):
        sh, notional, fee = C.walk_asks([(0.5, 10), (0.6, 1000)], 75, 0.04)
        self.assertAlmostEqual(notional + fee, 75, places=4)
        self.assertAlmostEqual(fee, 10*.04*.25 + (sh-10)*.04*.6*.4, places=4)
    def test_walk_bids(self):
        sold, gross, fee = C.walk_bids([(0.6, 5), (0.5, 100)], 10, 0.04)
        self.assertEqual(sold, 10); self.assertAlmostEqual(gross, 5.5); self.assertAlmostEqual(fee, round(5*.04*.24 + 5*.04*.25, 5))

class SportsFilter(unittest.TestCase):
    def test_filter(self):
        self.assertTrue(C.is_sports("Lakers vs. Celtics", "nba-lal-bos-2026-10-09", []))
        self.assertTrue(C.is_sports("Will Team Spirit win?", "some-event", ["esports"]))
        self.assertTrue(C.is_sports("Who wins?", "x", ["soccer"]))
        self.assertTrue(C.is_sports("Counter-Strike: NAVI vs FaZe", "cs2-navi-faze-2026-10-09", []))
        self.assertFalse(C.is_sports("Fed decision in October?", "fed-decision-in-october", ["economy", "fed"]))
        self.assertFalse(C.is_sports("Israeli election results in a hung parliament?", "israeli-election", ["politics"]))
    def test_engine_skips_sports(self):
        api = FakeAPI(); f = tmpfiles()
        api.add_market("c1", "t1", title="Lakers vs. Celtics", slug="nba-lal-bos-2026-10-09")
        api.buy(W1, "c1", "t1", slug="nba-lal-bos-2026-10-09", title="Lakers vs. Celtics")
        api.add_market("c2", "t2", title="Who wins the match?", slug="evt2"); api.tg["evt2"] = ["sports", "tennis"]
        api.buy(W1, "c2", "t2", slug="evt2", title="Who wins the match?")
        run(api, f)
        self.assertEqual(state(f)["lots"], {})
        self.assertEqual(sorted(e["reason"] for e in events(f) if e["type"] == "skip"), ["sports_or_esports"] * 2)

class Rules(unittest.TestCase):
    def test_copy_size_lag_slippage(self):
        api = FakeAPI(); f = tmpfiles(); api.add_market("c1", "t1", ask=0.42); api.buy(W1, "c1", "t1", price=0.40)
        run(api, f); st = state(f); l = st["lots"]["lot:%s:c1" % W1]
        self.assertAlmostEqual(l["cost"], 75, places=4); self.assertAlmostEqual(st["cash"], 5000 - 75, places=4)
        self.assertEqual(l["lag_s"], 600); self.assertAlmostEqual(l["slippage"], 0.02, places=6)
    def test_skip_rules(self):
        api = FakeAPI(); f = tmpfiles()
        api.add_market("a", "ta", ask=0.45); api.buy(W1, "a", "ta", price=0.40)                 # >3c worse
        api.add_market("b", "tb", ask=0.98); api.buy(W1, "b", "tb", price=0.97)                 # >97c
        api.add_market("c", "tc"); api.buy(W1, "c", "tc", ts=NOW - 7300)                         # seen >120 min late
        api.add_market("d", "td", end=NOW + 1200); api.buy(W1, "d", "td")                       # <30 min left
        run(api, f)
        self.assertEqual(state(f)["lots"], {})
        self.assertEqual(sorted(e["reason"] for e in events(f) if e["type"] == "skip"),
                         sorted(["slippage_over_3c", "price_above_97c", "seen_too_late", "under_30min_left"]))
    def test_one_entry_per_trader_market_and_market_cap(self):
        api = FakeAPI(); f = tmpfiles(); api.add_market("c1", "t1")
        api.buy(W1, "c1", "t1", ts=NOW - 900); api.buy(W1, "c1", "t1", ts=NOW - 800)    # later add ignored
        api.buy(W2, "c1", "t1", ts=NOW - 700); api.buy(W3, "c1", "t1", ts=NOW - 600)   # third trader hits $150 cap
        run(api, f); st = state(f)
        self.assertEqual(len(st["lots"]), 2)
        self.assertAlmostEqual(sum(l["cost"] for l in st["lots"].values()), 150, places=4)
        reasons = [e["reason"] for e in events(f) if e["type"] == "skip"]
        self.assertIn("already_copied_this_trader_market", reasons); self.assertIn("cap_reached", reasons)
    def test_deploy_cap_40pct_and_cash(self):
        api = FakeAPI(); f = tmpfiles()
        for i in range(40):
            api.add_market(f"m{i}", f"t{i}"); api.buy([W1, W2, W3][i % 3], f"m{i}", f"t{i}", ts=NOW - 1000 + i)
        run(api, f); st = state(f)
        dep = sum(l["cost"] for l in st["lots"].values())
        self.assertLessEqual(dep, 0.40 * 5000 + 1e-3); self.assertGreaterEqual(dep, 0.40 * 5000 - 75)
        self.assertGreaterEqual(st["cash"], 0)
    def test_mirror_sell_proportional(self):
        api = FakeAPI(); f = tmpfiles(); api.add_market("c1", "t1"); api.buy(W1, "c1", "t1", size=100)
        run(api, f)
        api.buy(W1, "c1", "t1", side="SELL", size=50, ts=NOW + 100, price=0.5); api.pos[W1] = [{"asset": "t1", "size": 50}]
        run(api, f, NOW + 300); l = state(f)["lots"]["lot:%s:c1" % W1]
        self.assertAlmostEqual(l["shares"], l["shares0"] / 2, places=6); self.assertEqual(l["status"], "open")
        run(api, f, NOW + 600)    # no re-sell
        self.assertAlmostEqual(state(f)["lots"]["lot:%s:c1" % W1]["shares"], l["shares0"] / 2, places=6)
        self.assertEqual(sum(1 for e in events(f) if e["type"] == "sell"), 1)

class Idempotency(unittest.TestCase):
    def test_restart_no_duplicate_fill_or_settlement(self):
        api = FakeAPI(); f = tmpfiles(); api.add_market("c1", "t1"); api.buy(W1, "c1", "t1")
        run(api, f); run(api, f, NOW + 300); run(api, f, NOW + 300)
        st = state(f); self.assertEqual(len(st["lots"]), 1); self.assertAlmostEqual(st["cash"], 4925, places=4)
        api.mk["c1"].update(closed=True, outcomePrices=json.dumps(["1", "0"]), umaResolutionStatus="resolved")
        run(api, f, NOW + 900); cash1 = state(f)["cash"]
        run(api, f, NOW + 1200); run(api, f, NOW + 1200)
        self.assertEqual(state(f)["cash"], cash1)
        ev = events(f)
        self.assertEqual(sum(1 for e in ev if e["type"] == "fill"), 1); self.assertEqual(sum(1 for e in ev if e["type"] == "settle"), 1)
        self.assertEqual(len(ev), len({e["id"] for e in ev}))
        l = state(f)["lots"]["lot:%s:c1" % W1]; self.assertAlmostEqual(cash1, 4925 + l["shares0"], places=4)
    def test_crash_before_state_write_replays_cleanly(self):
        api = FakeAPI(); f = tmpfiles(); api.add_market("c1", "t1"); api.buy(W1, "c1", "t1")
        run(api, f); saved = open(f["state_path"]).read()
        api.buy(W2, "c1", "t1", ts=NOW + 10)
        run(api, f, NOW + 300); open(f["state_path"], "w").write(saved)   # simulate lost state write
        run(api, f, NOW + 300)
        ev = events(f); self.assertEqual(sum(1 for e in ev if e["type"] == "fill"), 2); self.assertEqual(len(state(f)["lots"]), 2)
    def test_catch_up_after_downtime(self):
        api = FakeAPI(); f = tmpfiles(); run(api, f)
        api.add_market("c1", "t1"); api.buy(W1, "c1", "t1", ts=NOW + 100)   # traded while we were down
        api.add_market("c2", "t2"); api.buy(W2, "c2", "t2", ts=NOW + 3000)
        run(api, f, NOW + 7400)       # ~2h later: first is >120 min old (too late), second (73 min) is copied
        st = state(f); self.assertEqual(list(st["lots"]), ["lot:%s:c2" % W2])

class DelayedCopyAndKPI(unittest.TestCase):
    def test_window_is_120_min(self):
        self.assertEqual(C.MAX_LAG_S, 7200)
        api = FakeAPI(); f = tmpfiles(); api.add_market("c1", "t1"); api.buy(W1, "c1", "t1", ts=NOW - 7100)   # 118 min old
        run(api, f); l = state(f)["lots"]["lot:%s:c1" % W1]; self.assertEqual(l["lag_s"], 7100)
    def test_fill_event_logs_leader_vs_us(self):
        api = FakeAPI(); f = tmpfiles(); api.add_market("c1", "t1", ask=0.42); api.buy(W1, "c1", "t1", price=0.40)
        run(api, f); e = [x for x in events(f) if x["type"] == "fill"][0]
        self.assertEqual(e["leader_price"], 0.40); self.assertEqual(e["our_price"], 0.42); self.assertAlmostEqual(e["slippage"], 0.02)
        self.assertEqual(e["lag_s"], 600); self.assertGreater(e["our_all_in"], e["our_price"])
    def test_clv_on_resolved_lot_once(self):
        api = FakeAPI(); f = tmpfiles(); api.add_market("c1", "t1", ask=0.40, end=NOW + 86400); api.buy(W1, "c1", "t1")
        run(api, f)
        api.hist = {"t1": [(NOW + 3600, 0.50), (NOW + 80000, 0.61), (NOW + 86400 + 10, 0.99)]}
        api.mk["c1"].update(closed=True, outcomePrices=json.dumps(["1", "0"]), umaResolutionStatus="resolved", closedTime=iso(NOW + 89000).replace("T", " ").replace("Z", "+00"))
        run(api, f, NOW + 90000); run(api, f, NOW + 90300)
        l = state(f)["lots"]["lot:%s:c1" % W1]
        self.assertAlmostEqual(l["close_price"], 0.61); self.assertAlmostEqual(l["clv"], 0.21, places=6)   # before endDate, not after
        self.assertEqual(sum(1 for e in events(f) if e["type"] == "clv"), 1)
    def test_clv_retry_when_history_missing(self):
        api = FakeAPI(); f = tmpfiles(); api.add_market("c1", "t1"); api.buy(W1, "c1", "t1"); run(api, f)
        api.mk["c1"].update(closed=True, outcomePrices=json.dumps(["0", "1"]), umaResolutionStatus="resolved")
        run(api, f, NOW + 900); self.assertIsNone(state(f)["lots"]["lot:%s:c1" % W1]["clv"])
        api.hist = {"t1": [(NOW + 800, 0.30)]}; run(api, f, NOW + 1200)
        self.assertAlmostEqual(state(f)["lots"]["lot:%s:c1" % W1]["clv"], -0.10, places=6)
    def test_per_trader_attribution_and_kpi_file(self):
        api = FakeAPI(); f = tmpfiles(); run(api, f, NOW - 8000)          # engine starts before the leader trades
        api.add_market("a", "ta"); api.buy(W1, "a", "ta", price=0.40)                         # fill
        api.add_market("b", "tb", ask=0.45); api.buy(W1, "b", "tb", price=0.40)               # slippage miss
        api.add_market("c", "tc"); api.buy(W2, "c", "tc", ts=NOW - 7300)                       # too late
        api.add_market("d", "td", title="Lakers vs. Celtics", slug="nba-lal-bos-2026-10-09")
        api.buy(W3, "d", "td", slug="nba-lal-bos-2026-10-09", title="Lakers vs. Celtics")     # filtered, not a miss
        run(api, f)
        k = json.load(open(f["state_path"] + ".kpi.json"))
        self.assertEqual(k["fills"], 1); self.assertEqual(k["capturable_missed"], 2); self.assertAlmostEqual(k["capture_rate"], 0.333)
        p = k["per_trader"]
        self.assertEqual(p["mountaindewd"]["fills"], 1); self.assertEqual(p["mountaindewd"]["capture_rate"], 0.5)
        self.assertEqual(p["perfectnostalgia"]["capture_rate"], 0.0); self.assertIsNone(p["seal7"]["capture_rate"])
        self.assertEqual(p["seal7"]["skips_by_reason"], {"sports_or_esports": 1})
        for key in ("avg_lag_min", "avg_slippage_c", "avg_clv_c", "resolved_lots", "resolved_lots_needed", "roi_realized", "vs_cash", "per_trader"):
            self.assertIn(key, k)
        self.assertEqual(k["resolved_lots_needed"], 40)
    def test_roi_after_settlement(self):
        api = FakeAPI(); f = tmpfiles(); api.add_market("c1", "t1", ask=0.50); api.buy(W1, "c1", "t1", price=0.50); run(api, f)
        api.mk["c1"].update(closed=True, outcomePrices=json.dumps(["1", "0"]), umaResolutionStatus="resolved")
        run(api, f, NOW + 900); k = json.load(open(f["state_path"] + ".kpi.json"))
        l = state(f)["lots"]["lot:%s:c1" % W1]
        self.assertEqual(k["resolved_lots"], 1); self.assertAlmostEqual(k["roi_realized"], round((l["shares0"] - 75) / 75, 4))
        self.assertEqual(k["hit_rate"], 1.0)

if __name__ == "__main__": unittest.main(verbosity=1)
