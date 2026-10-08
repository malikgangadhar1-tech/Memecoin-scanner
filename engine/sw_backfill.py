"""Backfill sw_trades for judged wallets (interleaved, threaded, short DB locks)."""
import sqlite3,json,time,sys,os
from concurrent.futures import ThreadPoolExecutor
sys.argv=['x','--budget','0']; import smart_wallets as S
HOURS=float(os.environ.get('HRS',24)); CAP=int(os.environ.get('CAP',200)); TH=int(os.environ.get('TH',16))
now=time.time(); per={}
con=sqlite3.connect(S.DB,timeout=120)
known={x[0].split(':')[0] for x in con.execute("select sig from sw_trades")}
con.close()
for w,s in S.wallets:
    r=S.rpc("getSignaturesForAddress",[w,{"limit":1000}]) or []
    ts=[x['blockTime'] for x in r if x.get('blockTime')]
    if len(r)==1000 and ts and now-min(ts)<6*3600: print('skip hyperactive',w[:6],flush=True); continue
    per[w]=[x for x in r if (x.get('blockTime') or 0)>now-HOURS*3600 and not x.get('err') and x['signature'] not in known][:CAP]
jobs=[]
while any(per.values()):
    for w in list(per):
        if per[w]: jobs.append((w,per[w].pop(0)))
print('jobs',len(jobs),flush=True)
def work(j):
    w,x=j
    tx=S.rpc("getTransaction",[x['signature'],{"encoding":"jsonParsed","maxSupportedTransactionVersion":1}])
    return w,x,(S.parse(tx,w) if tx else [])
buf=[];done=0;t0=time.time()
def flush():
    global buf
    c=sqlite3.connect(S.DB,timeout=120); c.executemany("insert or ignore into sw_trades values(?,?,?,?,?,?,?)",buf); c.commit(); c.close(); buf=[]
with ThreadPoolExecutor(TH) as ex:
    for w,x,res in ex.map(work,jobs):
        for j,(mint,kind,sol,tok) in enumerate(res):
            buf.append((w,mint,kind,x['blockTime'],sol,tok,x['signature']+(f':{j}' if j else '')))
        done+=1
        if done%300==0: flush(); print(done,round(time.time()-t0),flush=True)
flush(); print('done',done,'credits',S.cred[0],flush=True)
