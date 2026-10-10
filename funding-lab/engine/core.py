"""Funding Lab event-driven core (PAPER ONLY, fake money; never places real orders).
Shared by the backtest (15m bars) and the live paper engine (5m bars).

Conventions (documented in README):
- Bars are keyed by OPEN time (ms UTC). A step at bar `ts` happens in this order:
  1) funding prints with T in (prev_ts, ts] are credited to positions open before T
     (a short RECEIVES positive funding; a position exiting AT T still gets T's print)
  2) if ts is a 4h boundary: signals on the 4h bar that CLOSED at ts (funding prints <= ts)
  3) at 00:00 UTC: schedule Book B daily rehedge for 00:15
  4) Book A 7-day time stops
  5) pending orders whose eligible time <= ts fill at this bar's OPEN (+/- slippage)
     entries signalled at a funding timestamp (00/08/16 UTC) are delayed to T+15min
  6) Book A stop checks on this bar's HIGH (fill at max(stop, open) + slippage)
  7) Book B liquidation check at bar highs
  8) mark-to-market at CLOSE, drawdown, Book A kill switch (flatten at next bar open)
- Idempotency: every fill is keyed by its order id (state['fills']); every funding credit by
  book:instrument:fundingTime (state['funding_done']). Re-processing can never double count.
"""
import bisect, math
import rules as R

H = 3_600_000; M5 = 300_000; M15 = 900_000; H4 = 4*H; H8 = 8*H; DAY = 24*H

def is_funding_ts(t): return t % H8 == 0

# ---------------------------------------------------------------- books / state
def new_book_a(name, equity=5000.0, ema_only=False, shadow=None, kill_switch=True, from_blocks_only=False):
    return {"type":"A","name":name,"cash":equity,"start":equity,"peak":equity,"equity":equity,
            "paused":False,"pause_reason":None,"ema_only":ema_only,"shadow":shadow,
            "kill_switch":kill_switch,"from_blocks_only":from_blocks_only,
            "pos":{},"closed":[],"tot":{"price":0.0,"funding":0.0,"costs":0.0},
            "maxdd":0.0,"bars":0,"bars_in":0}

def new_book_b(name, equity=5000.0, entry_thr=R.BOOKB_ENTRY):
    return {"type":"B","name":name,"cash":equity,"start":equity,"peak":equity,"equity":equity,
            "entry_thr":entry_thr,"legs":{},"closed":[],
            "tot":{"price":0.0,"funding":0.0,"costs":0.0},
            "maxdd":0.0,"bars":0,"bars_in":0,"min_margin_ratio":None,"liquidations":0}

def new_state(books, cursor, coins=("BTC","ETH")):
    return {"version":1,"coins":list(coins),"cursor":int(cursor),"books":{b["name"]:b for b in books},
            "orders":[],"fills":{},"funding_done":{},"vetoes":{},"b_pause_until":0,
            "depeg":False,"last_px":{}}

# ---------------------------------------------------------------- market container
class Market:
    """perp[coin]/spot[coin]: dict ts->(o,h,l,c). fund[coin]: sorted list of (T, rate).
    f4[coin]: dict close_ts -> {'c','ema','atr'}."""
    def __init__(self, coins, perp, spot, fund, f4):
        self.coins=coins; self.perp=perp; self.spot=spot; self.f4=f4
        self.fund = {}
        for c in coins:
            lst = sorted(fund[c]); T=[x[0] for x in lst]; r=[x[1] for x in lst]
            feats = []
            for i in range(len(r)):
                tr24 = sum(r[i-2:i+1])/3 if i>=2 else None
                tr7d = sum(r[i-20:i+1])/21 if i>=20 else None
                last3neg = i>=2 and all(x<0 for x in r[i-2:i+1])
                feats.append({"T":T[i],"rate":r[i],"tr24":tr24,"tr7d":tr7d,"last3neg":last3neg})
            # p90 of the PRIOR 90 trailing-24h values (no lookahead), linear interpolation like pandas
            vals=[f["tr24"] for f in feats]
            for i in range(len(feats)):
                win=vals[i-90:i] if i>=90 else None
                if win and all(v is not None for v in win):
                    s=sorted(win); k=(len(s)-1)*0.9; lo=math.floor(k); hi=math.ceil(k)
                    feats[i]["p90"]=s[lo]+(s[hi]-s[lo])*(k-lo)
                else: feats[i]["p90"]=None
            self.fund[c]={"T":T,"feats":feats}
    def funding_at(self, coin, t):
        d=self.fund[coin]; i=bisect.bisect_right(d["T"], t)-1
        return d["feats"][i] if i>=0 else None
    def prints_between(self, coin, t0, t1):
        d=self.fund[coin]; a=bisect.bisect_right(d["T"], t0); b=bisect.bisect_right(d["T"], t1)
        return d["feats"][a:b]

# ---------------------------------------------------------------- core
class Core:
    def __init__(self, state, mkt, record_curve=False):
        self.s=state; self.m=mkt; self.events=[]; self.alerts=[]
        self.curve = {n:[] for n in state["books"]} if record_curve else None

    # ---- helpers
    def emit(self, eid, t, typ, **kw):
        e={"id":eid,"ts":int(t),"type":typ}; e.update(kw); self.events.append(e); return e
    def px(self, coin, kind, ts, field):
        bars = self.m.perp if kind=="perp" else self.m.spot
        b = bars[coin].get(ts)
        key=f"{coin}:{kind}"
        if b is None: return self.s["last_px"].get(key)
        return b[{"o":0,"h":1,"l":2,"c":3}[field]]
    def book_equity(self, b, ts, field="c"):
        eq=b["cash"]
        if b["type"]=="A":
            for c,p in b["pos"].items():
                eq+=p["qty"]*(p["entry_px"]-self.px(c,"perp",ts,field))
        else:
            for c,l in b["legs"].items():
                eq+=l["qty"]*self.px(c,"spot",ts,field)+l["qty"]*(l["perp_avg"]-self.px(c,"perp",ts,field))
        return eq
    def has_order(self, book, pred):
        return any(o["book"]==book and pred(o) for o in self.s["orders"])
    def add_order(self, o):
        if o["id"] in self.s["fills"] or any(x["id"]==o["id"] for x in self.s["orders"]): return
        self.s["orders"].append(o)

    # ---- main loop
    def run(self, bar_ts_list):
        prev=self.s["cursor"]
        for ts in bar_ts_list:
            if ts<=self.s["cursor"]: continue
            self.step(prev, ts); prev=ts; self.s["cursor"]=ts
        return self.events

    def step(self, prev, ts):
        s=self.s; coins=s["coins"]
        # 1) funding
        for c in coins:
            for f in self.m.prints_between(c, prev, ts):
                T=f["T"]; mark=self.px(c,"perp",ts,"o") if T==ts else s["last_px"].get(f"{c}:perp")
                for b in s["books"].values():
                    qty = b["pos"][c]["qty"] if b["type"]=="A" and c in b["pos"] else \
                          (b["legs"][c]["qty"] if b["type"]=="B" and c in b["legs"] else 0)
                    if qty<=0: continue
                    if b["type"]=="A" and b["pos"][c]["entry_ts"]>=T: continue
                    if b["type"]=="B" and b["legs"][c]["open_ts"]>=T: continue
                    key=f"{b['name']}:{c}-USDT-SWAP:{T}"
                    if key in s["funding_done"]: continue
                    amt=qty*mark*f["rate"]            # short receives positive funding
                    b["cash"]+=amt; b["tot"]["funding"]+=amt
                    (b["pos"][c] if b["type"]=="A" else b["legs"][c])["funding"]+=amt
                    s["funding_done"][key]=round(amt,8)
                    self.emit("fund:"+key, T, "funding", book=b["name"], coin=c, rate=f["rate"],
                              qty=qty, mark=mark, amount=round(amt,6))
        # 2) 4h signals
        if ts % H4 == 0:
            for b in list(s["books"].values()):
                if b["type"]=="A": self.eval_a(b, ts)
                else: self.eval_b(b, ts)
        # 3) daily rehedge
        if ts % DAY == 0:
            for b in s["books"].values():
                if b["type"]=="B" and b["legs"]:
                    self.add_order({"id":f"{b['name']}:rehedge:{ts}","book":b["name"],"kind":"B_rehedge",
                                    "eligible":ts+M15,"prio":1})
        # 4) time stops
        for b in s["books"].values():
            if b["type"]!="A": continue
            for c,p in b["pos"].items():
                if ts-p["entry_ts"]>=7*DAY and not self.has_order(b["name"], lambda o:o.get("coin")==c and o["kind"]!="A_entry"):
                    self.add_order({"id":f"{b['name']}:{c}:time:{p['id']}","book":b["name"],"kind":"A_exit",
                                    "coin":c,"eligible":ts,"prio":0,"reason":"time_stop_7d"})
        # 5) fills
        due=sorted([o for o in s["orders"] if o["eligible"]<=ts], key=lambda o:(o["eligible"],o.get("prio",2),o["id"]))
        for o in due:
            if o["id"] in s["fills"]:
                s["orders"].remove(o); continue
            done=self.execute(o, ts)
            if done is not None:
                s["fills"][o["id"]]=done; s["orders"].remove(o)
        # 6) stops
        for b in s["books"].values():
            if b["type"]!="A": continue
            for c in list(b["pos"]):
                p=b["pos"][c]; hi=self.px(c,"perp",ts,"h"); op=self.px(c,"perp",ts,"o")
                if hi is not None and hi>=p["stop"]:
                    oid=f"{b['name']}:{c}:stop:{p['id']}"
                    if oid in s["fills"]: continue
                    raw=max(p["stop"],op)
                    s["fills"][oid]=self.close_a(b,c,ts,raw,R.SLIP,"stop",1.0,oid)
        # 7) B liquidation check at highs
        for b in s["books"].values():
            if b["type"]!="B" or not b["legs"]: continue
            eqh=self.book_equity(b, ts, "h")
            spot_v=sum(l["qty"]*self.px(c,"spot",ts,"h") for c,l in b["legs"].items())
            perp_n=sum(l["qty"]*self.px(c,"perp",ts,"h") for c,l in b["legs"].items())
            ratio=(eqh-spot_v)/perp_n if perp_n>0 else None
            if ratio is not None:
                b["min_margin_ratio"]=ratio if b["min_margin_ratio"] is None else min(b["min_margin_ratio"],ratio)
                if ratio<=0.005:
                    b["liquidations"]+=1
                    self.emit(f"{b['name']}:liq:{ts}", ts, "liquidation", book=b["name"], margin_ratio=ratio)
                    self.alerts.append({"type":"liquidation","book":b["name"],"ts":ts})
                    self.rebalance_b(b, ts, [], R.SLIP, f"{b['name']}:liq:{ts}", "liquidation", field="h")
        # 8) MTM, drawdown, kill switch
        for c in coins:
            for kind,bars in (("perp",self.m.perp),("spot",self.m.spot)):
                bb=bars[c].get(ts)
                if bb: s["last_px"][f"{c}:{kind}"]=bb[3]
        for b in s["books"].values():
            eq=self.book_equity(b, ts, "c"); b["equity"]=eq
            b["peak"]=max(b["peak"],eq); dd=(b["peak"]-eq)/b["peak"]; b["maxdd"]=max(b["maxdd"],dd)
            b["bars"]+=1
            if (b["type"]=="A" and b["pos"]) or (b["type"]=="B" and b["legs"]): b["bars_in"]+=1
            if b["type"]=="A" and b["kill_switch"] and not b["paused"] and dd>=R.DD_KILL:
                b["paused"]=True; b["pause_reason"]=f"kill_switch dd={dd:.4f}"
                self.emit(f"{b['name']}:kill:{ts}", ts, "kill_switch", book=b["name"], equity=round(eq,2), peak=round(b["peak"],2))
                self.alerts.append({"type":"kill_switch","book":b["name"],"ts":ts,"equity":eq})
                if b["pos"]:
                    self.add_order({"id":f"{b['name']}:kill:{ts}","book":b["name"],"kind":"A_flatten","coin":None,
                                    "eligible":ts+1,"prio":0,"reason":"kill_switch"})
            if self.curve is not None: self.curve[b["name"]].append((ts, round(eq,2)))

    # ---- Book A
    def eval_a(self, b, t):
        s=self.s
        for c in s["coins"]:
            f4=self.m.f4[c].get(t); fu=self.m.funding_at(c, t)
            if f4 is None or fu is None or f4["ema"] is None: continue
            if c in b["pos"]:
                reason=None
                if not b["ema_only"] and fu["tr24"] is not None and fu["tr24"]<=R.FUND_EXIT: reason="funding_tr24<=0.003%"
                elif f4["c"]>f4["ema"]: reason="close>ema20"
                if reason and not self.has_order(b["name"], lambda o:o.get("coin")==c and o["kind"]!="A_entry"):
                    self.add_order({"id":f"{b['name']}:{c}:exit:{t}","book":b["name"],"kind":"A_exit","coin":c,
                                    "eligible":t,"prio":0,"reason":reason})
                continue
            if b["from_blocks_only"]: continue
            if f4["atr"] is None or self.has_order(b["name"], lambda o:o.get("coin")==c and o["kind"]=="A_entry"): continue
            if not f4["c"]<f4["ema"]: continue
            if not b["ema_only"]:
                if fu["tr24"] is None or fu.get("p90") is None: continue
                if not (fu["tr24"]>=fu["p90"] and fu["tr24"]>=R.FUND_FLOOR): continue
            elig = t+M15 if is_funding_ts(t) else t
            o={"id":f"{b['name']}:{c}:entry:{t}","book":b["name"],"kind":"A_entry","coin":c,"eligible":elig,
               "prio":2,"atr":f4["atr"],"signal_t":t,"tr24":fu["tr24"],"p90":fu.get("p90"),"close":f4["c"],"ema":f4["ema"]}
            blocked = "kill_switch_paused" if b["paused"] else ("news_veto" if s["vetoes"].get(c,0)>t else None)
            if blocked:
                self.emit(f"{o['id']}:blocked", t, "entry_blocked", book=b["name"], coin=c, reason=blocked)
                if b["shadow"] and b["shadow"] in s["books"]:
                    so=dict(o); so["id"]="SH:"+o["id"]; so["book"]=b["shadow"]; so["blocked_reason"]=blocked
                    self.add_order(so)
                continue
            self.add_order(o)
            self.emit(f"{o['id']}:signal", t, "signal", book=b["name"], coin=c, side="short",
                      tr24=fu["tr24"], p90=fu.get("p90"), close=f4["c"], ema=f4["ema"], atr=f4["atr"])

    def open_a(self, b, o, ts):
        c=o["coin"]; op=self.px(c,"perp",ts,"o")
        if op is None: return None   # no bar yet: keep pending
        if c in b["pos"] or (b["paused"] and not b["from_blocks_only"]):
            self.emit(o["id"]+":cancel", ts, "order_cancelled", book=b["name"], coin=c, reason="position_open_or_paused")
            return {"status":"cancelled"}
        eq=self.book_equity(b, ts, "o")
        stop_dist=min(R.ATR_MULT*o["atr"]/op, R.STOP_CAP)
        used=sum(p["qty"]*self.px(cc,"perp",ts,"o") for cc,p in b["pos"].items())
        notional=min(R.RISK_PCT*eq/stop_dist, R.LEV_PER_COIN*eq, max(0.0,R.LEV_TOTAL*eq-used))
        if notional<10:
            self.emit(o["id"]+":cancel", ts, "order_cancelled", book=b["name"], coin=c, reason="size_too_small")
            return {"status":"cancelled"}
        slip=o.get("slip",R.SLIP); qty=notional/op
        cost=qty*op*slip+qty*op*(1-slip)*R.PERP_FEE
        b["cash"]-=cost; b["tot"]["costs"]+=cost
        b["pos"][c]={"id":o["id"],"qty":qty,"entry_px":op,"entry_ts":ts,"stop":op*(1+stop_dist),"stop_dist":stop_dist,
                     "funding":0.0,"costs":cost,"price_realized":0.0,"blocked_reason":o.get("blocked_reason")}
        self.emit(o["id"]+":fill", ts, "entry", book=b["name"], coin=c, side="short", qty=qty, px=op,
                  exec_px=op*(1-slip), notional=round(notional,2), stop=op*(1+stop_dist), cost=round(cost,4), equity=round(eq,2))
        return {"status":"filled","px":op,"qty":qty}

    def close_a(self, b, c, ts, raw, slip, reason, frac, oid):
        p=b["pos"][c]; q=p["qty"]*frac
        price=q*(p["entry_px"]-raw); cost=q*raw*slip+q*raw*(1+slip)*R.PERP_FEE
        b["cash"]+=price-cost; b["tot"]["price"]+=price; b["tot"]["costs"]+=cost
        p["price_realized"]+=price; p["costs"]+=cost; p["qty"]-=q
        self.emit(oid+":fill", ts, "exit", book=b["name"], coin=c, reason=reason, qty=q, px=raw,
                  exec_px=raw*(1+slip), price_pnl=round(price,4), cost=round(cost,4))
        if p["qty"]<=1e-12 or frac>=1.0:
            net=p["price_realized"]+p["funding"]-p["costs"]
            tr={"coin":c,"entry_ts":p["entry_ts"],"exit_ts":ts,"reason":reason,"entry_px":p["entry_px"],"exit_px":raw,
                "price":round(p["price_realized"],4),"funding":round(p["funding"],4),"costs":round(p["costs"],4),
                "net":round(net,4),"blocked_reason":p.get("blocked_reason")}
            b["closed"].append(tr); del b["pos"][c]
            self.emit(oid+":trade", ts, "trade_closed", book=b["name"], **tr)
        return {"status":"filled","px":raw,"qty":q}

    # ---- Book B
    def eval_b(self, b, t):
        s=self.s; held=set(b["legs"]); exiting=set(); new=set(); blocked=set()
        for c in s["coins"]:
            fu=self.m.funding_at(c,t)
            if fu is None or fu["tr7d"] is None: continue
            if c in held:
                if fu["tr7d"]<R.BOOKB_EXIT or fu["last3neg"]: exiting.add(c)
            elif fu["tr7d"]>=b["entry_thr"] and not fu["last3neg"]:
                if s["b_pause_until"]>t or s["depeg"]: blocked.add(c)
                else: new.add(c)
        for c in blocked:
            self.emit(f"{b['name']}:{c}:blocked:{t}", t, "entry_blocked", book=b["name"], coin=c,
                      reason="depeg" if s["depeg"] else "news_pause")
        desired=(held-exiting)|new
        if desired!=held and not self.has_order(b["name"], lambda o:o["kind"] in ("B_rebal","B_unwind")):
            elig=t+M15 if is_funding_ts(t) else t
            self.add_order({"id":f"{b['name']}:rebal:{t}","book":b["name"],"kind":"B_rebal","targets":sorted(desired),
                            "eligible":elig,"prio":1,"reason":f"enter={sorted(new)} exit={sorted(exiting)}"})

    def rebalance_b(self, b, ts, targets, slip, oid, reason, field="o"):
        coins=sorted(set(b["legs"])|set(targets))
        for c in coins:
            if self.px(c,"spot",ts,field) is None or self.px(c,"perp",ts,field) is None: return None
        eq=self.book_equity(b, ts, field)
        tgt_notional=(eq/(1+R.BOOKB_BUFFER))/len(targets) if targets else 0.0
        for c in coins:
            sp=self.px(c,"spot",ts,field); pp=self.px(c,"perp",ts,field)
            cur=b["legs"].get(c,{}).get("qty",0.0)
            tq=tgt_notional/sp if c in targets else 0.0
            d=tq-cur
            if abs(d)*sp<1.0 and not (tq==0 and cur>0): continue
            if c not in b["legs"]:
                b["legs"][c]={"qty":0.0,"perp_avg":pp,"open_ts":ts,"funding":0.0,"costs":0.0,"flows":0.0}
            l=b["legs"][c]
            cost=abs(d)*sp*(slip+R.SPOT_FEE*(1+slip))+abs(d)*pp*(slip+R.PERP_FEE*(1+slip))
            if d>0:   # buy spot, add to short
                l["flows"]-=d*sp; b["cash"]-=d*sp
                l["perp_avg"]=(l["perp_avg"]*l["qty"]+pp*d)/(l["qty"]+d) if l["qty"]>0 else pp
            else:     # sell spot, reduce short (realize perp pnl)
                q=-d; l["flows"]+=q*sp; b["cash"]+=q*sp
                real=q*(l["perp_avg"]-pp); l["flows"]+=real; b["cash"]+=real
            l["qty"]+=d; l["costs"]+=cost; b["cash"]-=cost; b["tot"]["costs"]+=cost
            self.emit(f"{oid}:{c}", ts, "b_trade", book=b["name"], coin=c, reason=reason, d_qty=d, spot=sp, perp=pp, cost=round(cost,4))
            if l["qty"]<=1e-12:
                # price pnl of a closed carry trade = spot flows + realized perp pnl
                price=l["flows"]; b["tot"]["price"]+=price
                tr={"coin":c,"entry_ts":l["open_ts"],"exit_ts":ts,"reason":reason,"price":round(price,4),
                    "funding":round(l["funding"],4),"costs":round(l["costs"],4),"net":round(price+l["funding"]-l["costs"],4)}
                b["closed"].append(tr); del b["legs"][c]
                self.emit(f"{oid}:{c}:trade", ts, "trade_closed", book=b["name"], **tr)
        return {"status":"filled","targets":targets,"equity":eq}

    def rehedge_b(self, b, ts, oid):
        if not b["legs"]: return {"status":"noop"}
        eq=self.book_equity(b, ts, "o")
        spot_v=sum(l["qty"]*self.px(c,"spot",ts,"o") for c,l in b["legs"].items())
        perp_n=sum(l["qty"]*self.px(c,"perp",ts,"o") for c,l in b["legs"].items())
        delta=(spot_v-perp_n)/perp_n if perp_n else 0.0
        mratio=(eq-spot_v)/perp_n if perp_n else None
        need = abs(delta)>R.REHEDGE_BAND or (mratio is not None and not (0.30<=mratio<=0.75))
        self.emit(oid+":check", ts, "b_rehedge_check", book=b["name"], delta=delta, margin_ratio=mratio, action=need)
        if need: return self.rebalance_b(b, ts, sorted(b["legs"]), R.SLIP, oid, "daily_rehedge")
        return {"status":"noop","delta":delta,"margin_ratio":mratio}

    # ---- order dispatcher
    def execute(self, o, ts):
        b=self.s["books"].get(o["book"])
        if b is None: return {"status":"no_book"}
        k=o["kind"]; slip=o.get("slip",R.SLIP)
        if k=="A_entry": return self.open_a(b, o, ts)
        if k in ("A_exit","A_flatten","A_cut50"):
            coins=[o["coin"]] if o.get("coin") else list(b["pos"])
            res={}
            for c in coins:
                if c not in b["pos"]: continue
                op=self.px(c,"perp",ts,"o")
                if op is None: return None
                res[c]=self.close_a(b,c,ts,op,slip,o.get("reason",k),0.5 if k=="A_cut50" else 1.0,f"{o['id']}:{c}")
            return {"status":"filled" if res else "noop","detail":res}
        if k=="B_rebal": return self.rebalance_b(b, ts, o["targets"], slip, o["id"], o.get("reason","rebal"))
        if k=="B_unwind": return self.rebalance_b(b, ts, [], slip, o["id"], o.get("reason","unwind"))
        if k=="B_rehedge": return self.rehedge_b(b, ts, o["id"])
        return {"status":"unknown_kind"}

def summarize(b):
    tr=b["closed"]; n=len(tr); wins=sum(1 for x in tr if x["net"]>0)
    net=b["equity"]-b["start"]
    return {"trades":n,"win_rate_pct":round(100*wins/n,1) if n else None,"net_pnl":round(net,2),
            "net_pct":round(100*net/b["start"],2),
            "price":round(b["tot"]["price"],2),"funding":round(b["tot"]["funding"],2),"fees_slippage":round(-b["tot"]["costs"],2),
            "max_dd_pct":round(100*b["maxdd"],2),"time_in_market_pct":round(100*b["bars_in"]/b["bars"],1) if b["bars"] else None,
            "paused":b.get("paused"),"pause_reason":b.get("pause_reason"),
            "min_margin_ratio":(round(b["min_margin_ratio"],3) if b.get("min_margin_ratio") is not None else None),
            "liquidations":b.get("liquidations")}
