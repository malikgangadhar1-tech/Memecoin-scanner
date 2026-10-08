"""Auto-block smart wallets that keep converging on coins that die (serial fake-launch rings).
Rule (Oct 7 2026 audit): a wallet in >=4 SW convergences older than 6h, >=80% of which are now under $5k mcap
on DexScreener, and none of which is above 2x its alert-time mcap, goes into sw_blocklist.json.
Blocked wallets are skipped by smart_wallets.py (not polled, not counted). Prints changes."""
import json,sqlite3,urllib.request,time,os,collections
D=os.path.dirname(os.path.abspath(__file__)); BL=os.path.join(D,'sw_blocklist.json')
con=sqlite3.connect(os.path.join(D,'watch.db'),timeout=60)
bl=json.load(open(BL)) if os.path.exists(BL) else {}
now=time.time()
A=con.execute("select mint,ts,wallets from sw_alerts where ts<?",(now-6*3600,)).fetchall()
ds={}
ms=[a[0] for a in A]
for i in range(0,len(ms),30):
    try:
        for p in json.load(urllib.request.urlopen(urllib.request.Request("https://api.dexscreener.com/tokens/v1/solana/"+",".join(ms[i:i+30]),headers={"User-Agent":"Mozilla/5.0"}),timeout=20)):
            m=p['baseToken']['address']; ds[m]=max(ds.get(m,0),p.get('marketCap') or 0)
    except Exception: pass
    time.sleep(1)
st=collections.defaultdict(lambda:[0,0,0])
for m,ts,w in A:
    if m not in ds: continue
    try: ws=list(json.loads(w)); amc=None
    except Exception: ws=[r[0] for r in con.execute("select distinct wallet from sw_trades where mint=? and kind='buy' and ts between ?-3600 and ?",(m,ts,ts))]
    for x in ws:
        st[x][0]+=1; st[x][1]+=ds[m]<5000
new=[]
for x,(n,dead,_) in st.items():
    if n>=4 and dead/n>=0.8 and x not in bl:
        bl[x]=f"auto {time.strftime('%Y-%m-%d')}: {dead}/{n} SW convergences dead (<$5k)"; new.append(x)
json.dump(bl,open(BL,'w'),indent=1)
print(json.dumps({"blocked_total":len(bl),"new":new}))
