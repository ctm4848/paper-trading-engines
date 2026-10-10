#!/usr/bin/env python3
"""Funding Lab live paper engine (PAPER ONLY: fake money, no keys, no orders).
Run by hand or from the loop script: `python3 engine.py [--now MS] [--no-news]`.
Each run: lock -> load state -> catch up from cursor to now on 5m bars (funding,
stops, signals, fills) -> news poll -> USDT depeg check -> atomic state write ->
append events -> heartbeat. Safe to run twice over the same window: fills are keyed
by order id and funding credits by book:instrument:fundingTime.
"""
import os, sys, json, time, fcntl, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import okx, news as newsmod, kpi as K
import rules as R, core as C

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
EVENTS = os.path.join(HERE, "events.jsonl")
HEARTBEAT = os.path.join(HERE, "heartbeat.txt")
ALERT = os.path.join(HERE, "ALERT.json")
LOCK = os.path.join(HERE, "engine.lock")
ALERT_ARCHIVE = os.path.join(HERE, "alerts_archive.jsonl")
KPI = os.path.join(HERE, "kpi.json")
RUNS = os.path.join(HERE, "runs.log")
COINS = ["BTC", "ETH"]
M5 = C.M5
WARMUP = 40*C.DAY          # funding history kept (30d p90 needs 93 prints = 31 days)
BAR_LOOKBACK = 6*C.DAY     # 5m bars kept in memory for restarts and stop catch-up

def atomic_write(path, text):
    d = os.path.dirname(path); fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f: f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def load_state():
    if not os.path.exists(STATE): return None
    with open(STATE) as f: return json.load(f)

def init_state(now):
    cursor = (now // M5) * M5 - M5
    books = [C.new_book_a("A", shadow="A_shadow"), C.new_book_a("A_shadow", kill_switch=False, from_blocks_only=True),
             C.new_book_b("B_0.006"), C.new_book_b("B_0.008", entry_thr=0.00008)]
    st = C.new_state(books, cursor, COINS)
    st["created_utc"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now/1000))
    st["bars"] = {c: {"perp": {}, "spot": {}} for c in COINS}   # ts(str)->[o,h,l,c]
    st["funding_hist"] = {c: [] for c in COINS}                 # [[T, rate]]
    st["news"] = newsmod.new_news_state()
    st["usdt_usd"] = None
    return st

def save_state(st):
    st["saved_utc"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    atomic_write(STATE, json.dumps(st))

def logged_ids():
    ids = set()
    if os.path.exists(EVENTS):
        with open(EVENTS) as f:
            for line in f:
                try: ids.add(json.loads(line)["id"])
                except Exception: pass
    return ids

def append_events(events):
    """Append-only log, deduped by event id (so a replay after a crash never duplicates lines)."""
    if not events: return
    ids = logged_ids()
    with open(EVENTS, "a") as f:
        for e in events:
            if e["id"] in ids: continue
            ids.add(e["id"]); f.write(json.dumps(e, default=float) + "\n")
        f.flush(); os.fsync(f.fileno())

def alert_key(a):
    return f"{a.get('type')}|{a.get('book')}|{a.get('coin')}|{a.get('ts')}"

def archived_alert_keys():
    keys = set()
    if os.path.exists(ALERT_ARCHIVE):
        with open(ALERT_ARCHIVE) as f:
            for line in f:
                try: keys.add(json.loads(line)["key"])
                except Exception: pass
    return keys

def write_alert(alerts, err=None):
    """ALERT.json holds UNACKNOWLEDGED alerts. It is only rewritten when a genuinely new alert arrives:
    an alert already in ALERT.json or already acknowledged (alerts_archive.jsonl) is never re-added.
    (Before Oct 9 2026 this rewrote the whole file every run, so an old alert stayed forever.)"""
    new = list(alerts)
    if err: new.append({"type": "engine_error", "ts": int(time.time()*1000), "error": str(err)[:500]})
    if not new: return
    prev = []
    if os.path.exists(ALERT):
        try: prev = json.load(open(ALERT)).get("alerts", [])
        except Exception: prev = []
    seen = {alert_key(a) for a in prev} | archived_alert_keys()
    added = False
    for a in new:
        k = alert_key(a)
        if k not in seen: prev.append(a); seen.add(k); added = True
    if added: atomic_write(ALERT, json.dumps({"alerts": prev[-200:]}, indent=1, default=float))

def ack_alerts(note, who="paper-trader"):
    """Acknowledge every alert in ALERT.json: append each to alerts_archive.jsonl (with key, time, note), then remove
    ALERT.json. Acknowledged alerts are never re-asserted. Returns the number archived."""
    if not os.path.exists(ALERT): return 0
    alerts = json.load(open(ALERT)).get("alerts", [])
    with open(ALERT_ARCHIVE, "a") as f:
        for a in alerts:
            f.write(json.dumps({"key": alert_key(a), "acked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "acked_by": who, "note": note, "alert": a}, default=float) + "\n")
        f.flush(); os.fsync(f.fileno())
    os.unlink(ALERT)
    return len(alerts)

def read_events():
    out = []
    if os.path.exists(EVENTS):
        with open(EVENTS) as f:
            for line in f:
                try: out.append(json.loads(line))
                except Exception: pass
    return out

def set_kpi_ref(st):
    """BTC buy-and-hold reference: BTC-USDT spot close of the first 5m bar the engine processed (state creation)."""
    if st.get("kpi_ref"): return
    import calendar
    t0 = calendar.timegm(time.strptime(st["created_utc"], "%Y-%m-%d %H:%M:%S")) * 1000
    c0 = (t0 // M5) * M5 - M5
    bars = st["bars"]["BTC"]["spot"]
    ks = sorted(int(k) for k in bars if int(k) >= c0)
    if ks: st["kpi_ref"] = {"start_ts": ks[0], "btc_start_px": bars[str(ks[0])][3], "source": "OKX BTC-USDT 5m close"}

def runs_last_24h(now):
    if not os.path.exists(RUNS): return None
    n = 0
    with open(RUNS) as f:
        for line in f:
            try:
                if int(line.split()[1]) >= now - C.DAY: n += 1
            except Exception: pass
    return n

def trim(st, now):
    cut_b = now - BAR_LOOKBACK; cut_f = now - WARMUP
    for c in COINS:
        for k in ("perp", "spot"):
            st["bars"][c][k] = {t: v for t, v in st["bars"][c][k].items() if int(t) >= cut_b}
        st["funding_hist"][c] = [p for p in st["funding_hist"][c] if p[0] >= cut_f]
    st["funding_done"] = {k: v for k, v in st["funding_done"].items() if int(k.rsplit(":",1)[-1]) >= cut_f}

def fetch_new(st, now):
    start = st["cursor"] - M5
    for c in COINS:
        for kind, inst in (("perp", f"{c}-USDT-SWAP"), ("spot", f"{c}-USDT")):
            for ts, o, h, l, c_, conf in okx.candles(inst, "5m", start, now):
                if conf == "1": st["bars"][c][kind][str(ts)] = [o, h, l, c_]
        st.setdefault("bars4h", {})[c] = [[ts, o, h, l, c_] for ts, o, h, l, c_, conf in
                                          okx.candles(f"{c}-USDT-SWAP", "4H", now - 300*C.H4, now) if conf == "1" and ts + C.H4 <= now]
        known = {p[0] for p in st["funding_hist"][c]}
        for T, rate in okx.funding_history(f"{c}-USDT-SWAP", now, pages=2):
            if T not in known and T >= now - WARMUP: st["funding_hist"][c].append([T, rate])
        st["funding_hist"][c].sort()

def build_market(st):
    def conv(d): return {int(k): tuple(v) for k, v in d.items()}
    perp = {c: conv(st["bars"][c]["perp"]) for c in COINS}
    spot = {c: conv(st["bars"][c]["spot"]) for c in COINS}
    f4 = {}
    for c in COINS:
        import pandas as pd
        rows = sorted(tuple(r) for r in st.get("bars4h", {}).get(c, []))
        if not rows: f4[c] = {}; continue
        g = pd.DataFrame(rows, columns=["ts","o","h","l","c"])
        g["t"] = pd.to_datetime(g.ts, unit="ms", utc=True); g = g.set_index("t")
        g["ema"] = R.ema20(g["c"]); g["atr"] = R.atr14_4h(g["h"], g["l"], g["c"])
        f4[c] = {int(t.timestamp()*1000)+C.H4: {"c": r.c, "ema": None if pd.isna(r.ema) else float(r.ema),
                                                 "atr": None if pd.isna(r.atr) else float(r.atr)} for t, r in g.iterrows()}
    fund = {c: [(int(T), float(r)) for T, r in st["funding_hist"][c]] for c in COINS}
    return C.Market(COINS, perp, spot, fund, f4)

def apply_news_actions(st, actions, now):
    """Shock orders act at the next 5m candle that opens at/after the poll time, with shock slippage."""
    nxt = -(-now // M5) * M5
    orders = []
    for a in actions:
        if a["type"] == "veto":
            st["vetoes"][a["coin"]] = max(st["vetoes"].get(a["coin"], 0), a["until"])
        elif a["type"] == "shock":
            st["b_pause_until"] = max(st["b_pause_until"], now + R.BOOKB_SHOCK_PAUSE_MS)   # pause NEW Book B entries only
            for bn in (("B_0.006", "B_0.008") if R.BOOKB_UNWIND_ON_SHOCK else ()):          # disabled: unwind only on depeg
                if st["books"].get(bn, {}).get("legs"):
                    orders.append({"id": f"{bn}:shock_unwind:{now}", "book": bn, "kind": "B_unwind", "eligible": nxt,
                                   "prio": 0, "slip": R.SHOCK_SLIP, "reason": "news_shock"})
            if a["scope"] == "market":
                if st["news"]["last_market_cut"] and now - st["news"]["last_market_cut"] < C.DAY: continue
                st["news"]["last_market_cut"] = now
                orders.append({"id": f"A:shock50:{now}", "book": "A", "kind": "A_cut50", "coin": None,
                               "eligible": nxt, "prio": 0, "slip": R.SHOCK_SLIP, "reason": "market_shock"})
            else:
                coin = a["coin"] if a["scope"] == "coin" else None
                if coin is None:
                    for c in COINS:
                        orders.append({"id": f"A:{c}:shock:{now}", "book": "A", "kind": "A_flatten", "coin": c,
                                       "eligible": nxt, "prio": 0, "slip": R.SHOCK_SLIP, "reason": "okx_shock"})
                else:
                    orders.append({"id": f"A:{coin}:shock:{now}", "book": "A", "kind": "A_flatten", "coin": coin,
                                   "eligible": nxt, "prio": 0, "slip": R.SHOCK_SLIP, "reason": f"shock_{coin}"})
    return orders

def run_once(now=None, do_news=True, fetcher=None, runner=None, price_override=None):
    now = int(now if now is not None else time.time()*1000)
    st = load_state() or init_state(now)
    events = []; alerts = []
    try:
        fetch_new(st, now)
        trim(st, now)
        mkt = build_market(st)
        core = C.Core(st, mkt)
        end = (now // M5) * M5 - M5                     # last COMPLETE 5m bar
        for c in COINS:
            for k in ("perp", "spot"):
                have = [int(t) for t in st["bars"][c][k]]
                end = min(end, max(have) if have else st["cursor"])   # never move past missing data
        if end > st["cursor"]:
            core.run(range(st["cursor"] + M5, end + M5, M5))
        # USDT depeg: OKX index USDT-USD (see okx.usdt_usd)
        u = okx.usdt_usd(now); st["usdt_usd"] = u.get("usdt_usd")
        events.append({"id": f"usdt:{end}", "ts": end, "type": "usdt_check", **{k: u[k] for k in u if "error" not in k}})
        if st["usdt_usd"] is not None and st["usdt_usd"] < R.DEPEG_LEVEL and not st["depeg"]:
            st["depeg"] = True
            st["orders"].append({"id": f"depeg:{end}", "book": "B_0.006", "kind": "B_unwind", "eligible": end+M5, "prio": 0, "slip": R.SHOCK_SLIP, "reason": "usdt_depeg"})
            st["orders"].append({"id": f"depeg2:{end}", "book": "B_0.008", "kind": "B_unwind", "eligible": end+M5, "prio": 0, "slip": R.SHOCK_SLIP, "reason": "usdt_depeg"})
            alerts.append({"type": "depeg_unwind", "ts": end, "usdt_usd": st["usdt_usd"]})
        # news
        if do_news:
            def price_fn(coin, a, b):
                return [(int(t), *v) for t, v in st["bars"][coin]["perp"].items() if a <= int(t) <= b]
            acts, nev = newsmod.poll(st["news"], now, price_fn, runner=runner, fetcher=fetcher)
            events += nev
            for o in apply_news_actions(st, acts, now):
                if o["id"] not in st["fills"] and not any(x["id"] == o["id"] for x in st["orders"]):
                    st["orders"].append(o)
            for a in acts:
                if a["type"] == "shock":
                    alerts.append({"type": "shock", "ts": now, "scope": a["scope"], "coin": a["coin"], "title": a["title"]})
                    st["news"]["shock_log"].append({"ts": now, "scope": a["scope"], "coin": a["coin"], "title": a["title"], "url": a["url"]})
        events += core.events
        alerts += core.alerts
        # snapshot
        snap = {n: {"equity": round(b["equity"], 2), "paused": b.get("paused"), "positions": list(b.get("pos", {})),
                    "legs": list(b.get("legs", {})), "net_pct": round(100*(b["equity"]/b["start"]-1), 2)} for n, b in st["books"].items()}
        events.append({"id": f"snapshot:{end}", "ts": end, "type": "snapshot", "books": snap, "usdt_usd": st["usdt_usd"],
                       "pending_orders": len(st["orders"])})
        try:   # KPIs are reporting only: a KPI bug must never block trading/state
            set_kpi_ref(st)
            kpi = K.build_kpi(st, mkt, read_events() + events, now, runs_last_24h(now))
            st["signal_line"] = kpi["signal_line"]
            atomic_write(KPI, json.dumps(kpi, indent=1, default=float))
        except Exception as ke:
            events.append({"id": f"kpi_error:{now}", "ts": now, "type": "kpi_error", "error": repr(ke)[:300]})
    except Exception as e:
        import traceback; tb = traceback.format_exc()[-1500:]
        events.append({"id": f"error:{now}", "ts": now, "type": "engine_error", "error": repr(e), "trace": tb})
        alerts.append({"type": "engine_error", "ts": now, "error": repr(e)})
        raise
    finally:
        save_state(st); append_events(events); write_alert(alerts)
        atomic_write(HEARTBEAT, json.dumps({"ts": int(time.time()*1000), "cursor": st["cursor"],
                     "equity": {n: round(b["equity"],2) for n, b in st["books"].items()},
                     "signal_distance": st.get("signal_line")}))
        with open(RUNS, "a") as f:   # one line per pass (used for uptime): iso_utc now_ms cursor
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now/1000))} {now} {st['cursor']}\n")
    return st

if __name__ == "__main__":
    args = sys.argv[1:]; now = None; do_news = True
    if "--now" in args: now = int(args[args.index("--now")+1])
    if "--no-news" in args: do_news = False
    if "--ack-alerts" in args:   # python3 engine.py --ack-alerts "reason"
        note = args[args.index("--ack-alerts")+1] if len(args) > args.index("--ack-alerts")+1 else "acknowledged"
        print(f"archived {ack_alerts(note)} alert(s) to {ALERT_ARCHIVE}"); sys.exit(0)
    with open(LOCK, "w") as lf:
        try: fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: print("another run holds the lock; exiting"); sys.exit(0)
        st = run_once(now=now, do_news=do_news)
    print(json.dumps({"cursor": st["cursor"], "usdt_usd": st["usdt_usd"],
                      "equity": {n: round(b["equity"],2) for n, b in st["books"].items()},
                      "positions": {n: list(b.get("pos", {})) for n, b in st["books"].items()},
                      "pending": [o["id"] for o in st["orders"]], "signal_distance": st.get("signal_line")}, indent=1))
