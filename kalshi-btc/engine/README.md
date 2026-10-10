# Kalshi BTC 15-minute paper bot (PAPER ONLY, fake $5,000)

## MODE: LOG-ONLY since Oct 9, 2026 ~9:05 PM CT (KPI plan fix 2, approved by the owner)
`kcore.LOG_ONLY = True`: no paper capital goes to new trades. Positions already open settle normally; then cash is held
($4,925.19 after 3 settled live trades, all losses, -$74.81). Every pass still logs `check` rows (p_up, market mid,
yes_bid, asks, edges, proxy vs ref, decision) and an `outcome` per market.
Hypothetical-trade log: inside the 10..2-minute window, what the current rules WOULD buy ($25, real ask book + fee, 1 per
market, 40/day, edge >= 4c), two ways: `base` (current rules) and `ban15` (no contracts under 15c). Events `hypo_fill`
and `hypo_settle` (settled with Kalshi's official result); state.json["hypo"].

### Gate to restore capital (all three, computed every pass -> gate_status.json and kpi.json["gate"])
1. >= 2,000 `check` rows with a settled `outcome` and a market mid;
2. Brier(model p_up) <= Brier(market mid) on those same rows;
3. the forward hypothetical-trade log for a variant has >= 150 settled trades with net P&L > 0 after fees.
If it passes, capital comes back only with the rules of the variant that passed, and only after the owner is told; nothing
switches automatically (LOG_ONLY is a code constant).
Pace: with only the hourly :07 wake there are about 24 checks/day (one window, ~1 hypothetical trade/hour), so 2,000
checks takes ~3 months. Extra checks accrue whenever an agent keeps the box awake and the 1-minute loop is running.
Caveat: checks within one market are correlated, so 2,000 checks are ~fewer independent markets.

### kpi.json (each pass; read by the weekly scorecard)
mode, n_checks, n_checks_with_outcome, brier_model, brier_market_mid, brier_diff_model_minus_market, calibration
(per 10% bucket: n/model/market/actual), resolved_trades, realized_c_per_contract, roi_on_cost, real_pnl,
edge_at_entry_claimed_c, fee_drag, fees_paid, max_dd, equity, paused, hypothetical{base,ban15:{n,wins,pnl,
c_per_contract,roi,fees,open}}, gate{...}.

### Backtest with the 15c ban (same 7 days)
No ban: 301 trades, -$730, -2.2c/contract. Ban <15c: 297 trades, -$798, -3.3c/contract. The ban does not help: the
<15c bucket was about flat (-0.2c, 48 trades); the losses sit in the 15-30c bucket (-3.4 to -3.8c/contract).
Public trade-api v2 only (markets, market, orderbook). No login, never touches the owner's account, no keys, no orders.

Series KXBTC15M only ("BTC price up in next 15 mins?"). YES wins if the CF Benchmarks BRTI 60-second average just before
close >= floor_strike (the BRTI 60 s average just before open, published by Kalshi on the market).

## Signal: lag vs spot
- Spot proxy for BRTI: median of last prices from 4 BRTI constituent exchanges with free public tickers: Coinbase, Kraken,
  Bitstamp, Gemini (needs >= 2 including Coinbase or Kraken). Binance is geo-blocked and is USDT, not USD.
  Backtest check: Kalshi's official settle value vs our Coinbase/Bitstamp proxy at close differs by 0.2 bp mean, 1.9 bp sd.
- Opening reference: Kalshi's own floor_strike (official), so only the "now" side uses the proxy.
- Vol: realized sd of the last 60 Coinbase 1-min log returns, floored at 3 bp/min.
- P(Up) = Phi(ln(S/K) / sqrt(sigma_s^2 x (tau - 40) + 1.5bp^2)). The tau-40 is the 60 s settlement average: the final-minute
  average of a Brownian path has variance (tau - 60) + 60/3. Clamped to [1%, 99%].
- Trade when p_side - (best ask + 0.07 x p x (1-p)) >= 4c, only 10..2 min before close, $25 incl. fee, whole contracts,
  walking real book depth (YES asks = 1 - NO bids), at most 1 trade/market, 40/UTC day. Cost+fee rounded up to the cent
  (non-direct member rule). Settled only with Kalshi's official result.
- Every minute the open market is logged (`check` events: p_up, asks, edges, proxy vs ref in bp, per-exchange prices,
  vol, decision, reason) and an `outcome` event is written when it settles, for calibration.
- Kill switch: pause at 15% drawdown from peak, or avg P&L < -1c/contract after >= 150 settled trades; ALERT.json written.
  To resume after review: set "paused": false in state.json (deliberate manual step).

Restart: fills keyed by ticker, settlements/outcomes by ticker, check events by ticker:minute. Missed minutes during
downtime are not traded retroactively; open positions and outcomes settle on restart.

Backtest: ../backtest/backtest.py over the stored 7 days (backtest_results.json).

Start: `bash install.sh` (runs tests first). Stop: `bash uninstall.sh`. Health: `bash healthcheck.sh` (exit 1 if loop down,
heartbeat > 5 min old, or ALERT.json exists). Loop runs every minute at :05 s. After a box restart re-run install.sh.
