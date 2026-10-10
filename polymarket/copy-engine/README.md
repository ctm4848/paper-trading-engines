# Polymarket copy sleeve (PAPER ONLY, fake $5,000): a DELAYED COPY test

## DELAYED COPY (KPI plan fix 3, Oct 9, 2026)
The box only runs while an agent is awake. An hourly routine runs one pass at :07, so a leader's trade can wait about
60-67 min before we first see it. The copy window is now **120 min** (was 60), which leaves margin for one late or missed
wake. This is a test of copying with up to 2 hours of delay, NOT of fast copying. Fills still use the REAL order book at
our actual fill time, with the 3c slippage cap, the 97c cap and the taker fee. Per copy we log leader_price, our_price,
our_all_in, slippage, lag_s, best_ask, their_ts.
CLV per resolved lot = last CLOB trade price before the resolution reference time (the earlier of Gamma endDate and
closedTime) minus our entry VWAP, in c/share. We use the earlier time so a post-event jump to $1/$0 doesn't count as the
"closing line". The `clv` event is written once per lot and retried if price history isn't available yet.
kpi.json (written every pass, read by the weekly scorecard): mode, copy_window_min, fills, skips_by_reason,
capturable_missed, capture_rate, avg_lag_min, avg_slippage_c, avg_leader_price, avg_our_price, resolved_lots,
resolved_lots_needed (40), verdict_ready, closed_by_mirrored_sell, open_lots, avg_clv_c, clv_n, hit_rate, realized_pnl,
roi_realized, equity, cash, deployed, cash_benchmark, vs_cash, plus per_trader{name: same block}.
capture_rate = fills / (fills + filter-passing misses: seen_too_late, slippage_over_3c, price_above_97c, no_depth,
cap_reached, book_failed). Only leader trades made after the engine started count. Sports, closed and <30-min markets
are filters, not misses.
Leader re-check with executable prices: research/RECHECK.md.
Public read-only data (data-api, Gamma, CLOB book). No login, no keys, no order endpoints.

Follows mountaindewd (0x944e…8c94), perfectnostalgia (0x0ffc…8236), seal7 (0x2b9d…adc0).
Every 5 min (`loop.sh`, at :00:05/:05:05/…): pull each trader's last 500 trades (`takerOnly=false`).
- Copies only NEW BUYs seen within 120 min of their trade, in markets that are open, accepting orders, with >=30 min to endDate.
- Sports/esports always skipped: regex classifier (`filters_classify.py`, the nonsports research filter) OR any Gamma event tag in `SPORT_TAGS`.
- Fill: walks the real CLOB ask book for the budget (fee included). Fee = C x rate x p x (1-p), rate from the market's own
  `feeSchedule` (falls back to the verified category table; `feesEnabled=false` -> 0). Skip if VWAP > their price + 3c or > 97c,
  or if less than $10 fills.
- Size: $75 per trader per market (later adds ignored), $150 per market across traders, open cost <= 40% of equity, cash >= 0.
- Exits: their SELLs are mirrored proportionally (sold / (sold + their remaining size from /positions)), at the real bid book
  minus fee; unfilled remainder retried next poll. Otherwise held and settled at $1/$0 when Gamma shows closed + resolved.
- Each lot records lag_s (our fill time - their trade time) and slippage (our VWAP - their price). Heartbeat shows averages and
  equity vs a 4%/yr cash benchmark. Equity marks open lots at Gamma outcome prices.

Files: engine.py (one run), copycore.py (rules), pm_api.py (read-only HTTP), filters_classify.py, paperlib.py (atomic state,
deduped events, heartbeat, alerts, lock), test_copy.py. state.json, events.jsonl (types: skip, fill, sell_signal, sell,
settle, fetch_error), heartbeat.txt, ALERT.json, loop.log.

Restart safety: trades keyed by tx:asset:side:size:price:ts, fills by trader:market, settlements by lot id, events deduped by id.
Events are written before the atomic state write, so a crash in between replays to the same ids. After downtime it
reprocesses unseen trades: ones still inside 60 min get copied, older ones are logged as seen_too_late; settlements catch up.
Transient fetch failures alert only after 3 consecutive failures; lookup failures retry while inside the 60-min window.

Start: `bash install.sh` (runs the unit tests first; idempotent). Stop: `bash uninstall.sh`. Health: `bash healthcheck.sh`
(exit 1 if loop down, heartbeat > 15 min old, or ALERT.json exists). No cron/systemd here: after a box restart re-run install.sh.
