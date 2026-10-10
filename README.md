# Paper-trading engines (FAKE MONEY research project)

**Paper trading only.** Nothing here places real orders, holds exchange keys, or touches a real brokerage or exchange
account. Every engine reads free public market data and keeps a simulated ("paper") ledger in this repo.

| Engine | Dir | Cadence | What it simulates |
|---|---|---|---|
| Funding Lab | `funding-lab/engine` | every 15 min (:01/:16/:31/:46) | Book A contrarian BTC/ETH perp short (+ news-veto shadow), Book B hedged funding carry (0.006% and 0.008% variants). OKX public data; news pre-filter + small-model headline classifier |
| Polymarket copy | `polymarket/copy-engine` | every 5 min | Copies new BUYs of three public leaderboard traders into a fake $5k book |
| Kalshi BTC 15m | `kalshi-btc/engine` | every 1 min | Model-vs-market on Kalshi "BTC up in 15 min?" markets. **Mode flag: `LOG_ONLY` in `kalshi-btc/engine/kcore.py`** |

## How it runs
`.github/workflows/engines.yml` starts one job that runs `runner/run_loop.py` for up to 5h45m (under the 6-hour job cap):
each engine runs in its own thread on its native cadence, `runner/heartbeat.json` is rewritten after every pass, and
state is committed and pushed every ~15 min and at exit (`git pull --rebase` + retry on push failure). A global
concurrency group (`engines`, no cancel) guarantees a single writer. Near the end the job dispatches its successor;
an hourly `:07` schedule is the fallback. Scheduled runs only start when the repo variable `ENGINES_ENABLED` is `true`.

Restarts are safe: the engines key every fill, funding credit, settlement and event by a deterministic id, so a pass that
repeats a window changes nothing.

## Headline classifier budget
Only the Funding Lab uses an LLM (gpt-4o-mini via 1min.AI, key in the `ONEMIN_AI_API_KEY` Actions secret). `tools/onemin`
hard-caps it at 10 calls/UTC day and 300,000 credits/month and logs every call to `usage/onemin-usage.tsv`.

## Files
- `*/state.json`, `*/events.jsonl`, `*/kpi.json`, `*/heartbeat.txt`: paper ledgers and KPIs (written only by the runner)
- `*/loop.log`: one line per pass (uptime evidence)
- `runner/reachability.json`: endpoint check from the runner at job start
- Do not edit state files by hand while a run is active.

## Caveats
- GitHub cron is best-effort (often 5-30 min late, occasionally skipped); the self-dispatch chain does not depend on it.
- Scheduled workflows in public repos are disabled after 60 days without repository activity. The runner's state commits
  should keep the repo active; if the schedule ever shows as disabled, re-enable it in the Actions tab.
