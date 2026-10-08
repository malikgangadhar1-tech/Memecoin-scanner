#!/usr/bin/env python3
"""Incremental KOL convergence check (Solana).
State: data/kol_state.json  {wallets:{kol:{newest,oldest,backfill_done}}, buys:[{mint,kol,ts,sig}]}
Each run: fetch new sigs (until=newest), then backfill older sigs (before=oldest) down to 24h cutoff, within a time budget.
Prints JSON {stats, hits(new, >=3 kols, not alerted), multi2}. --mark appends hit mints to kol_alerted.json.
"""
import json, time, csv, sys, os, urllib.request
from concurrent.futures import ThreadPoolExecutor
BASE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RPCS=["https://solana-rpc.publicnode.com","https://api.mainnet-beta.solana.com"]
IGNORE={"So11111111111111111111111111111111111111112","EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v","Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
BUDGET=float(os.environ.get("BUDGET","95"))
T0=time.time()
def post(R,b):
    req=urllib.request.Request(R,data=json.dumps(b).encode(),headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req,timeout=25))
def rpc(method,params,tries=6):
    for i in range(tries):
        try:
            r=post(RPCS[i%2==1 and method=="getSignaturesForAddress"],{"jsonrpc":"2.0","id":1,"method":method,"params":params})
            if "result" in r: return r["result"],None
            err=r.get("error",{})
            if err.get("code")==-32015: return None,"version"
        except Exception as e: pass
        time.sleep(0.6*(i+1))
    return None,"fail"
def gettx(sig):
    for v in (0,1):
        r,e=rpc("getTransaction",[sig,{"encoding":"jsonParsed","maxSupportedTransactionVersion":v}])
        if e!="version": return r
    return None
now=time.time(); cutoff=now-86400
sp=f"{BASE}/data/kol_state.json"
st=json.load(open(sp)) if os.path.exists(sp) else {"wallets":{},"buys":[]}
wallets=list(csv.DictReader(open(f"{BASE}/data/kol_wallets.csv")))
seen={b["sig"]+b["mint"] for b in st["buys"]}
def process(kol,addr,sigs):
    sigs=[s for s in sigs if s.get("err") is None and (s.get("blockTime") or 0)>=cutoff]
    def one(s):
        if time.time()-T0>BUDGET: return s["signature"],None,"skip"
        return s["signature"],gettx(s["signature"]),"ok"
    with ThreadPoolExecutor(6) as ex: res=list(ex.map(one,sigs))
    done=[]
    for s,(sig,tx,flag) in zip(sigs,res):
        if flag=="skip": break
        done.append(s)
        if not tx or not tx.get("meta"): continue
        m=tx["meta"]
        def bal(l):
            d={}
            for b in l or []:
                if b.get("owner")==addr: d[b["mint"]]=d.get(b["mint"],0)+float(b["uiTokenAmount"].get("uiAmount") or 0)
            return d
        pre,post_=bal(m.get("preTokenBalances")),bal(m.get("postTokenBalances"))
        for mint,amt in post_.items():
            if mint in IGNORE or amt<=pre.get(mint,0): continue
            if sig+mint in seen: continue
            seen.add(sig+mint); st["buys"].append({"mint":mint,"kol":kol,"ts":s["blockTime"],"sig":sig})
    return done
stats={}
order=[]
for w in wallets:
    kol,addr=w["kol"],w["solana_wallet"]
    ws=st["wallets"].setdefault(kol,{"newest":None,"oldest":None,"backfill_done":False})
    stats[kol]={"new":0,"backfill":0}
    # new sigs
    p={"limit":1000}
    if ws["newest"]: p["until"]=ws["newest"]
    res,_=rpc("getSignaturesForAddress",[addr,p]); res=res or []
    res=[s for s in res if (s.get("blockTime") or 0)>=cutoff]
    if res:
        if ws["newest"] is None: ws["oldest"]=None  # first run: backfill from top
        done=process(kol,addr,res)  # newest-first
        stats[kol]["new"]=len(done)
        if done:
            if len(done)==len(res): ws["newest"]=res[0]["signature"]
            else:
                # partial: treat processed top slice as newest; unprocessed gap becomes backfill
                ws["newest"]=res[0]["signature"]; ws["oldest"]=done[-1]["signature"]; ws["backfill_done"]=False; continue
            if ws["oldest"] is None: ws["oldest"]=done[-1]["signature"]
for w in wallets:
    kol,addr=w["kol"],w["solana_wallet"]; ws=st["wallets"][kol]
    if ws["backfill_done"] or not ws["oldest"] or time.time()-T0>BUDGET: continue
    res,_=rpc("getSignaturesForAddress",[addr,{"limit":300,"before":ws["oldest"]}]); res=res or []
    inwin=[s for s in res if (s.get("blockTime") or 0)>=cutoff]
    if not inwin: ws["backfill_done"]=True; continue
    done=process(kol,addr,inwin); stats[kol]["backfill"]=len(done)
    if done: ws["oldest"]=done[-1]["signature"]
    if len(done)==len(inwin) and len(inwin)<len(res): ws["backfill_done"]=True
st["buys"]=[b for b in st["buys"] if b["ts"]>=cutoff]
json.dump(st,open(sp,"w"))
buys={}
for b in st["buys"]:
    d=buys.setdefault(b["mint"],{})
    if b["kol"] not in d or b["ts"]<d[b["kol"]]: d[b["kol"]]=b["ts"]
ap=f"{BASE}/data/kol_alerted.json"
alerted=json.load(open(ap)) if os.path.exists(ap) else []
hits=[{"mint":m,"kols":k} for m,k in buys.items() if len(k)>=3 and m not in alerted]
for k,ws in st["wallets"].items(): stats[k]["backfill_done"]=ws["backfill_done"]
print(json.dumps({"elapsed":round(time.time()-T0),"stats":stats,"hits":hits,"multi2":{m:k for m,k in buys.items() if len(k)>=2}},indent=1))
if "--mark" in sys.argv:
    json.dump(alerted+[h["mint"] for h in hits],open(ap,"w"),indent=1)
