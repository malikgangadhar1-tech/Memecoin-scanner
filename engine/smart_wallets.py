"""Direct smart-wallet tracker (Hark, Oct 7 2026).
Polls judged wallets (judge_scores >= MIN_SCORE) straight from Helius RPC, so
coverage no longer depends on watch.py happening to record the pool.
Table sw_trades(wallet, mint, kind, ts, sol, tokens, sig). 
Usage: python3 smart_wallets.py [--backfill HOURS] [--budget N]
Prints SW_BUY_CONVERGENCE lines (>=2 good wallets same mint within 60 min)."""
import json,sqlite3,sys,time,urllib.request,os
DB=os.path.join(os.path.dirname(__file__),'watch.db')
RPC = "https://mainnet.helius-rpc.com/" + (("?api-key=" + __import__("os").environ["HELIUS_API_KEY"]) if __import__("os").environ.get("HELIUS_API_KEY") else "")  # Hark injects the key on the wire; GitHub Actions passes HELIUS_API_KEY
MIN_SCORE=float(os.environ.get('SW_MIN_SCORE',65))
SKIP={'So11111111111111111111111111111111111111112','EPjFWdd5AufqSSqeM2qrxa9LmUjnTq2FDV1kDNjAiuJ1v','Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB'}
args=sys.argv[1:]
BACKFILL=float(args[args.index('--backfill')+1]) if '--backfill' in args else 0
BUDGET=int(args[args.index('--budget')+1]) if '--budget' in args else int(os.environ.get('SW_BUDGET',180))
PER_WALLET=int(os.environ.get('SW_PER_WALLET',8))
cred=[0]
def rpc(method,params):
    body=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    for t in range(4):
        try:
            cred[0]+=1
            r=json.load(urllib.request.urlopen(urllib.request.Request(RPC,body,{"Content-Type":"application/json"}),timeout=30))
            if 'error' in r:
                if r['error'].get('code') in (-32429,429): time.sleep(2*(t+1)); continue
                return None
            return r['result']
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(2*(t+1)); continue
            return None
        except Exception: time.sleep(1)
con=sqlite3.connect(DB)
con.execute("create table if not exists sw_trades(wallet,mint,kind,ts INTEGER,sol REAL,tokens REAL,sig TEXT PRIMARY KEY)")
con.execute("create table if not exists sw_alerts(mint TEXT PRIMARY KEY, ts INTEGER, wallets TEXT)")
wallets=con.execute("select wallet,score from judge_scores where score>=? order by score desc",(MIN_SCORE,)).fetchall()
import random
# Oct 7 audit: blocked ring wallets (sw_blocklist.json, kept by sw_ring_update.py) are neither polled nor counted.
try: _BL=set(json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'sw_blocklist.json'))))
except Exception: _BL=set()
wallets=[x for x in wallets if x[0] not in _BL]
if not BACKFILL: random.shuffle(wallets)
try:
    _blk={l.split()[0] for l in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'sw_blocklist.txt')) if l.strip() and not l.startswith('#')}
except Exception: _blk=set()
wallets=[x for x in wallets if x[0] not in _blk]
score=dict(wallets)
# Extra wallets from Muse's handover (Oct 7): R = Muse's named runner catchers, I = top independent traders from his Gemini forensics.
# Judged wallets are polled first; extras rotate (EXTRA_SLICE per run) inside the same credit budget.
tier={w:'J' for w,_ in wallets}
try:
    import csv as _csv
    _ex=[r for r in _csv.DictReader(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'extra_wallets.csv'))) if r['wallet'] not in tier]
except Exception: _ex=[]
for r in _ex: tier[r['wallet']]=r['tier']; score.setdefault(r['wallet'],0)
if _ex and not BACKFILL:
    EXTRA_SLICE=int(os.environ.get('SW_EXTRA_SLICE',15))
    _R=[(r['wallet'],0) for r in _ex if r['tier']=='R']; _I=[(r['wallet'],0) for r in _ex if r['tier']!='R']
    _k=(int(time.time())//900)%max(1,(len(_I)+EXTRA_SLICE-1)//EXTRA_SLICE)
    wallets=wallets+_R+_I[_k*EXTRA_SLICE:(_k+1)*EXTRA_SLICE]
def parse(tx,w):
    m=tx.get('meta') or {}
    if m.get('err'): return []
    keys=[k['pubkey'] if isinstance(k,dict) else k for k in tx['transaction']['message']['accountKeys']]
    if w not in keys: return []
    i=keys.index(w); dsol=(m['postBalances'][i]-m['preBalances'][i])/1e9
    pre={};post={}
    for b in m.get('preTokenBalances') or []:
        if b.get('owner')==w: pre[b['mint']]=float(b['uiTokenAmount']['uiAmount'] or 0)
    for b in m.get('postTokenBalances') or []:
        if b.get('owner')==w: post[b['mint']]=float(b['uiTokenAmount']['uiAmount'] or 0)
    # wSOL counted as SOL
    wsol='So11111111111111111111111111111111111111112'
    dsol+=post.get(wsol,0)-pre.get(wsol,0)
    out=[]
    for mint in set(pre)|set(post):
        if mint in SKIP: continue
        d=post.get(mint,0)-pre.get(mint,0)
        if abs(d)<=0: continue
        out.append((mint,'buy' if d>0 else 'sell',abs(dsol),abs(d)))
    return out
now=int(time.time())
for w,s in wallets:
    if cred[0]>=BUDGET: break
    last=con.execute("select max(ts) from sw_trades where wallet=?",(w,)).fetchone()[0]
    cutoff=now-int(BACKFILL*3600) if BACKFILL else (last or now-3600)
    before=None; sigs=[]
    while True:
        p={"limit":1000}
        if before: p["before"]=before
        r=rpc("getSignaturesForAddress",[w,p])
        if not r: break
        new=[x for x in r if (x.get('blockTime') or 0)>cutoff and not x.get('err')]
        sigs+=new
        if len(new)<len(r) or len(r)<1000 or not BACKFILL: break
        before=r[-1]['signature']
    known={x[0] for x in con.execute("select sig from sw_trades where wallet=?",(w,))}
    sigs=[x for x in sigs if x['signature'] not in known]
    if not BACKFILL: sigs=sigs[:PER_WALLET]   # live: newest first; bots can't eat the budget
    for x in sigs:
        if cred[0]>=BUDGET: break
        tx=rpc("getTransaction",[x['signature'],{"encoding":"jsonParsed","maxSupportedTransactionVersion":1}])
        if not tx: continue
        for j,(mint,kind,sol,tok) in enumerate(parse(tx,w)):
            con.execute("insert or ignore into sw_trades values(?,?,?,?,?,?,?)",(w,mint,kind,tx.get('blockTime') or x['blockTime'],sol,tok,x['signature']+(f':{j}' if j else '')))
    con.commit()
# convergence: >=2 distinct good wallets buying same mint within 60 min (last 2h only when live)
if BUDGET<=0: sys.exit(0) if __name__=='__main__' else None
win_start=now-(int(BACKFILL*3600) if BACKFILL else 7200)
rows=[] if BUDGET<=0 else con.execute("select mint,wallet,ts,sol from sw_trades where kind='buy' and ts>=? and sol>=0.05 order by ts",(win_start,)).fetchall()
rows=[r for r in rows if r[1] not in _BL]

# Rohit Oct 7 21:44 IST: a wallet that deployed or funded the coin it buys counts as 0, whatever its judge score.
con.execute("create table if not exists sw_insiders(mint TEXT PRIMARY KEY, creator TEXT, insiders TEXT)")
def insiders(mint):
    r=con.execute("select insiders from sw_insiders where mint=?",(mint,)).fetchone()
    if r: return set(json.loads(r[0]))
    before=None; oldest=None
    for _ in range(8):
        p={"limit":1000}
        if before: p["before"]=before
        sg=rpc("getSignaturesForAddress",[mint,p]) or []
        if not sg: break
        oldest=sg[-1]["signature"]; before=oldest
        if len(sg)<1000: break
    if not oldest: return set()
    tx=rpc("getTransaction",[oldest,{"encoding":"json","maxSupportedTransactionVersion":0}])
    if not tx: return set()
    creator=tx["transaction"]["message"]["accountKeys"][0]; ins={creator}
    # funders: wallets that sent SOL to the creator in its earliest transactions
    cs=rpc("getSignaturesForAddress",[creator,{"limit":1000}]) or []
    for x in cs[-4:]:
        t=rpc("getTransaction",[x["signature"],{"encoding":"json","maxSupportedTransactionVersion":0}])
        if not t or not t.get("meta"): continue
        ak=t["transaction"]["message"]["accountKeys"]; pre=t["meta"]["preBalances"]; post=t["meta"]["postBalances"]
        if creator in ak and post[ak.index(creator)]>pre[ak.index(creator)]:
            for i,a in enumerate(ak):
                if a!=creator and pre[i]-post[i]>10_000_000: ins.add(a)  # sent >0.01 SOL
    con.execute("insert or replace into sw_insiders values(?,?,?)",(mint,creator,json.dumps(sorted(ins))))
    return ins
by={}
for mint,w,ts,sol in rows:
    if w in _blk: continue
    by.setdefault(mint,[]).append((ts,w,sol))
for mint,L in by.items():
    for i,(ts,w,sol) in enumerate(L):
        ws={}
        for t2,w2,s2 in L:
            if ts-3600<=t2<=ts: ws.setdefault(w2,(t2,s2))
        if len(ws)>=2:
            if con.execute("select 1 from sw_alerts where mint=?",(mint,)).fetchone(): break
            if not BACKFILL:
                ins=insiders(mint); bad=[k for k in ws if k in ins]
                if bad:
                    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'logs','sw_insider_blocks.jsonl'),'a') as fh:
                        fh.write(json.dumps({"t":now,"mint":mint,"insider_wallets":bad,"wallets":list(ws)})+"\n")
                    ws={k:v for k,v in ws.items() if k not in ins}
                    if len(ws)<2: continue
            con.execute("insert into sw_alerts values(?,?,?)",(mint,ts,json.dumps(ws)))
            times=sorted(v[0] for v in ws.values())
            tag='TIGHT' if times[-1]-times[0]<=2 else ('LOOSE' if times[-1]-times[0]<=60 else 'NONE')
            if not BACKFILL:
                mc=liq=0;sym=mint[:6]
                try:
                    ps=json.load(urllib.request.urlopen(urllib.request.Request("https://api.dexscreener.com/tokens/v1/solana/"+mint,headers={"User-Agent":"Mozilla/5.0"}),timeout=15))
                    if ps:
                        p=max(ps,key=lambda x:x.get('marketCap') or 0); mc=p.get('marketCap') or 0; liq=(p.get('liquidity') or {}).get('usd',0); sym=p['baseToken']['symbol']
                except Exception: pass
                ist=time.strftime('%H:%M',time.gmtime(ts+19800))
                farm=' | RUGFARM-PAIR' if {'DPn2K5e8ZrVhvrUPYHPLaX5Dd78LF9cY4G5qh1QmYFYF','57wLkQBctPAfLeuoA119Pq77KdGxViqGqYAbbJA2TURf'}<=set(ws) else ''
                print(f"SW_BUY_CONVERGENCE {sym}{farm} | {len(ws)} judged wallets | scores={','.join((str(int(score[k])) if tier.get(k)=='J' else tier.get(k,'?')) for k in ws)} | tier={'JUDGED' if sum(1 for k in ws if tier.get(k)=='J')>=2 else 'MIXED'} | sol={','.join(f'{v[1]:.2f}' for v in ws.values())} | last buy {ist} IST | mcap=${mc:,.0f} liq=${liq:,.0f} | coord={tag} | https://dexscreener.com/solana/{mint}",flush=True)
            break
con.commit()
print(f"# credits used {cred[0]}, wallets {len(wallets)}",file=sys.stderr)
