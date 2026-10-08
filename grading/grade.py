"""Unified grader for every memecoin alert (pool filter, dex paid, revival, KOL convergence).
Outcome per alert: peak_x, low_x, now_x, min_to_peak, and a $2.5 trade sim:
entry = high of first 1-min/5-min candle starting >= alert+60s (pessimistic fill), 12% round-trip fees.
sim_tp2: exit at 2x entry if hit before 0.5x stop, else at stop or at last price. net_x after fees.
"""
import json, os, time, csv, urllib.request
P = "/workspace/projects/7ed675e0-9a24-4f62-9d59-e27bf0c6ccd0"
OUT = f"{P}/grading/outcomes.jsonl"
FEE = 0.12
def get(u, tries=4):
    for a in range(tries):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}), timeout=25))
        except Exception as e:
            if a == tries - 1: raise
            time.sleep(15 * (a + 1) if "429" in str(e) else 3)
def ist_ts(s):
    import re, datetime
    s = s.strip()
    for f in ("%Y-%m-%d %H:%M IST",):
        try: return datetime.datetime.strptime(s, f).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).timestamp()
        except Exception: pass
    m = re.search(r"(\d{4}-\d\d-\d\d) (\d{1,2}):(\d\d) (AM|PM)", s)
    if m:
        h = int(m[2]) % 12 + (12 if m[4] == "PM" else 0)
        return datetime.datetime.strptime(f"{m[1]} {h}:{m[3]}", "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).timestamp()
    m = re.search(r"(\d{4}-\d\d-\d\d)T(\d\d):(\d\d)", s)
    return datetime.datetime.strptime(f"{m[1]} {m[2]}:{m[3]}", "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).timestamp()

alerts = []
for l in open(f"{P}/scanner/alerts_log.jsonl"):
    if l.strip():
        r = json.loads(l); alerts.append({"scanner": r["scanner"], "signal": r["signal"], "t": r["t"], "mint": r["mint"], "sym": r.get("symbol"), "mc": r.get("mc")})
for r in csv.DictReader(open(f"{P}/data/alert_outcomes.csv")):
    _t = ist_ts(r["alerted_at"])
    alerts.append({"scanner": "newpool", "signal": "pool filter v3" if _t >= 1791387720 else ("pool filter v2" if _t >= 1791356100 else "pool filter v1"), "t": _t, "pool": r["pool"], "sym": r["ticker"], "mc": float(r["alert_mcap"] or 0),
                   "liq": float(r["alert_liq"] or 0), "buyers": int(r["alert_buyers"] or 0), "buys": int(r["alert_buys"] or 0), "sells": int(r["alert_sells"] or 0)})
for m, t, sym in json.load(open(f"{P}/grading/kol_alerts.json")):
    alerts.append({"scanner": "kol", "signal": "3 KOL convergence", "t": t, "mint": m, "sym": sym, "mc": None})

# pump.fun band watcher alerts (live in dispatcher since Oct 7 23:37 IST)
if os.path.exists(f"{P}/data/pump_band_alerts.jsonl"):
    for l in open(f"{P}/data/pump_band_alerts.jsonl"):
        if l.strip():
            r = json.loads(l); alerts.append({"scanner": "band", "signal": "pump.fun band", "t": r["t"], "mint": r["mint"], "sym": r.get("symbol"), "mc": r.get("mc")})

# smart-wallet convergence alerts (live since Oct 7 13:00 IST; earlier rows are backfill)
try:
    import sqlite3
    _c = sqlite3.connect(f"{P}/engine/watch.db", timeout=30)
    for m, t in _c.execute("select mint, ts from sw_alerts where ts >= 1791358200"):
        alerts.append({"scanner": "sw", "signal": "smart-wallet convergence", "t": t, "mint": m, "sym": None, "mc": None})
except Exception as e: print("sw read failed", e)

# resolve pairs via DexScreener
pools = [a["pool"] for a in alerts if a.get("pool")]
for i in range(0, len(pools), 30):
    d = get("https://api.dexscreener.com/latest/dex/pairs/solana/" + ",".join(pools[i:i + 30]))
    for p in d.get("pairs") or []:
        for a in alerts:
            if a.get("pool") == p["pairAddress"]: a["pair"] = p
mints = sorted({a["mint"] for a in alerts if a.get("mint")})
best = {}
for i in range(0, len(mints), 30):
    for p in get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30])):
        m = p["baseToken"]["address"]; k = lambda x: ((x.get("liquidity") or {}).get("usd", 0) or 0)
        if m not in best or k(p) > k(best[m]): best[m] = p
for a in alerts:
    if a.get("mint") and a["mint"] in best: a["pair"] = best[a["mint"]]

cache = {}
def candles(p):
    m = p["baseToken"]["address"]
    if m in cache: return cache[m]
    c = None
    if True:  # pump.fun swap-api serves pumpswap/other mints too (Oct 7 22:50); GT only as fallback (429s)
        try:
            j = get(f"https://swap-api.pump.fun/v2/coins/{m}/candles?interval=1m&limit=1000&currency=USD&createdTs={p.get('pairCreatedAt') or int((time.time()-200000)*1000)}", 2)
            c = [(x["timestamp"] / 1000, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"])) for x in j]; src = "pump1m"
        except Exception: c = None
    if not c:
        try:
            o = get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{p['pairAddress']}/ohlcv/minute?aggregate=1&limit=1000")["data"]["attributes"]["ohlcv_list"]
            c = sorted((x[0], x[1], x[2], x[3], x[4]) for x in o); src = "gt1m"; time.sleep(4)
        except Exception as e:
            c = []; src = "err"
    cache[m] = (c, src); return cache[m]

done = {}
if os.path.exists(OUT):
    for l in open(OUT):
        x = json.loads(l); done[(x["scanner"], x.get("pool") or x.get("mint"), int(x["t"]))] = x
res = []
now = time.time()
# Messaged signals first; paused/log-only (dex paid, capitulation) last. Hard time budget so the file always gets rewritten.
_PRI = {"newpool": 0, "sw": 1, "kol": 2}
alerts.sort(key=lambda a: (_PRI.get(a["scanner"], 3 if a["signal"] in ("Dev sold at a loss", "Side wallet sold") else 4), -a["t"]))
_T0 = time.time(); BUDGET = float(os.environ.get("GRADE_BUDGET", 420))
for a in alerts:
    if time.time() - _T0 > BUDGET:
        _k = (a["scanner"], a.get("pool") or a.get("mint"), int(a["t"])); a.pop("pair", None)
        res.append(done.get(_k) or {**a, "err": "budget"}); continue
    key = (a["scanner"], a.get("pool") or a.get("mint"), int(a["t"]))
    p = a.pop("pair", None)
    dk = done.get(key)
    if dk and "err" not in dk and "clean_2x" in dk and (now - a["t"] > 86400 or dk.get("graded_at", 0) > now - 1800):
        a.pop("pair", None); res.append(dk); continue
    if not p: res.append({**a, "err": "no pair"}); continue
    c, src = candles(p)
    mc_now = p.get("marketCap") or p.get("fdv") or 0; pr_now = float(p.get("priceUsd") or 0)
    scale = mc_now / pr_now if pr_now else 0
    a.update(mint=p["baseToken"]["address"], pair=p["pairAddress"], url=p["url"], dex=p.get("dexId"), src=src, mc_now=round(mc_now),
             liq_now=round((p.get("liquidity") or {}).get("usd", 0) or 0), socials=bool((p.get("info") or {}).get("socials")),
             website=bool((p.get("info") or {}).get("websites")), age_h=round((a["t"] * 1000 - (p.get("pairCreatedAt") or 0)) / 3.6e6, 2))
    after = [x for x in c if x[0] >= a["t"] - 60]
    if not after or not scale:
        a["err"] = "no candles"; res.append(a); continue
    if not a.get("mc"): a["mc"] = after[0][1] * scale
    entry_c = next((x for x in after if x[0] >= a["t"] + 60), after[0])
    entry = entry_c[2]  # pessimistic: candle high
    post = [x for x in after if x[0] >= entry_c[0]]
    pk = max(post, key=lambda x: x[2])
    _t2 = next((x for x in post if x[2] * scale >= 2 * a["mc"]), None)
    a["min_to_2x"] = round((_t2[0] - a["t"]) / 60) if _t2 else None
    a.update(peak_x=round(pk[2] * scale / a["mc"], 2), low_x=round(min(x[3] for x in post) * scale / a["mc"], 2), now_x=round(mc_now / a["mc"], 2),
             min_to_peak=round((pk[0] - a["t"]) / 60), entry_x=round(entry * scale / a["mc"], 2), entry_peak_x=round(pk[2] / entry, 2))
    # Rohit Oct 8 00:16 IST: success = target hit BEFORE the coin ever trades at <= 0.5x alert MC (stop at -50%).
    # Ordered 1m candles: a candle that touches both target and 0.5x counts as stopped (unknown order, conservative).
    for T, k in ((2.0, "2x"), (2.5, "2_5x")):
        mae = 1.0; res_ = None
        for x in post:
            hi, lo = x[2] * scale / a["mc"], x[3] * scale / a["mc"]
            if lo <= 0.5: res_ = False; mae = min(mae, lo); break
            if hi >= T: res_ = True; a[f"min_to_clean_{k}"] = round((x[0] - a["t"]) / 60); break
            mae = min(mae, lo)
        a[f"clean_{k}"] = bool(res_)
        a[f"mae_before_{k}"] = round(mae, 2)
    # stop would have killed a winner: touched 0.5x first, later reached 2x
    a["stopped_then_2x"] = (not a["clean_2x"]) and a["peak_x"] >= 2 and any(x[3] * scale / a["mc"] <= 0.5 for x in post if x[0] <= pk[0])
    # Rohit's drawdown rule (Oct 8 00:16-00:17 IST): success = target hit BEFORE mcap touches 0.5x alert MC.
    # Primary metric CLEAN_2X (2R with stop at 0.5x); stretch CLEAN_2.5X (3R). Candles from the alert minute on, in order;
    # a candle touching both stop and target counts as stop first (pessimistic).
    am = a["mc"]; stop_t = None; hit = {}; mae = 1e9
    for x in after:
        lo, hi = x[3] * scale / am, x[2] * scale / am
        for k in (2.0, 2.5):
            if k not in hit and stop_t is None and hi >= k and lo > 0.5: hit[k] = x[0]
        if stop_t is None and lo <= 0.5: stop_t = x[0]
        if 2.0 not in hit: mae = min(mae, lo)
    a["clean_2x"] = 2.0 in hit; a["clean_25x"] = 2.5 in hit
    a["min_to_clean2x"] = round((hit[2.0] - a["t"]) / 60) if 2.0 in hit else None
    a["min_to_clean25x"] = round((hit[2.5] - a["t"]) / 60) if 2.5 in hit else None
    a["mae_before_2x"] = round(mae, 2) if mae < 1e9 else None
    a["stop_then_2x"] = bool(stop_t is not None and any(x[0] > stop_t and x[2] * scale >= 2 * am for x in after))
    # sim: TP 2x, SL 0.5x, else last close
    out = None
    for x in post:
        if x[3] <= entry * 0.5: out = 0.5; break
        if x[2] >= entry * 2: out = 2.0; break
    if out is None: out = post[-1][4] / entry
    a["sim_tp2_net"] = round(out * (1 - FEE), 2)
    # best case: sold at peak
    a["best_net"] = round(pk[2] / entry * (1 - FEE), 2)
    a["graded_at"] = now
    res.append(a)
    with open(OUT, "a") as f: f.write(json.dumps(a) + "\n")
with open(OUT, "w") as f:
    for r in res: f.write(json.dumps(r) + "\n")
print(len(res), sum("err" in r for r in res))
