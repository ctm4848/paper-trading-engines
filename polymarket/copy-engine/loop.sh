#!/bin/bash
# Polymarket copy sleeve PAPER loop: one engine run every 300 s (aligned to the clock + 5 s). engine.py holds its own lock,
# and this loop runs under flock -n loop.lock, so two loops or overlapping runs are impossible.
cd "$(dirname "$0")"
while true; do
  timeout 900 python3 engine.py >> loop.log 2>&1 || echo "$(date -u +%FT%TZ) engine exit $?" >> loop.log
  if [ $(stat -c %s loop.log) -gt 20000000 ]; then mv loop.log loop.log.1; fi
  now=$(date +%s); next=$(( (now/300+1)*300 + 5 )); sleep $(( next-now ))
done
