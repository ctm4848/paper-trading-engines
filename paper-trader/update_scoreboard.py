"""Refresh prices for the PAPER portfolio and print value vs. the SPY benchmark.
Dividends with ex-dates after the start date are credited as (virtual) cash for both sides.
Run:  python update_scoreboard.py        (prints + refreshes SCOREBOARD.md / last_update in scoreboard.json)
      python update_scoreboard.py --no-save   (print only)"""
import json, os, sys
from datetime import datetime
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SCOREBOARD_JSON = os.path.join(HERE, "scoreboard.json")
SCOREBOARD_MD = os.path.join(HERE, "SCOREBOARD.md")


def value_position(h, shares, start):
    px = float(h["Close"].iloc[-1])
    divs = h["Dividends"]
    div_cash = float(shares * divs[divs.index > pd.Timestamp(start)].sum())
    return px, shares * px, div_cash


def refresh(board):
    from common import get_history
    start = board["start_date"]
    tickers = [x["ticker"] for x in board["holdings"]] + [board["benchmark"]["ticker"]]
    hist = get_history(tickers, start=(pd.Timestamp(start) - pd.Timedelta(days=10)).date().isoformat())
    asof = min(h.index[-1] for h in hist.values())
    rows, mv, divs = [], 0.0, 0.0
    for x in board["holdings"]:
        px, v, d = value_position(hist[x["ticker"]].loc[:asof], x["shares"], start)
        rows.append({"ticker": x["ticker"], "price": round(px, 2), "value": round(v, 2), "dividends": round(d, 2),
                     "gain_pct": round(100 * ((v + d) / x["cost_basis"] - 1), 2) + 0.0})
        mv += v; divs += d
    b = board["benchmark"]
    bpx, bv, bd = value_position(hist[b["ticker"]].loc[:asof], b["shares"], start)
    total = mv + divs + board["cash"]
    spy_total = bv + bd
    return {"as_of": str(asof.date()), "positions": rows, "market_value": round(mv, 2), "dividends_cash": round(divs, 2),
            "total_value": round(total, 2), "return_pct": round(100 * (total / board["starting_cash"] - 1), 2) + 0.0,
            "spy_price": round(bpx, 2), "spy_total_value": round(spy_total, 2), "spy_dividends": round(bd, 2),
            "spy_return_pct": round(100 * (spy_total / b["cost_basis"] - 1), 2) + 0.0,
            "lead_vs_spy": round(total - spy_total, 2), "checked_at": datetime.now().isoformat(timespec="seconds")}


def write_markdown(board, upd):
    L = ["# Dividend Growers Paper Portfolio", "",
         "> **Paper trading only - fake money.** Not connected to any brokerage account.", "",
         f"- **Start date:** {board['start_date']} (bought at that day's close)",
         f"- **Starting virtual cash:** ${board['starting_cash']:,.2f}  |  **Uninvested cash:** ${board['cash']:,.2f}",
         f"- **Benchmark:** {board['benchmark']['shares']:.4f} sh SPY @ ${board['benchmark']['buy_price']:,.2f} = ${board['benchmark']['cost_basis']:,.2f}", "",
         "## Starting positions (equal weight, fractional shares)", "",
         "| Ticker | Company | Sector | Div-raise streak (yrs) | Shares | Buy price | Cost basis | Yield at start |",
         "|---|---|---|---|---:|---:|---:|---:|"]
    for h in board["holdings"]:
        L.append(f"| {h['ticker']} | {h['name']} | {h['sector']} | {h['streak']} | {h['shares']:.4f} | ${h['buy_price']:,.2f} | ${h['cost_basis']:,.2f} | {h['ttm_yield_pct_at_start']:.2f}% |")
    if upd:
        L += ["", f"## Latest check - prices as of {upd['as_of']} (run {upd['checked_at']})", "",
              "| Ticker | Price | Value | Dividends | Gain incl. divs |", "|---|---:|---:|---:|---:|"]
        for r in upd["positions"]:
            L.append(f"| {r['ticker']} | ${r['price']:,.2f} | ${r['value']:,.2f} | ${r['dividends']:,.2f} | {r['gain_pct']:+.2f}% |")
        L += ["", "| | Basket | SPY |", "|---|---:|---:|",
              f"| Total value (incl. dividends) | ${upd['total_value']:,.2f} | ${upd['spy_total_value']:,.2f} |",
              f"| Return | {upd['return_pct']:+.2f}% | {upd['spy_return_pct']:+.2f}% |", "",
              f"**{'Beating' if upd['lead_vs_spy'] >= 0 else 'Trailing'} SPY by ${abs(upd['lead_vs_spy']):,.2f}.**"]
    else:
        L += ["", "_No price check yet - run `python update_scoreboard.py`._"]
    L += ["", "Data: Yahoo Finance via yfinance. Dividends are credited as cash on the ex-date (not reinvested)."]
    open(SCOREBOARD_MD, "w").write("\n".join(L) + "\n")


def main():
    board = json.load(open(SCOREBOARD_JSON))
    u = refresh(board)
    print(f"PAPER portfolio (fake money) - prices as of {u['as_of']}  (started {board['start_date']})")
    print(f"{'Ticker':<6} {'Price':>10} {'Value':>11} {'Divs':>8} {'Gain':>8}")
    for r in u["positions"]:
        print(f"{r['ticker']:<6} {r['price']:>10,.2f} {r['value']:>11,.2f} {r['dividends']:>8,.2f} {r['gain_pct']:>+7.2f}%")
    print(f"\nBasket total: ${u['total_value']:,.2f} ({u['return_pct']:+.2f}%)  [cash ${board['cash']:,.2f}, dividends ${u['dividends_cash']:,.2f}]")
    print(f"SPY total:    ${u['spy_total_value']:,.2f} ({u['spy_return_pct']:+.2f}%)  [SPY ${u['spy_price']:,.2f}, dividends ${u['spy_dividends']:,.2f}]")
    print(f"{'Beating' if u['lead_vs_spy'] >= 0 else 'Trailing'} SPY by ${abs(u['lead_vs_spy']):,.2f}")
    if "--no-save" not in sys.argv:
        board["last_update"] = u
        json.dump(board, open(SCOREBOARD_JSON, "w"), indent=2)
        write_markdown(board, u)


if __name__ == "__main__":
    main()
