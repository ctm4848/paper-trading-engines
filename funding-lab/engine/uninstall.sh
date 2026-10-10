#!/bin/bash
# Stop the Funding Lab paper engine loop. Keeps state.json/events.jsonl.
cd "$(dirname "$0")"
if [ -f loop.pid ] && kill -0 "$(cat loop.pid)" 2>/dev/null; then
  kill -- -"$(cat loop.pid)" 2>/dev/null || kill "$(cat loop.pid)"
  echo "stopped loop (pid $(cat loop.pid))"
else
  echo "loop not running"
fi
rm -f loop.pid
