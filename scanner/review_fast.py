import json,os,time,urllib.request
D="/workspace/memecoin-scanner"; H=30
def get(u,tries=5):
    for a in range(tries):
        try: return json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}),timeout=25))
        except Exception as e:
            if "429" not in str(e) or a==tries-1: raise
            time.sleep(10*(a+1))
now=time.time(); rows=[json.loads(l) for l in open(os.path.join(D,"alerts_log.jsonl")) if l.strip()]
rows=[r for r in rows if now-r["t"]<=H*3600]
rows.sort(key=lambda r: not r["mint"].endswith("pump"))
prev={(x["mint"],x["t"]):x for x in (json.load(open("/workspace/memecoin-scanner/review_last.json")) if os.path.exists("/workspace/memecoin-scanner/review_last.json") else []) if "err" not in x}
mints=sorted({r["mint"] for r in rows}); best={}
k=lambda x:((x.get("liquidity") or {}).get("usd",0) or 0, x.get("volume",{}).get("h24",0) or 0)
for i in range(0,len(mints),30):
    for p in get("https://api.dexscreener.com/tokens/v1/solana/"+",".join(mints[i:i+30])):
        m=p["baseToken"]["address"]
        if m not in best or k(p)>k(best[m]): best[m]=p
candles={}
def ohlc(m,p):
    if m in candles: return candles[m]
    c=None; src=None
    if m.endswith("pump"):
        try:
            j=get(f"https://swap-api.pump.fun/v2/coins/{m}/candles?interval=5m&limit=1000&currency=USD&createdTs={p.get('pairCreatedAt') or int((now-200000)*1000)}",3)
            c=[(x["timestamp"]/1000,float(x["high"]),float(x["low"])) for x in j]; src="pump"
        except Exception: c=None
    if not c:
        o=get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{p['pairAddress']}/ohlcv/minute?aggregate=5&limit=1000")["data"]["attributes"]["ohlcv_list"]
        c=[(x[0],x[2],x[3]) for x in o]; src="gecko"; time.sleep(3)
    candles[m]=(c,src); return candles[m]
out=[]
for r in rows:
    m=r["mint"]
    if (m,r["t"]) in prev: out.append(prev[(m,r["t"])]); continue
    try:
        p=best[m]; mc_now=p.get("marketCap") or p.get("fdv") or 0; price_now=float(p["priceUsd"])
        c,src=ohlc(m,p); after=[x for x in c if x[0]>=r["t"]-300]
        scale=mc_now/price_now
        peak=max(max(x[1] for x in after)*scale,mc_now) if after else mc_now
        low=min(min(x[2] for x in after)*scale,mc_now) if after else mc_now
        out.append({**r,"mc_now":round(mc_now),"peak_mc":round(peak),"peak_x":round(peak/r["mc"],2),"now_x":round(mc_now/r["mc"],2),"low_x":round(low/r["mc"],2),"src":src,"dexId":p.get("dexId"),"liq_now":(p.get("liquidity") or {}).get("usd",0),"age_h_at_alert":round((r["t"]*1000-(p.get("pairCreatedAt") or 0))/3.6e6,1),"dexscreener":p["url"]})
    except Exception as e: out.append({**r,"err":str(e)[:100]})
    json.dump(out,open("/workspace/memecoin-scanner/review_last.json","w"),indent=1)
print(len(out),sum("err" in x for x in out))
