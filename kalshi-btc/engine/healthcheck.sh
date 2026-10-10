#!/bin/bash
# Prints heartbeat age, loop status, state summary and alerts. Exit 0 = healthy, 1 = stale/alerting/not running.
cd "$(dirname "$0")"
STALE=300 python3 - <<'PY'
import json, os, time, sys
ok = True; stale = int(os.environ["STALE"])
pid = open("loop.pid").read().strip() if os.path.exists("loop.pid") else None
running = bool(pid) and os.path.exists(f"/proc/{pid}")
print(f"LOOP: {'running pid '+pid if running else 'NOT running'}"); ok &= running
if not os.path.exists("heartbeat.txt"): print("HEARTBEAT: none (never ran)"); ok = False
else:
    d = json.load(open("heartbeat.txt")); age = time.time() - d["ts"]/1000
    print(f"HEARTBEAT: {age:.0f}s old"); print("STATE:", json.dumps({k: v for k, v in d.items() if k not in ("ts",)}))
    if age > stale: print(f"STALE: heartbeat older than {stale}s"); ok = False
if os.path.exists("ALERT.json"):
    a = json.load(open("ALERT.json")).get("alerts", []); print(f"ALERTS: {len(a)} (last: {json.dumps(a[-1])[:400] if a else '-'})"); ok = False
else: print("ALERTS: none")
sys.exit(0 if ok else 1)
PY
