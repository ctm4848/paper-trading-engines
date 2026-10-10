"""Shared paper-engine plumbing (same pattern as /workspace/funding-lab/engine):
atomic JSON state, append-only events.jsonl deduped by id, heartbeat, ALERT.json, run lock."""
import os, json, time, fcntl, tempfile, contextlib

def atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path)); fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def load_json(path, default=None):
    if not os.path.exists(path): return default
    with open(path) as f: return json.load(f)

def save_json(path, obj):
    atomic_write(path, json.dumps(obj, indent=1, default=float))

def logged_ids(path):
    ids = set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try: ids.add(json.loads(line)["id"])
                except Exception: pass
    return ids

def append_events(path, events):
    """Append-only, deduped by event id: a replay after a crash never duplicates lines."""
    if not events: return 0
    ids = logged_ids(path); n = 0
    with open(path, "a") as f:
        for e in events:
            if e["id"] in ids: continue
            ids.add(e["id"]); f.write(json.dumps(e, default=float) + "\n"); n += 1
        f.flush(); os.fsync(f.fileno())
    return n

def write_heartbeat(path, **kw):
    save_json(path, {"ts": int(time.time() * 1000), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw})

def write_alert(path, alerts):
    """Merge alerts into ALERT.json (deduped by type+key)."""
    prev = (load_json(path, {}) or {}).get("alerts", [])
    seen = {(a.get("type"), a.get("key")) for a in prev}
    for a in alerts:
        k = (a.get("type"), a.get("key"))
        if k not in seen: prev.append(a); seen.add(k)
    if prev: save_json(path, {"alerts": prev[-200:]})

@contextlib.contextmanager
def run_lock(path):
    f = open(path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close(); raise SystemExit("another run holds the lock; exiting")
    try: yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN); f.close()
