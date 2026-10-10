#!/usr/bin/env python3
"""Polymarket copy sleeve: one run. PAPER ONLY (fake money, public read-only data, no keys, no orders).
Run: python3 engine.py   (the loop calls it every 5 min). Lock -> load state -> poll traders -> copy/mirror/settle
-> append events (deduped) -> atomic state write -> heartbeat. Re-running is safe: trades are keyed, fills keyed by
trader:market, settlements by lot id."""
import os, sys, time, json, traceback
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paperlib as P, copycore as C, pkpi as KPI
from pm_api import PolymarketAPI
STATE, EVENTS, HB, ALERT, LOCK, KPIF = (os.path.join(HERE, f) for f in ("state.json", "events.jsonl", "heartbeat.txt", "ALERT.json", "engine.lock", "kpi.json"))

def run_once(api=None, now=None, state_path=STATE, events_path=EVENTS, hb_path=HB, alert_path=ALERT, kpi_path=None):
    kpi_path = kpi_path or (KPIF if state_path == STATE else state_path + '.kpi.json')
    now = int(now or time.time()); api = api or PolymarketAPI()
    st = P.load_json(state_path) or C.new_state(now)
    eng = C.CopyEngine(st, api, now).run()
    marks = eng.marks()
    summ = C.summary(st, now, marks); st["last_summary"] = summ
    P.append_events(events_path, eng.events)       # deterministic ids -> safe if we crash before the state write
    P.save_json(state_path, st)
    kpi = KPI.compute(KPI.load_events(events_path), st, now, marks); P.save_json(kpi_path, kpi)
    summ["capture_rate"] = kpi["capture_rate"]; summ["resolved_lots"] = kpi["resolved_lots"]
    if eng.alerts: P.write_alert(alert_path, eng.alerts)
    P.write_heartbeat(hb_path, **summ)
    return eng, summ

if __name__ == "__main__":
    with P.run_lock(LOCK):
        try:
            eng, summ = run_once()
            print(time.strftime("%FT%TZ", time.gmtime()), "ok", json.dumps(summ), f"events={len(eng.events)}")
        except Exception as e:
            P.write_alert(ALERT, [{"type": "engine_error", "key": str(int(time.time())//3600), "ts": int(time.time()), "error": traceback.format_exc()[-800:]}])
            raise
