#!/bin/bash
# Start the Funding Lab PAPER engine loop (fake money only). Idempotent.
# This box has no cron and no systemd, so this is a detached, supervised shell loop.
# It inherits this shell's environment (needed for ONEMIN_AI_API_KEY; the key is never
# written to disk or put on a command line). A box restart kills it: re-run this script
# (e.g. from a watchdog that runs healthcheck.sh). The engine catches up on missed funding
# prints and stop touches by itself.
cd "$(dirname "$0")"
if [ -f loop.pid ] && kill -0 "$(cat loop.pid)" 2>/dev/null; then echo "already running (pid $(cat loop.pid))"; exit 0; fi
[ -n "$ONEMIN_AI_API_KEY" ] || echo "WARNING: ONEMIN_AI_API_KEY not set in this shell; news classification will fail (engine still runs)"
nohup setsid flock -n loop.lock bash loop.sh >> loop.log 2>&1 < /dev/null &
echo $! > loop.pid
sleep 1
kill -0 "$(cat loop.pid)" 2>/dev/null && echo "started paper engine loop (pid $(cat loop.pid)); log: $(pwd)/loop.log" || { echo "failed to start"; exit 1; }
