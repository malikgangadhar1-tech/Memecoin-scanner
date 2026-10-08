import json, urllib.request, urllib.parse, time, sys, os

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, 'forensics_candidates.json')) as f:
    data = json.load(f)
cands = data['candidates'] if isinstance(data, dict) else data
wallets = [c['wallet'] if isinstance(c, dict) else c for c in cands]
print(f"Total candidates: {len(wallets)}", flush=True)

OUT = os.path.join(BASE, 'kol_xref_hits.json')
CHECKED = os.path.join(BASE, 'kol_xref_checked.json')
hits = []
# Resume: skip wallets already checked (hits AND non-hits).
# The old code only remembered hit wallets, so every restart rechecked
# thousands of non-hits. Now we persist the full checked set. (2026-10-05)
done = set()
if os.path.exists(OUT):
    try:
        with open(OUT) as f:
            for h in json.load(f):
                done.add(h['wallet'])
                hits.append(h)
        print(f"Resuming, {len(hits)} hits loaded", flush=True)
    except: pass
if os.path.exists(CHECKED):
    try:
        with open(CHECKED) as f:
            done.update(json.load(f))
        print(f"Resuming, {len(done)} total already checked", flush=True)
    except: pass

for i, w in enumerate(wallets):
    if w in done:
        continue
    try:
        url = f"https://dethective.com/kollector/api/search?q={urllib.parse.quote(w)}&src=typed"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        accts = d.get('accounts', [])
        if accts:
            for a in accts:
                hits.append({
                    'wallet': w,
                    'kol_name': d.get('name'),
                    'platform': a.get('platform'),
                    'handle': a.get('handle'),
                    'x_handle': a.get('x_handle'),
                    'followers': a.get('followers'),
                    'ethos_score': (a.get('ethos') or {}).get('score'),
                })
            print(f"[{i+1}/{len(wallets)}] HIT: {w[:12]}... = {d.get('name')}", flush=True)
        done.add(w)
    except Exception as e:
        err = str(e)[:60]
        print(f"[{i+1}/{len(wallets)}] err {err}", flush=True)
        # Connection dropped = API throttling us. Back off hard before the
        # next request so we don't burn through the wallet list erroring.
        if "closed connection" in err or "timed out" in err.lower():
            time.sleep(15)
    # checkpoint every 100
    if (i+1) % 100 == 0:
        with open(OUT, 'w') as f:
            json.dump(hits, f, indent=1)
        with open(CHECKED, 'w') as f:
            json.dump(sorted(done), f)
        print(f"  checkpoint: {len(hits)} hits, {len(done)} checked so far", flush=True)
    time.sleep(2.0)  # polite: ~30 req/min, not 200 (2026-10-05: 0.3s got us blocked)

with open(OUT, 'w') as f:
    json.dump(hits, f, indent=1)
with open(CHECKED, 'w') as f:
    json.dump(sorted(done), f)
print(f"DONE: {len(hits)} KOL matches out of {len(wallets)} candidates", flush=True)
