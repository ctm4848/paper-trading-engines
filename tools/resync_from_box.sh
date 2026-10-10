#!/bin/bash
# Copy the latest engine code + state from the box working dirs into this repo (run on the box, in the repo clone).
# Then re-applies the two repo-only edits (1min helper path via ONEMIN_BIN; no personal names in public files)
# and runs a hygiene scan. Does NOT commit or push. Stop the box loops first so state is final.
set -euo pipefail
cd "$(dirname "$0")/.."
for p in funding-lab/engine polymarket/copy-engine kalshi-btc/engine; do
  [ -d "/workspace/$p" ] || { echo "missing /workspace/$p"; exit 1; }
  find "$p" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -exec rm -rf {} +
  (cd "/workspace/$p" && tar cf - --exclude=__pycache__ --exclude='*.lock' --exclude=loop.pid --exclude='loop.log*' \
     --exclude='.tmp-*' --exclude=research --exclude=.venv .) | (cd "$p" && tar xf -)
done
if [ -f /workspace/paper-trader/weekly_scorecard.py ]; then mkdir -p paper-trader; cp /workspace/paper-trader/weekly_scorecard.py paper-trader/; fi
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
# repo-only edit 2: no personal first name in a public repo
rg -l -i 'chris' . --glob '!tools/resync_from_box.sh' | xargs -r sed -i -E "s/Chris's/the owner's/g; s/Chris-approved/owner-approved/g; s/\bChris\b/the owner/g"
echo "--- hygiene scan (should print nothing below) ---"
rg -n -i "chris|mccall|ctm4848@|@gmail|@outlook|gho_|sk-[a-z0-9]{10}|API-KEY\": *\"[a-z0-9]" . --glob '!tools/resync_from_box.sh' --glob '!tools/onemin' --glob '!.git' || true
rg -n -P "(?<![0-9.])\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?![0-9])" . --glob '!.git' || true
echo "--- done; review 'git status' then commit + push"
