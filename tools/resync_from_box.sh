#!/bin/bash
# Copy the latest engine code + state from the box working dirs into this repo (run on the box, in the repo clone).
# Then re-applies the two repo-only edits (1min helper path via ONEMIN_BIN; no personal names in public files)
# and runs a hygiene scan. Does NOT commit or push. Stop the box loops first so state is final.
set -euo pipefail
cd "$(dirname "$0")/.."
for p in funding-lab/engine polymarket/copy-engine kalshi-btc/engine; do
  [ -d "/workspace/$p" ] || { echo "missing /workspace/$p"; exit 1; }
  find "$p" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -exec rm -rf {} +
  # keep pass history (uptime evidence) as compact "<iso> ok" lines; the runner appends to the same file
  if [ -f "/workspace/$p/loop.log" ]; then rg -o -N '^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ok' "/workspace/$p/loop.log" | sed 's/$/ box/' > "$p/loop.log.box" || true; fi
  (cd "/workspace/$p" && tar cf - --exclude=__pycache__ --exclude='*.lock' --exclude=loop.pid --exclude='loop.log*' \
     --exclude='.tmp-*' --exclude=research --exclude=.venv .) | (cd "$p" && tar xf -)
  if [ -f "$p/loop.log.box" ]; then mv "$p/loop.log.box" "$p/loop.log"; fi
done
# weekly scorecard + the dividend tracker it reads (no charts/backtests)
mkdir -p paper-trader
for f in weekly_scorecard.py test_weekly_scorecard.py update_scoreboard.py common.py scoreboard.json SCOREBOARD.md \
         WEEKLY.md WEEKLY.json rule_changes.jsonl escalation_state.json decision.txt; do
  [ -f "/workspace/paper-trader/$f" ] && cp "/workspace/paper-trader/$f" paper-trader/
done
# repo-only edit 1: news.py calls tools/onemin on the runner
python3 - <<'PY'
import re
p = "funding-lab/engine/news.py"; s = open(p).read()
s = s.replace('ONEMIN = "/home/box/tools/onemin"', 'ONEMIN = os.environ.get("ONEMIN_BIN", "/home/box/tools/onemin")   # GitHub runner sets ONEMIN_BIN=tools/onemin')
if not re.search(r"^import os\b|^import os,", s, re.M) and "import os" not in s.split("ONEMIN =")[0]:
    s = re.sub(r"^import ", "import os\nimport ", s, count=1, flags=re.M)
open(p, "w").write(s)
PY
grep -q 'ONEMIN_BIN' funding-lab/engine/news.py || { echo "news.py ONEMIN patch FAILED"; exit 1; }
# repo-only edit 1b: scorecard reads the repo's engine dirs and the repo usage log when PAPER_ENGINES_ROOT is set
python3 - <<'PY'
p = "paper-trader/weekly_scorecard.py"; s = open(p).read()
a = 'ONEMIN = "/home/box/tools/onemin"; ONEMIN_LOG = "/home/box/tools/onemin-usage.log"'
b = ('_ROOT = os.environ.get("PAPER_ENGINES_ROOT")   # set on the GitHub runner (repo checkout); unset on the box\n'
     'ONEMIN = os.environ.get("ONEMIN_BIN", "/home/box/tools/onemin")\n'
     'ONEMIN_LOG = os.path.join(_ROOT, "usage", "onemin-usage.tsv") if _ROOT else "/home/box/tools/onemin-usage.log"')
assert a in s, "scorecard ONEMIN line changed"; s = s.replace(a, b)
a2 = 'PATHS = {"funding": "/workspace/funding-lab/engine", "polymarket": "/workspace/polymarket/copy-engine",\n         "kalshi": "/workspace/kalshi-btc/engine"}'
b2 = ('PATHS = {"funding": "/workspace/funding-lab/engine", "polymarket": "/workspace/polymarket/copy-engine",\n         "kalshi": "/workspace/kalshi-btc/engine"}\n'
      'if _ROOT: PATHS = {"funding": os.path.join(_ROOT, "funding-lab/engine"), "polymarket": os.path.join(_ROOT, "polymarket/copy-engine"),\n'
      '                   "kalshi": os.path.join(_ROOT, "kalshi-btc/engine")}')
assert a2 in s, "scorecard PATHS changed"; s = s.replace(a2, b2)
open(p, "w").write(s)
PY
# repo-only edit 2: no personal first name in a public repo
rg -l -i 'chris' . --glob '!tools/resync_from_box.sh' | xargs -r sed -i -E "s/Chris's Dividend Growers/Dividend Growers/g; s/Chris's/the owner's/g; s/Chris-approved/owner-approved/g; s/\bChris\b/the owner/g"
echo "--- hygiene scan (should print nothing below) ---"
rg -n -i "chris|mccall|ctm4848@|@gmail|@outlook|gho_|sk-[a-z0-9]{10}|API-KEY\": *\"[a-z0-9]" . --glob '!tools/resync_from_box.sh' --glob '!tools/onemin' --glob '!.git' || true
rg -n -P "(?<![0-9.])\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?![0-9])" . --glob '!.git' || true
echo "--- done; review 'git status' then commit + push"
