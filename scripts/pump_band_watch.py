"""pump.fun band watcher (Rohit, Oct 7 23:19 IST: "can't miss 3x coins").
Polls pump.fun's most-recently-traded coins and alerts the first time a coin under 4h old sits at $15K-30K mcap
with Rohit's v3 heat rules: >= 8 distinct buyers in the last 5 min, and not one dominant bundle
(a single slot with >= 5 buyer wallets that is >= 50% of the 5-min buyers).
Usage: python3 scripts/pump_band_watch.py [--seconds 100]   (polls every ~30s for that long; prints JSON)
Alerts -> data/pump_band_alerts.jsonl (for grading); rejects -> data/pump_band_rejects.jsonl; dedupe data/pump_band_alerted.json."""
import json, os, sys, time, urllib.request
P = "/workspace/projects/7ed675e0-9a24-4f62-9d59-e27bf0c6ccd0"
sys.path.insert(0, f"{P}/research")
from heat import heat, save
sys.path.insert(0, f"{P}/scripts")
from farm_devs import FARM
LO, HI, MAX_AGE_H, MIN_B5 = 15000, 30000, 4, 8
args = sys.argv[1:]
SECS = float(args[args.index("--seconds") + 1]) if "--seconds" in args else 100
AL = f"{P}/data/pump_band_alerted.json"
alerted = json.load(open(AL)) if os.path.exists(AL) else {}
seen_rej = {}
def get(u):
    for i in range(4):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Origin": "https://pump.fun"}), timeout=15))
        except Exception as e:
            time.sleep(2 * (i + 1) if "429" in str(e) else 1)
    return None
out = []; polls = 0; t0 = time.time()
while True:
    polls += 1
    coins = []
    for off in (0, 50, 100):
        j = get(f"https://frontend-api-v3.pump.fun/coins?offset={off}&limit=50&sort=last_trade_timestamp&order=DESC&includeNsfw=true")
        if isinstance(j, list): coins += j
        time.sleep(0.5)
    now = time.time()
    for c in coins:
        m = c.get("mint"); mc = c.get("usd_market_cap") or 0
        age = now - (c.get("created_timestamp") or 0) / 1000
        if not m or m in alerted or not (LO <= mc <= HI) or age > MAX_AGE_H * 3600: continue
        if seen_rej.get(m, 0) > now - 60: continue
        h = heat(m, now)
        if not h: continue
        bundle = h["max_slot_buyers5"] >= 5 and h["max_slot_buyers5"] >= 0.5 * max(h["buyers5"], 1)
        rec = dict(t=int(now), symbol=c.get("symbol"), name=c.get("name"), mint=m, mc=round(mc), age_min=round(age / 60),
                   bonded=bool(c.get("complete")), twitter=bool(c.get("twitter")), telegram=bool(c.get("telegram")), **h)
        rec["creator"] = c.get("creator"); rec["farm_dev"] = rec["creator"] in FARM  # FARM DEV label only (Rohit Oct 8 22:46 IST)
        thin_avg = h["buys5"] and h["buyvol5"] / h["buys5"] < 20   # v3: average trade >= $20
        if h["buyers5"] < MIN_B5 or bundle or thin_avg:
            seen_rej[m] = now
            rec["why"] = "bundle" if bundle else ("buyers5<8" if h["buyers5"] < MIN_B5 else "avg<$20")
            open(f"{P}/data/pump_band_rejects.jsonl", "a").write(json.dumps(rec) + "\n"); continue
        alerted[m] = int(now)
        rec["link"] = f"https://dexscreener.com/solana/{m}"
        out.append(rec)
        open(f"{P}/data/pump_band_alerts.jsonl", "a").write(json.dumps(rec) + "\n")
    json.dump(alerted, open(AL, "w")); save()
    if time.time() - t0 + 35 > SECS: break
    time.sleep(max(0, 30 - (time.time() - now)))
print(json.dumps({"polls": polls, "alerts": out}))
