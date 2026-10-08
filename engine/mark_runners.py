"""Hark: flag runner pools in watch.db from DexScreener (free, batched), because
watch.py only live-watches 25 pools and misses most runners. runner = token mcap
>= $200k with >= $100k 24h volume (volume guard kills spoofed mcap prints)."""
import sqlite3,json,time,urllib.request
con=sqlite3.connect('watch.db',timeout=120)
rows=con.execute("select pool,base_mint from pools where runner=0 and first_seen is not null").fetchall()
mp={}
for p,m in rows:
    if m: mp.setdefault(m.replace('solana_',''),[]).append(p)
ms=list(mp); n=0
for i in range(0,len(ms),30):
    try: r=json.load(urllib.request.urlopen(urllib.request.Request("https://api.dexscreener.com/tokens/v1/solana/"+",".join(ms[i:i+30]),headers={"User-Agent":"Mozilla/5.0"}),timeout=20))
    except Exception: continue
    best={}
    for x in r:
        a=x['baseToken']['address']; mc=x.get('marketCap') or 0; v=x.get('volume',{}).get('h24',0)
        best[a]=max(best.get(a,(0,0)),(mc,v))
    for a,(mc,v) in best.items():
        if mc>=200000 and v>=100000:
            for p in mp.get(a,[]):
                con.execute("update pools set runner=1, runner_at=coalesce(runner_at,?) where pool=?",(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),p)); n+=1
    time.sleep(0.25)
con.commit(); print('runners flagged',n)
