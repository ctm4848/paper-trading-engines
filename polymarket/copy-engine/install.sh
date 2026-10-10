#!/bin/bash
# Start this PAPER engine loop (fake money only). Idempotent. No cron/systemd on this box, so this is a
# detached setsid + flock loop. A box restart kills it: re-run this script (the engine catches up by itself).
cd "$(dirname "$0")"
if [ -f loop.pid ] && kill -0 "$(cat loop.pid)" 2>/dev/null; then echo "already running (pid $(cat loop.pid))"; exit 0; fi
python3 -W ignore -m unittest -q $(ls test_*.py | sed 's/\.py$//') >/dev/null 2>&1 || { echo "unit tests FAILED; not starting"; exit 1; }
nohup setsid flock -n loop.lock bash loop.sh >> loop.log 2>&1 < /dev/null &
echo $! > loop.pid
sleep 1
kill -0 "$(cat loop.pid)" 2>/dev/null && echo "started paper loop (pid $(cat loop.pid)); log: $(pwd)/loop.log" || { echo "failed to start (another loop holding loop.lock?)"; exit 1; }
