#!/bin/bash
# Funding Lab paper engine loop: one run per quarter hour, 60s after :00/:15/:30/:45
# (so the last 5m candle has closed). engine.py has its own lock, so overlap is impossible.
cd "$(dirname "$0")"
while true; do
  python3 engine.py >> loop.log 2>&1 || echo "$(date -u +%FT%TZ) engine exit $?" >> loop.log
  now=$(date +%s); next=$(( (now/900+1)*900 + 60 )); sleep $(( next-now ))
done
