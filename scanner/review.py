#!/usr/bin/env python3
"""Grade alerts from the last N hours (default 30): peak MC multiple after alert, current multiple, min drawdown."""
import json,os,sys,time,urllib.request
D=os.path.dirname(os.path.abspath(__file__)); H=float(sys.argv[1]) if len(sys.argv)>1 else 30
def get(u):
    for a in range(5):
        try: return json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}),timeout=25))
        except Exception as e:
            if "429" not in str(e) or a==4: raise
            time.sleep(10*(a+1))
now=time.time(); rows=[json.loads(l) for l in open(os.path.join(D,"alerts_log.jsonl")) if l.strip()]
rows=[r for r in rows if now-r["t"]<=H*3600]
out=[]
for r in rows:
    try:
        ps=get("https://api.dexscreener.com/tokens/v1/solana/"+r["mint"])
        p=max(ps,key=lambda x:(x.get("liquidity") or {}).get("usd",0) or x.get("volume",{}).get("h24",0))
        mc_now=p.get("marketCap") or p.get("fdv") or 0; price_now=float(p["priceUsd"])
        o=get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{p['pairAddress']}/ohlcv/minute?aggregate=5&limit=1000")["data"]["attributes"]["ohlcv_list"]
        time.sleep(2.1)
        after=[c for c in o if c[0]>=r["t"]-300]
        scale=mc_now/price_now if price_now else 0
        peak=max(c[2] for c in after)*scale if after else mc_now
        low=min(c[3] for c in after)*scale if after else mc_now
        out.append({**r,"mc_now":round(mc_now),"peak_mc":round(peak),"peak_x":round(peak/r["mc"],2),"now_x":round(mc_now/r["mc"],2),"low_x":round(low/r["mc"],2),"dexscreener":p["url"]})
    except Exception as e: out.append({**r,"err":str(e)[:100]})
print(json.dumps(out,indent=1))
