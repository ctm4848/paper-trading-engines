# Funding Lab paper engine (FAKE MONEY ONLY)

Simulates two paper books on OKX public data. It never holds API keys and never calls any order endpoint.

- Book A ($5,000): contrarian short BTC/ETH perps. Entry when trailing-24h funding >= the coin's own trailing-30d p90 AND >= 0.008%/8h, and the 4h close is below the 4h 20 EMA. Risk 1% of equity, stop = 2x 4h ATR(14) capped at 5%, notional capped at 1x per coin and 1.5x total. Kill switch at 15% drawdown.
- Book A_shadow: tracks what a news-blocked Book A short would have done (no kill switch, entries only from blocked signals).
- Book B_0.006 and Book B_0.008 ($5,000 each): long spot + short perp, notional = equity/1.5, split across qualifying coins. Entry on trailing-7d funding >= 0.006% (or 0.008% for the variant).

Costs: perp taker 0.05%, spot taker 0.10%, slippage 2 bps (30 bps on shock/depeg fills). A short RECEIVES positive funding.

## Files
engine.py        one run: fetch, catch up, news, depeg check, save. `python3 engine.py`
core.py          the rule engine (also used by ../backtest/run_backtest.py)
rules.py         constants
okx.py           read-only OKX public data
news.py          RSS/OKX poll, keyword pre-filter, 1min.AI classification
state.json       all state (atomic writes). events.jsonl = append-only log. heartbeat.txt, ALERT.json
loop.sh install.sh uninstall.sh healthcheck.sh

## USDT depeg check
Primary: OKX index `USDT-USD` (5m index candle close). Secondary, recorded alongside: 1/USDC-USDT spot. Book B fully unwinds if USDT-USD < 0.99.

## News layer
Sources: CoinDesk RSS, The Block RSS, Cointelegraph RSS, SEC press releases RSS (official), OKX announcements API (official).
Keyword pre-filter, then survivors go to `/home/box/tools/onemin -m gpt-4o-mini -a paper-trader` (max 10/day, confidence >= 0.7).
Positive catalyst confirmed only with >=2 sources (or 1 official OKX/SEC notice) AND +1.5% within 1h on OKX 5m -> 48h Book A veto.
Shock: flatten Book A for that coin (OKX-scoped shock flattens all), cut 50% on a market-wide shock (at most daily), pause NEW Book B entries 24h (Book B is NOT unwound on news; it fully unwinds only if USDT-USD < 0.99). Fills use 30 bps slippage at the next 5m candle after the poll.

## Restart safety
Fills keyed by order id, funding credits keyed by book:instrument:fundingTime, events deduped by id. Re-running over the same window changes nothing. After downtime it replays missed funding prints and stop touches from 5m candles. 4h indicators come from OKX 4H candles (about 50 days kept).

## Scheduling (NOT installed)
The box has no cron, no crontab and no systemd (PID 1 is a container init), and nothing restarts automatically after a box restart.
- Start: `bash /workspace/funding-lab/engine/install.sh`. Idempotent. Starts a detached loop that runs every 15 min at :01/:16/:31/:46, inherits the env for the 1min key, and is guarded by flock plus the engine lock.
- Stop: `bash /workspace/funding-lab/engine/uninstall.sh`
- Health: `bash /workspace/funding-lab/engine/healthcheck.sh` prints heartbeat age and alerts. Exit 1 if stale (>30 min) or alerting.
- After a box restart the loop is gone. Re-run install.sh, ideally from an agent-side routine that runs healthcheck.sh and calls install.sh when the heartbeat is stale. The engine replays missed funding prints and stop touches from 5m candles.
