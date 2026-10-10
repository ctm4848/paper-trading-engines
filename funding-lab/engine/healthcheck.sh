#!/bin/bash
# Prints heartbeat age and any alerts. Exit 0 = healthy, 1 = stale or alerting.
cd "$(dirname "$0")"
python3 - <<'PY'
import json, os, time, sys
hb = "heartbeat.txt"; alert = "ALERT.json"
ok = True
if not os.path.exists(hb):
    print("HEARTBEAT: none (engine has never run)"); ok = False
else:
    d = json.load(open(hb)); age = (time.time()*1000 - d["ts"]) / 1000
    print(f"HEARTBEAT: {age:.0f}s old (cursor {d.get('cursor')}, equity {d.get('equity')})")
    if d.get("signal_distance"): print(f"SIGNAL: {d['signal_distance']}")
    if age > 1800:
        print("STALE: heartbeat older than 30 min")
        print("NOTE: after a box pause the loop is usually still alive (its sleep is frozen with the VM). Do NOT kill it;"
              " run one pass instead: python3 engine.py (it catches up). Only run install.sh if loop.pid is not alive.")
        ok = False
if os.path.exists(alert):
    a = json.load(open(alert)).get("alerts", [])
    print(f"ALERTS: {len(a)} (last: {a[-1] if a else '-'})"); ok = False
else:
    print("ALERTS: none")
sys.exit(0 if ok else 1)
PY
