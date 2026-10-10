#!/usr/bin/env python3
"""Always-on runner for the PAPER-ONLY engines (fake money; public read-only data; no orders, no exchange keys).
Runs inside one GitHub Actions job for up to --minutes (default 345 = 5h45m, under the 6h job cap):
  - each engine on its native cadence in its own thread (Kalshi 60 s, Polymarket 300 s, Funding Lab 900 s),
    one pass at a time per engine (the engines also hold their own file locks);
  - runner/heartbeat.json updated after every pass;
  - every ~15 min and at exit: pause all engines between passes, commit state, pull --rebase, push (retry).
Single writer: the workflow's concurrency group allows only one runner at a time.
Idempotency is the engines' own: Funding credits keyed book:instrument:fundingTime and fills by order id;
Kalshi fills/settlements keyed by ticker, checks by ticker:minute; Polymarket trades keyed by tx:asset:side:size:price:ts,
fills by trader:market, settlements by lot id; all events deduped by id. A restart replays to the same ids."""
import argparse, json, os, subprocess, sys, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HB = os.path.join(ROOT, "runner", "heartbeat.json")
# name, dir, period_s, offset_s (aligned to the clock), timeout_s, run command
ENGINES = [
    ("kalshi", "kalshi-btc/engine", 60, 5, 180, ["python3", "engine.py"]),
    ("polymarket", "polymarket/copy-engine", 300, 5, 900, ["python3", "engine.py"]),
    ("funding", "funding-lab/engine", 900, 60, 840, ["python3", "engine.py"]),
]
COMMIT_EVERY = 900
LOCKS = {e[0]: threading.Lock() for e in ENGINES}
HB_LOCK = threading.Lock()
STOP = threading.Event()

def utc(t=None): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t or time.time()))

def write_hb(hb):
    with HB_LOCK:
        hb["ts"] = int(time.time() * 1000); hb["utc"] = utc()
        tmp = HB + ".tmp"
        with open(tmp, "w") as f: json.dump(hb, f, indent=1)
        os.replace(tmp, HB)

def engine_thread(hb, name, d, period, offset, timeout, cmd, deadline):
    st = hb["engines"][name]
    first = True
    while not STOP.is_set():
        now = time.time()
        nxt = now if first else (now // period + 1) * period + offset
        first = False
        if nxt >= deadline: break
        if STOP.wait(max(0, nxt - time.time())): break
        with LOCKS[name]:
            t0 = time.time(); st["last_start"] = utc(t0)
            try:
                p = subprocess.run(cmd, cwd=os.path.join(ROOT, d), capture_output=True, text=True, timeout=timeout)
                rc, out = p.returncode, (p.stdout + p.stderr).strip()
            except subprocess.TimeoutExpired:
                rc, out = "timeout", f"timeout after {timeout}s"
            t1 = time.time()
        st.update(last_end=utc(t1), last_rc=rc, last_secs=round(t1 - t0, 1), passes=st["passes"] + 1)
        if rc == 0: st["ok"] += 1; st["last_ok"] = utc(t1)
        else: st["fails"] += 1; st["last_error"] = out[-600:]
        # compact pass log in the box loop.log format ("<iso> ok ...") so the weekly scorecard's uptime math still works
        with open(os.path.join(ROOT, d, "loop.log"), "a") as f:
            f.write(f"{utc(t0)} {'ok' if rc == 0 else 'fail'} rc={rc} secs={t1-t0:.1f} run={hb.get('run_id')}\n")
        line = out.splitlines()[-1][:400] if out else ""
        print(f"[{utc(t1)}] {name} rc={rc} {t1-t0:.1f}s | {line}", flush=True)
        write_hb(hb)

def git(*a, check=True):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=check)

def commit_push(hb, final=False):
    for l in LOCKS.values(): l.acquire()          # engines pause between passes while we snapshot
    try:
        hb["last_commit_attempt"] = utc(); hb["final"] = final; write_hb(hb)
        git("add", "-A")
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print(f"[{utc()}] commit: nothing to commit", flush=True); return
        git("commit", "-q", "-m", f"state: {utc()} run {hb.get('run_id')}{' (final)' if final else ''}")
        branch = hb["branch"]
        for i in range(6):
            p = git("push", "-q", "origin", f"HEAD:{branch}", check=False)
            if p.returncode == 0:
                hb["last_push"] = utc(); write_hb(hb)
                print(f"[{utc()}] pushed state to {branch}", flush=True); return
            print(f"[{utc()}] push failed ({p.stderr.strip()[-200:]}); pull --rebase and retry {i+1}", flush=True)
            r = git("pull", "-q", "--rebase", "origin", branch, check=False)
            if r.returncode != 0:
                print(f"[{utc()}] rebase failed: {r.stderr.strip()[-300:]}", flush=True); git("rebase", "--abort", check=False)
            time.sleep(3 + 5 * i)
        print(f"[{utc()}] PUSH GAVE UP this cycle; will retry next cycle", flush=True)
    finally:
        for l in LOCKS.values(): l.release()

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--minutes", type=float, default=345)
    ap.add_argument("--only", default="", help="comma list of engines to run (default all)")
    a = ap.parse_args()
    start = time.time(); deadline = start + a.minutes * 60
    only = set(filter(None, a.only.split(",")))
    engines = [e for e in ENGINES if not only or e[0] in only]
    hb = {"run_id": os.environ.get("GITHUB_RUN_ID", "local"), "branch": os.environ.get("GITHUB_REF_NAME", "main"),
          "started": utc(start), "deadline": utc(deadline), "paper_only": True,
          "engines": {e[0]: {"dir": e[1], "period_s": e[2], "passes": 0, "ok": 0, "fails": 0} for e in engines}}
    write_hb(hb)
    th = [threading.Thread(target=engine_thread, args=(hb, *e, deadline), daemon=True) for e in engines]
    for t in th: t.start()
    nxt_commit = start + COMMIT_EVERY
    while time.time() < deadline and any(t.is_alive() for t in th):
        time.sleep(5)
        if time.time() >= nxt_commit:
            commit_push(hb); nxt_commit += COMMIT_EVERY
    STOP.set()
    for t in th: t.join(timeout=900)
    hb["ended"] = utc(); commit_push(hb, final=True)

if __name__ == "__main__":
    main()
