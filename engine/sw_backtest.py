"""Backtest smart-wallet signals from sw_trades against pump.fun 1-min candles.
Signals: CONV2 (>=2 distinct judged wallets buy same mint within 60 min),
CONV3 (>=3), SOLO (first buy by any judged wallet). Entry = candle high DELAY
min after trigger; outcome = peak after entry / entry, TP2/SL0.5 sim, 12% fees."""
import sqlite3,json,time,urllib.request,sys,os
DB=os.path.join(os.path.dirname(__file__),'watch.db')
CACHE=os.path.join(os.path.dirname(__file__),'..','research','sw_candles.json')
DELAYS=[1,5,10]
con=sqlite3.connect(DB,timeout=120)
score=dict(con.execute("select wallet,score from judge_scores"))
rows=con.execute("select mint,wallet,ts,sol from sw_trades where kind='buy' and sol>=0.05 order by ts").fetchall()
by={}
for m,w,ts,sol in rows: by.setdefault(m,[]).append((ts,w,sol))
sig=[]
for m,L in by.items():
    first=L[0][0]; sig.append(('SOLO',m,first,1))
    done=set()
    for ts,w,sol in L:
        ws={w2 for t2,w2,s2 in L if ts-3600<=t2<=ts}
        for k in (2,3):
            if len(ws)>=k and k not in done: done.add(k); sig.append((f'CONV{k}',m,ts,len(ws)))
cache=json.load(open(CACHE)) if os.path.exists(CACHE) else {}
def candles(m):
    if m in cache: return cache[m]
    out=[]; ts=1791000000000
    for _ in range(5):
        try:
            r=json.load(urllib.request.urlopen(urllib.request.Request(f"https://swap-api.pump.fun/v2/coins/{m}/candles?interval=1m&limit=1000&currency=USD&createdTs={ts}",headers={"User-Agent":"Mozilla/5.0"}),timeout=20))
        except Exception: break
        out+=[(c['timestamp']//1000,float(c['high'])*1e9,float(c['low'])*1e9,float(c['close'])*1e9) for c in r if c.get('high') is not None and c.get('low') is not None and c.get('close') is not None and c.get('timestamp') is not None]
        if len(r)<1000: break
        ts=r[-1]['timestamp']+60000
    cache[m]=out; return out
from concurrent.futures import ThreadPoolExecutor
need=[m for m in {x[1] for x in sig} if m not in cache]
with ThreadPoolExecutor(12) as ex: list(ex.map(candles,need))
json.dump(cache,open(CACHE,'w'))
res=[]
for kind,m,ts,n in sig:
    C=candles(m)
    if not C: continue
    for d in DELAYS:
        after=[c for c in C if c[0]>=ts+d*60]
        if len(after)<2: continue
        e=after[0][1]
        if e<=0: continue
        pk=max(c[1] for c in after[1:]); net=None
        for c in after[1:]:
            if c[2]<=e*0.5: net=0.5;break
            if c[1]>=e*2: net=2;break
        if net is None: net=after[-1][3]/e
        res.append(dict(kind=kind,m=m,ts=ts,d=d,mc=e,px=pk/e,net=net*0.88,peakmc=pk))
json.dump(cache,open(CACHE,'w'))
json.dump(res,open(os.path.join(os.path.dirname(CACHE),'sw_backtest.json'),'w'))
for kind in ('SOLO','CONV2','CONV3'):
    for d in DELAYS:
        R=[r for r in res if r['kind']==kind and r['d']==d]
        if not R: continue
        n=len(R)
        print(f"{kind} delay{d}m n={n} 2x={sum(r['px']>=2 for r in R)} 5x={sum(r['px']>=5 for r in R)} 10x={sum(r['px']>=10 for r in R)} peak>=1M={sum(r['peakmc']>=1e6 for r in R)} avgnet={sum(r['net'] for r in R)/n:.2f} medMC={sorted(r['mc'] for r in R)[n//2]:.0f}")
