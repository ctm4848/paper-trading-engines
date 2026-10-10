"""Funding Lab KPIs (PAPER ONLY). Pure functions over engine state + events; written to kpi.json every pass.
Definitions follow /workspace/paper-trader-review/kpi-plan.md section 3 (Funding Lab)."""
import json, os, time
import rules as R, core as C, news as N

CASH_APY = 0.04
YEAR_MS = 365*C.DAY

def pct(x, nd=4): return None if x is None else f"{100*x:.{nd}f}%"

def signal_distance(st, mkt, now):
    """Current funding vs the entry triggers, per coin. Uses the same features core.py trades on."""
    out = {}; parts = []
    for c in st["coins"]:
        fu = mkt.funding_at(c, now)
        if fu is None: out[c] = None; continue
        f4 = [v for t, v in sorted(mkt.f4.get(c, {}).items()) if t <= now]
        last4 = f4[-1] if f4 else None
        below = (last4["c"] < last4["ema"]) if last4 and last4.get("ema") is not None else None
        tr24, p90, tr7d = fu["tr24"], fu.get("p90"), fu["tr7d"]
        need_a = max(R.FUND_FLOOR, p90) if p90 is not None else None
        a_ready = tr24 is not None and need_a is not None and tr24 >= need_a and below is True
        out[c] = {"funding_ts": fu["T"], "last_print": fu["rate"], "tr24": tr24, "p90_30d": p90, "floor": R.FUND_FLOOR,
                  "bookA_needed": need_a, "bookA_gap": (need_a - tr24) if (need_a is not None and tr24 is not None) else None,
                  "close_below_ema20_4h": below, "bookA_entry_conditions_met": a_ready,
                  "tr7d": tr7d, "bookB_entry_006": R.BOOKB_ENTRY, "bookB_entry_008": 0.00008, "bookB_exit": R.BOOKB_EXIT,
                  "bookB_gap_006": (R.BOOKB_ENTRY - tr7d) if tr7d is not None else None,
                  "last3_negative": fu["last3neg"]}
        parts.append(f"{c} 24h {pct(tr24)} vs {pct(R.FUND_FLOOR,3)} floor and p90 {pct(p90)}"
                     f"{' (4h close<EMA20)' if below else ' (4h close>=EMA20)' if below is False else ''}; "
                     f"7d {pct(tr7d)} vs {pct(R.BOOKB_ENTRY,3)} Book B entry (0.008% variant)")
    return out, " | ".join(parts)

def _equity_curve_from_events(events, book):
    return [(e["ts"], e["books"][book]["equity"]) for e in events if e.get("type") == "snapshot" and book in e.get("books", {})]

def classifier_consistency(events):
    """Duplicate stories classified the same? Stories with >=2 classifications (legacy, before story dedupe) are
    re-clustered by title; since story dedupe each story is classified exactly once (consistent by construction)."""
    cl = [e for e in events if e.get("type") == "news_classified"]
    legacy = [e for e in cl if not e.get("story_id")]
    groups = []
    for e in legacy:
        toks = N.story_tokens(e.get("title", ""))
        for g in groups:
            if abs(e["ts"] - g["ts"]) <= N.STORY_WINDOW_MS and N.same_story(toks, g["tokens"]):
                g["items"].append(e); g["tokens"] |= toks; break
        else:
            groups.append({"ts": e["ts"], "tokens": set(toks), "items": [e]})
    multi = [g for g in groups if len(g["items"]) >= 2]
    agree = [g for g in multi if len({(x.get("result") or {}).get("type") for x in g["items"]}) == 1]
    detail = [{"titles": [x["title"][:90] for x in g["items"]], "sources": [x["source"] for x in g["items"]],
               "types": [(x.get("result") or {}).get("type") for x in g["items"]],
               "conf": [(x.get("result") or {}).get("confidence") for x in g["items"]]} for g in multi]
    deduped = [e for e in cl if e.get("story_id")]
    return {"classifications_total": len(cl),
            "legacy_duplicate_stories": len(multi), "legacy_agreeing": len(agree),
            "legacy_agreement_pct": round(100*len(agree)/len(multi), 1) if multi else None,
            "legacy_disagreements": [d for d, g in zip(detail, multi) if g not in agree],
            "since_story_dedupe": {"classifications": len(deduped),
                                   "multi_source_stories": sum(1 for e in deduped if len(e.get("sources", [])) >= 2),
                                   "note": "one classification per story, so duplicates cannot disagree"},
            "target_pct": 90}

def build_kpi(st, mkt, events, now, runs_24h=None):
    b = st["books"]; A, AS = b["A"], b["A_shadow"]
    ref = st.get("kpi_ref") or {}
    btc_now = st["last_px"].get("BTC:spot")
    bh = None
    if ref.get("btc_start_px") and btc_now:
        bh_val = A["start"] * btc_now / ref["btc_start_px"]
        bh = {"start_ts": ref["start_ts"], "btc_start_px": ref["btc_start_px"], "btc_now_px": btc_now,
              "value": round(bh_val, 2), "pnl": round(bh_val - A["start"], 2), "pct": round(100*(btc_now/ref["btc_start_px"]-1), 2)}
    def book_kpi(bk):
        s = C.summarize(bk); net = bk["equity"] - bk["start"]
        realized = s["price"] + s["funding"] + s["fees_slippage"]
        s["unrealized_open"] = round(net - realized, 2)
        s["equity"] = round(bk["equity"], 2); s["open"] = sorted(bk.get("pos", bk.get("legs", {})))
        return s
    years_live = max((now - ref.get("start_ts", now)) / YEAR_MS, 1e-9)
    cash_pnl = round(A["start"] * CASH_APY * years_live, 2)
    out = {"generated_ts": int(time.time()*1000), "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "cursor": st["cursor"], "start_ts": ref.get("start_ts"), "paper_only": True,
           "cash_benchmark_pnl_per_5k": cash_pnl, "books": {}}
    for n, bk in b.items(): out["books"][n] = book_kpi(bk)
    a = out["books"]["A"]
    out["bookA_vs_btc_buy_hold"] = {"bookA_pnl": a["net_pnl"], "btc_bh": bh,
                                    "diff": round(a["net_pnl"] - bh["pnl"], 2) if bh else None}
    out["bookA_decomposition"] = {k: a[k] for k in ("price", "funding", "fees_slippage", "unrealized_open")}
    sh = out["books"]["A_shadow"]
    out["A_vs_A_shadow"] = {"A_pnl": a["net_pnl"], "shadow_pnl": sh["net_pnl"], "vetoed_trades": sh["trades"] + len(sh["open"]),
                            "veto_value": round(-sh["net_pnl"], 2),
                            "note": "A_shadow holds only trades the news veto blocked; veto_value > 0 means vetoes saved money"}
    bb = {}
    for n in ("B_0.006", "B_0.008"):
        bk = b[n]; dep_years = bk["bars_in"] * C.M5 / YEAR_MS
        net = bk["equity"] - bk["start"]
        apy = (net / bk["start"]) / dep_years if dep_years > 0 else None
        bb[n] = {"net_pnl": round(net, 2), "deployed_days": round(bk["bars_in"]*C.M5/C.DAY, 2),
                 "net_apy_on_deployed": round(100*apy, 2) if apy is not None else None, "cash_apy": 100*CASH_APY,
                 "beats_cash": (apy > CASH_APY) if apy is not None else None,
                 "note": None if apy is not None else "never deployed: no APY yet"}
    out["bookB_net_apy"] = bb
    out["time_in_market_pct"] = {n: out["books"][n]["time_in_market_pct"] for n in b}
    sd, line = signal_distance(st, mkt, now)
    out["signal_distance"] = sd; out["signal_line"] = line
    out["classifier_consistency"] = classifier_consistency(events)
    out["kill_switch"] = {n: bk.get("paused") for n, bk in b.items() if bk["type"] == "A"}
    out["news"] = {"b_pause_until": st.get("b_pause_until"), "vetoes": st.get("vetoes"),
                   "credits_total": st["news"].get("credits_total"), "class_count": st["news"].get("class_count")}
    if runs_24h is not None: out["runs_last_24h"] = runs_24h
    return out
