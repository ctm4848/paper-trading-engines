#!/bin/bash
# Box-side health check for the GitHub-hosted runner (replaces "restart the box loop").
# Pulls the repo, prints runner + engine heartbeat ages and the latest workflow runs.
# Exit 0 healthy; 1 = stale (runner heartbeat > 20 min, or an engine's last OK pass older than 3x its cadence + 10 min) or ALERT.json present.
cd "$(dirname "$0")/.."
git pull -q --ff-only 2>/dev/null || echo "WARN: git pull failed (using local copy)"
gh run list --repo "${REPO:-ctm4848/paper-trading-engines}" --workflow engines.yml --limit 3 2>/dev/null || echo "WARN: gh run list failed"
python3 - <<'PY'
import json, os, sys, time, datetime as dt
def age(s): return time.time() - dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
ok = True
hb = json.load(open("runner/heartbeat.json"))
a = (time.time()*1000 - hb["ts"]) / 1000
print(f"RUNNER: run {hb.get('run_id')} heartbeat {a/60:.1f} min old (pushed copy; commits every ~15 min), last push {hb.get('last_push')}")
if a > 1200: print("STALE: runner heartbeat > 20 min"); ok = False
for n, e in hb["engines"].items():
    lo = e.get("last_ok"); lim = 3*e["period_s"] + 600
    print(f"  {n}: passes {e['passes']} ok {e['ok']} fails {e['fails']} last_ok {lo} rc {e.get('last_rc')}")
    if not lo or age(lo) > lim + 900: print(f"  STALE: {n} last OK pass too old"); ok = False
for d in ("funding-lab/engine", "polymarket/copy-engine", "kalshi-btc/engine"):
    if os.path.exists(os.path.join(d, "ALERT.json")):
        al = json.load(open(os.path.join(d, "ALERT.json"))).get("alerts", [])
        print(f"ALERT in {d}: {len(al)} (last: {json.dumps(al[-1])[:300] if al else '-'})"); ok = False
sys.exit(0 if ok else 1)
PY
