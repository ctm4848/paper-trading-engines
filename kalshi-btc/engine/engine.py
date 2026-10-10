#!/usr/bin/env python3
"""Kalshi BTC 15-minute paper bot: one run (the loop calls it every minute). PAPER ONLY: public read-only data,
no login, no keys, no orders. Lock -> load -> settle with official results -> kill switch -> check open market ->
append events (deduped) -> atomic state write -> heartbeat. Missed minutes during downtime are NOT traded
retroactively (no honest live book for them); pending positions and outcomes are settled on restart."""
import os, sys, time, json, traceback
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paperlib as P, kcore as C, kpi as KPI
from feeds import KalshiAPI, SpotAPI
STATE, EVENTS, HB, ALERT, LOCK, KPIF, GATEF = (os.path.join(HERE, f) for f in ("state.json", "events.jsonl", "heartbeat.txt", "ALERT.json", "engine.lock", "kpi.json", "gate_status.json"))

def run_once(kalshi=None, spot=None, now=None, state_path=STATE, events_path=EVENTS, hb_path=HB, alert_path=ALERT, kpi_path=None, gate_path=None):
    kpi_path = kpi_path or (KPIF if state_path == STATE else state_path + '.kpi.json')
    gate_path = gate_path or (GATEF if state_path == STATE else state_path + '.gate.json')
    now = now or time.time()
    st = P.load_json(state_path) or C.new_state(now)
    bot = C.Bot(st, kalshi or KalshiAPI(), spot or SpotAPI(), now).run()
    summ = C.summary(st); st["last_summary"] = summ
    P.append_events(events_path, bot.events)
    P.save_json(state_path, st)
    st["log_only"] = C.LOG_ONLY
    kpi, gate = KPI.compute(KPI.load_events(events_path), st, now)
    P.save_json(kpi_path, kpi); P.save_json(gate_path, gate)
    summ["mode"] = kpi["mode"]; summ["gate_passed"] = gate["passed"]; summ["checks_with_outcome"] = gate["checks_with_outcome"]
    if bot.alerts: P.write_alert(alert_path, bot.alerts)
    P.write_heartbeat(hb_path, **summ)
    return bot, summ

if __name__ == "__main__":
    with P.run_lock(LOCK):
        try:
            bot, summ = run_once()
            chk = [e for e in bot.events if e["type"] == "check"]
            c = chk[-1] if chk else {}
            print(time.strftime("%FT%TZ", time.gmtime()), "ok", json.dumps(summ),
                  f"| {c.get('ticker')} tau={c.get('tau_s')} p_up={c.get('p_up')} yes={c.get('yes_ask')} no={c.get('no_ask')} {c.get('decision')}:{c.get('reason') or c.get('side')}")
        except Exception:
            P.write_alert(ALERT, [{"type": "engine_error", "key": str(int(time.time()) // 3600), "ts": int(time.time()), "error": traceback.format_exc()[-800:]}])
            raise
