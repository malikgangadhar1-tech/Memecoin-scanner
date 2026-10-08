#!/usr/bin/env python3
"""Revival scanner: (A) capitulation then stabilising base, (B) dev sold at a loss. Prints JSON of NEW alerts."""
import json,os,time,urllib.request,urllib.error
D=os.path.dirname(os.path.abspath(__file__))
WL=os.path.join(D,"rev_watchlist.json"); AL=os.path.join(D,"rev_alerted.json"); DEV=os.path.join(D,"rev_devcache.json")
RPC="https://api.mainnet-beta.solana.com"; now=time.time(); DEADLINE=now+420
def get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}),timeout=25))
def rpc(body):
    for a in range(5):
        if time.time()>DEADLINE+60: raise Exception('deadline')
        try:
            r=urllib.request.Request(RPC,json.dumps(body).encode(),{"Content-Type":"application/json"})
            return json.load(urllib.request.urlopen(r,timeout=30))
        except urllib.error.HTTPError as e:
            if e.code!=429 or a==4: raise
            time.sleep(3*(a+1))
def call(m,p): return rpc({"jsonrpc":"2.0","id":1,"method":m,"params":p}).get("result")
def load(f,d):
    try: return json.load(open(f))
    except Exception: return d
wl=load(WL,{}); al=set(load(AL,[])); devc=load(DEV,{})
# 1. grow watchlist from dexscreener paid profiles + boosts (+ the main scanner's seen list)
for u in ("https://api.dexscreener.com/token-profiles/latest/v1","https://api.dexscreener.com/token-boosts/latest/v1"):
    try:
        for x in get(u):
            if x.get("chainId")=="solana": wl.setdefault(x["tokenAddress"],{"added":now,"links":[l.get("url") for l in x.get("links",[])]})
    except Exception: pass
for m in load(os.path.join(D,"seen.json"),[]): wl.setdefault(m,{"added":now,"links":[]})
wl={k:v for k,v in wl.items() if now-v["added"]<72*3600}
# 2. batch market data
pairs={}
keys=list(wl)
for i in range(0,len(keys),30):
    try:
        for p in get("https://api.dexscreener.com/tokens/v1/solana/"+",".join(keys[i:i+30])):
            m=p["baseToken"]["address"]
            if m not in pairs or (p.get("liquidity") or {}).get("usd",0)>(pairs[m].get("liquidity") or {}).get("usd",0): pairs[m]=p
    except Exception: pass
    time.sleep(0.3)
MIN_HOLDERS=50; MIN_BUYERS_1H=30  # Rohit 21:30: holders 50 (was 100); buyers 30 is Hark default
EARLY_EXIT_MIN=5  # Rohit Oct 7 21:28 IST: within 5 min max
def dev_pnl(mint):
    if mint in devc and now-devc[mint].get("t",0)<1800 and "exit_min" in devc[mint]: return devc[mint]
    before=None;oldest=None
    for _ in range(12):
        p={"limit":1000}
        if before:p["before"]=before
        s=call("getSignaturesForAddress",[mint,p]) or []
        if not s:break
        oldest=s[-1]["signature"];before=oldest
        if len(s)<1000:break
        time.sleep(0.3)
    if not oldest: raise Exception("no signatures")
    tx=call("getTransaction",[oldest,{"encoding":"json","maxSupportedTransactionVersion":0}])
    if not tx: raise Exception("creation tx unavailable")
    creator=tx["transaction"]["message"]["accountKeys"][0]; created_bt=tx.get("blockTime") or 0; last_exit_bt=0
    sigs=call("getSignaturesForAddress",[creator,{"limit":150}]) or []
    spent=recv=0.0; bought=sold=0.0; moved=0.0; recips={}
    for i in range(0,len(sigs),20):
        body=[{"jsonrpc":"2.0","id":j,"method":"getTransaction","params":[s["signature"],{"encoding":"jsonParsed","maxSupportedTransactionVersion":0}]} for j,s in enumerate(sigs[i:i+20])]
        for r in rpc(body):
            t=r.get("result")
            if not t or not t.get("meta") or t["meta"].get("err"): continue
            mt=t["meta"]; ak=[a["pubkey"] if isinstance(a,dict) else a for a in t["transaction"]["message"]["accountKeys"]]
            f=lambda L:sum(float(b["uiTokenAmount"]["uiAmount"] or 0) for b in L if b.get("mint")==mint and b.get("owner")==creator)
            dt=f(mt.get("postTokenBalances",[]))-f(mt.get("preTokenBalances",[]))
            if abs(dt)<1e-9: continue
            if creator not in ak: continue
            k=ak.index(creator); ds=(mt["postBalances"][k]-mt["preBalances"][k])/1e9
            if dt>0: bought+=dt; spent+=max(-ds,0)
            else:
                sold+=-dt; recv+=max(ds,0); last_exit_bt=max(last_exit_bt,t.get("blockTime") or 0)
                if ds<=0.001:
                    moved+=-dt
                    # Rohit Oct 7 21:44: track who received the dev's moved tokens
                    g=lambda L,o:sum(float(b["uiTokenAmount"]["uiAmount"] or 0) for b in L if b.get("mint")==mint and b.get("owner")==o)
                    for o in {b.get("owner") for b in mt.get("postTokenBalances",[]) if b.get("mint")==mint and b.get("owner") not in (creator,None)}:
                        inc=g(mt.get("postTokenBalances",[]),o)-g(mt.get("preTokenBalances",[]),o)
                        if inc>0: recips[o]=recips.get(o,0)+inc
        time.sleep(0.5)
    bal=sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in (call("getTokenAccountsByOwner",[creator,{"mint":mint},{"encoding":"jsonParsed"}]) or {"value":[]})["value"])
    res={"t":now,"creator":creator,"sol_spent":round(spent,3),"sol_recv":round(recv,3),"sold":sold,"moved":moved,"bought":bought,"bal":bal,"recips":recips,"created_bt":created_bt,"last_exit_bt":last_exit_bt,"exit_min":round((last_exit_bt-created_bt)/60,1) if (created_bt and last_exit_bt) else None}
    devc[mint]=res; return res
alerts=[];errs=[];devchecks=0
def add(sig,m,p,dd=None,rng=None,dv=None):
    if f"{m}:{sig}" in al: return
    al.add(f"{m}:{sig}")
    mc=p.get("marketCap") or p.get("fdv") or 0; t=p.get("txns",{}).get("h1",{})
    alerts.append({"signal":"Holders capitulated, now basing" if sig=="A" else "Dev sold at a loss","symbol":p["baseToken"]["symbol"],"name":p["baseToken"]["name"],"mint":m,"mc":round(mc),"drawdown_from_peak_pct":round(dd*100) if dd is not None else None,"range_last_45m_pct":round(rng*100) if rng is not None else None,"h1_buys":t.get("buys"),"h1_sells":t.get("sells"),"dev_sol_in":dv and dv["sol_spent"],"dev_sol_out":dv and dv["sol_recv"],"dev_exit_min":dv and dv.get("exit_min"),"holders":dv and dv.get("holders"),"buyers_1h":dv and dv.get("buyers_1h"),"dev_exit":(dv and ("MOVED" if dv.get("moved",0)>=0.5*max(dv.get("sold",0),1e-9) else "SOLD")),"socials":wl.get(m,{}).get("links"),"dexscreener":p["url"],"age_h":round((now*1000-p.get("pairCreatedAt",now*1000))/3.6e6,1)})
# Signal A
for m,p in pairs.items():
    if time.time()>now+240: break
    try:
        mc=p.get("marketCap") or p.get("fdv") or 0; pc=p.get("priceChange",{}); t=p.get("txns",{}).get("h1",{})
        age_h=(now*1000-p.get("pairCreatedAt",now*1000))/3.6e6
        if mc<5000 or age_h<1 or t.get("buys",0)<15: continue
        if not (pc.get("h6",0)<=-40 or pc.get("h24",0)<=-50): continue
        if abs(pc.get("h1",0))>15 or abs(pc.get("m5",0))>5: continue
        o=sorted(get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{p['pairAddress']}/ohlcv/minute?aggregate=5&limit=288")["data"]["attributes"]["ohlcv_list"])
        time.sleep(2.1)
        if len(o)<15: continue
        hi=max(c[2] for c in o); pk=max(range(len(o)),key=lambda i:o[i][2]); last=o[-9:]
        dd=1-o[-1][4]/hi; rng=max(c[2] for c in last)/min(c[3] for c in last)-1
        if dd>=0.6 and rng<=0.25 and pk<len(o)-9: add("A",m,p,dd,rng)
    except Exception as e: errs.append({"mint":m,"sig":"A","err":str(e)[:120]})
# Signal B (standalone): any watchlist coin; dev P&L is final once dev is fully out, so skip those already settled
cand=[]
for m,p in pairs.items():
    if f"{m}:B" in al: continue
    c=devc.get(m)
    if c and c.get("final"): continue
    if c and now-c.get("t",0)<1200: continue
    cand.append((p.get("priceChange",{}).get("h1",0),m,p))
cand.sort(key=lambda x:x[0])  # biggest recent drops first
for _,m,p in cand[:12]:
    if time.time()>DEADLINE: break
    try:
        dv=dev_pnl(m); devchecks+=1
        mc=p.get("marketCap") or p.get("fdv") or 0
        supply=mc/float(p["priceUsd"]) if p.get("priceUsd") else 0
        out= supply and dv["bal"]/supply<0.01 and dv["sold"]>0
        if out: devc[m]["final"]=True
        alive= mc>=3000 and p.get("txns",{}).get("h1",{}).get("buys",0)>=10
        # Rohit Oct 7 21:27 IST: alert only if dev exited EARLY (fully out within EARLY_EXIT_MIN of creating the coin),
        # i.e. handed it to the community. Late exits are logged to dev_late_rejects.jsonl for grading, not messaged.
        early= dv.get("exit_min") is not None and dv["exit_min"]<=EARLY_EXIT_MIN
        if out and dv["sol_spent"]>=0.3 and dv["sol_recv"]<dv["sol_spent"] and alive:
            # Rohit Oct 7 21:29 IST: coin must have decent holders and buyers (GT holders count, GT pool h1 unique buyers)
            hold=buy1h=None
            try:
                hold=(get(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{m}/info")["data"]["attributes"].get("holders") or {}).get("count"); time.sleep(2)
                buy1h=get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{p['pairAddress']}")["data"]["attributes"]["transactions"]["h1"].get("buyers"); time.sleep(2)
            except Exception as e: errs.append({"mint":m,"sig":"B_holders","err":str(e)[:120]})
            dv["holders"]=hold; dv["buyers_1h"]=buy1h
            decent= hold is not None and buy1h is not None and hold>=MIN_HOLDERS and buy1h>=MIN_BUYERS_1H
            if early and decent: add("B",m,p,dv=dv)
            elif early:
                with open(os.path.join(D,"dev_late_rejects.jsonl"),"a") as fh: fh.write(json.dumps({"t":time.time(),"mint":m,"symbol":p["baseToken"]["symbol"],"mc":round(mc),"exit_min":dv.get("exit_min"),"holders":hold,"buyers_1h":buy1h,"why":"thin"})+"\n")
                devc[m]["final"]=False  # early exit but thin: recheck later as holders grow
            else:
                with open(os.path.join(D,"dev_late_rejects.jsonl"),"a") as fh: fh.write(json.dumps({"t":time.time(),"mint":m,"symbol":p["baseToken"]["symbol"],"mc":round(mc),"exit_min":dv.get("exit_min")})+"\n")
    except Exception as e: errs.append({"mint":m,"sig":"B","err":str(e)[:120]})

# Signal C (Rohit Oct 7 21:44): for MOVED dev alerts, watch the wallets that received the dev's tokens; ping if they sell.
MW=os.path.join(D,"moved_watch.json"); mw=load(MW,{})
for a in alerts:
    if a["signal"]=="Dev sold at a loss" and a.get("dev_exit")=="MOVED":
        r=(devc.get(a["mint"]) or {}).get("recips") or {}
        if r: mw[a["mint"]]={"symbol":a["symbol"],"since":now,"start":{},"recips":r,"pinged":[]}
for m,w in list(mw.items()):
    if now-w["since"]>72*3600: mw.pop(m); continue
    if time.time()>DEADLINE+60: break
    for o in list(w["recips"])[:5]:
        try:
            bal=sum(float(x["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for x in (call("getTokenAccountsByOwner",[o,{"mint":m},{"encoding":"jsonParsed"}]) or {"value":[]})["value"])
        except Exception: continue
        st=w["start"].setdefault(o,max(bal,float(w["recips"][o])))
        if st>0 and bal<=0.8*st and o not in w["pinged"]:
            w["pinged"].append(o); p=pairs.get(m,{})
            alerts.append({"signal":"Side wallet sold","symbol":w["symbol"],"mint":m,"wallet":o,"start_tokens":round(st),"now_tokens":round(bal),"sold_pct":round(100*(1-bal/st)),"mc":round(p.get("marketCap") or p.get("fdv") or 0),"dexscreener":p.get("url") or f"https://dexscreener.com/solana/{m}"})
        time.sleep(0.3)
json.dump(mw,open(MW,"w"))

_lg=open(os.path.join(D,"alerts_log.jsonl"),"a")
for _a in alerts:
    _lg.write(json.dumps({"t":time.time(),"scanner":"revival","signal":_a.get("signal","dex paid"),"mint":_a["mint"],"symbol":_a["symbol"],"mc":_a["mc"]})+"\n")
_lg.close()
json.dump(wl,open(WL,"w")); json.dump(sorted(al),open(AL,"w")); json.dump(devc,open(DEV,"w"))
print(json.dumps({"alerts":alerts,"watchlist":len(wl),"with_data":len(pairs),"dev_checks":devchecks,"errors":errs[:5]},indent=1))
