#!/usr/bin/env python3
"""Solana memecoin scanner. Prints JSON list of NEW matches (not alerted before)."""
import json,os,time,urllib.request
D=os.path.dirname(os.path.abspath(__file__)); SEEN=os.path.join(D,"seen.json")
RPC="https://api.mainnet-beta.solana.com"
def get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=20))
def rpc(m,p):
    r=urllib.request.Request(RPC,json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),{"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=20))
def dev_check(mint,supply):
    before=None;oldest=None
    for _ in range(15):
        p={"limit":1000}
        if before:p["before"]=before
        s=rpc("getSignaturesForAddress",[mint,p]).get("result",[])
        if not s:break
        oldest=s[-1]["signature"];before=oldest
        if len(s)<1000:break
        time.sleep(0.3)
    tx=rpc("getTransaction",[oldest,{"encoding":"json","maxSupportedTransactionVersion":0}])["result"]
    creator=tx["transaction"]["message"]["accountKeys"][0]
    v=rpc("getTokenAccountsByOwner",[creator,{"mint":mint},{"encoding":"jsonParsed"}])["result"]["value"]
    bal=sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in v)
    return creator,bal
seen=set(json.load(open(SEEN))) if os.path.exists(SEEN) else set()
profiles=[x for x in get("https://api.dexscreener.com/token-profiles/latest/v1") if x.get("chainId")=="solana"]
out=[];checked=[]
for pr in profiles:
    mint=pr["tokenAddress"]
    if mint in seen: continue
    links=pr.get("links",[])
    socials=[l for l in links if (l.get("type") in ("twitter","telegram")) or "x.com" in l.get("url","") or "twitter.com" in l.get("url","") or "t.me" in l.get("url","")]
    website=[l for l in links if (l.get("label","").lower()=="website") or (not l.get("type") and l not in socials)]
    if not socials or not website: continue
    try:
        orders=get(f"https://api.dexscreener.com/orders/v1/solana/{mint}")
        o=orders.get("orders",orders) if isinstance(orders,dict) else orders
        if not any(x.get("type")=="tokenProfile" and x.get("status")=="approved" for x in o): continue
        pairs=get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
        if not pairs: continue
        p=max(pairs,key=lambda x:(x.get("liquidity") or {}).get("usd",0) or x.get("volume",{}).get("h24",0))
        mc=p.get("marketCap") or p.get("fdv") or 0
        if not mc or mc>=50000: continue
        t=p["txns"]["h1"]; buys,sells=t["buys"],t["sells"]
        vol=p["volume"]["h1"]
        liq=(p.get("liquidity") or {}).get("usd",0) or 0
        bonding = p.get("dexId")=="pumpfun" or liq==0
        if bonding or liq<10000 or liq<mc: continue  # Oct 7: user rule, real LP >= MC required; bonding-curve coins skipped
        if p["volume"].get("h24",0)<10000: continue
        if t["buys"] and p["volume"]["h1"]/max(t["buys"]+t["sells"],1)<20: continue
        organic = buys>=50 and buys>=1.2*max(sells,1) and vol>0 and (vol/max(buys+sells,1))<500 and vol<5*mc
        if not organic: continue
        supply=mc/float(p["priceUsd"]) if p.get("priceUsd") else None
        creator,bal=dev_check(mint,supply)
        pct=(bal/supply*100) if supply else None
        if pct is None or pct>=1: continue
        out.append({"name":p["baseToken"]["name"],"symbol":p["baseToken"]["symbol"],"mint":mint,"mc":round(mc),"buys_1h":buys,"sells_1h":sells,"vol_1h":round(vol),"dev":creator,"dev_pct":round(pct,3),"socials":[l.get("url") for l in pr.get("links",[])],"dexscreener":p["url"],"liq":round(liq),"tags":[x for x in (["PRIORITY <$10k MC"] if mc<10000 else [])+(["bonding curve, no LP yet"] if bonding else [])],"age_min":round((time.time()*1000-p.get("pairCreatedAt",0))/60000)})
        seen.add(mint)
    except Exception as e:
        checked.append({"mint":mint,"err":str(e)[:120]})

_lg=open(os.path.join(D,"alerts_log.jsonl"),"a")
for _a in out:
    _lg.write(json.dumps({"t":time.time(),"scanner":"dexpaid","signal":_a.get("signal","dex paid"),"mint":_a["mint"],"symbol":_a["symbol"],"mc":_a["mc"]})+"\n")
_lg.close()
json.dump(sorted(seen),open(SEEN,"w"))
print(json.dumps({"matches":out,"profiles_scanned":len(profiles),"errors":checked},indent=1))
