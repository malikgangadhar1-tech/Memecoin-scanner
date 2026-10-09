#!/usr/bin/env python3
"""Screen GeckoTerminal Solana new_pools against the user's organic-launch filter.
Prints JSON {checked, passing:[...]} and updates data/pool_alerted.json for passing pools
only when run with --commit."""
import json, os, re, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

ROOT = "/workspace/projects/7ed675e0-9a24-4f62-9d59-e27bf0c6ccd0"
ALERTED = f"{ROOT}/data/pool_alerted.json"
SOL = "So11111111111111111111111111111111111111112"
IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(15 * (i + 1))


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    m = re.match(r"\w+ (\d{4}-\d{2}-\d{2}) (\d{1,2}:\d{2} [AP]M) .*UTC([+-]\d{2}):(\d{2})", s)
    if m:
        d = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %I:%M %p")
        sign = 1 if m.group(3)[0] == "+" else -1
        off = timedelta(hours=int(m.group(3)[1:]), minutes=int(m.group(4))) * sign
        return d.replace(tzinfo=timezone(off))
    return None


def f(x):
    try:
        return float(x)
    except Exception:
        return None


def main():
    commit = "--commit" in sys.argv
    alerted = []
    if os.path.exists(ALERTED):
        try:
            alerted = json.load(open(ALERTED))
        except Exception:
            alerted = []
    seen = set(alerted)
    now = datetime.now(timezone.utc)
    checked, passing, reasons, errors = 0, [], {}, []
    # Oct 7 audit: new_pools only reaches ~10 min back (~20 launches/min), so pools that hit $15-30k later
    # were never seen (e.g. HEALTHCOIN 4.7x). Also screen trending pools (same rules, same <4h age cap).
    G = "https://api.geckoterminal.com/api/v2/networks/solana"
    urls = [f"{G}/new_pools?page={i}" for i in range(1, 7)] + [f"{G}/trending_pools?duration=5m&page=1", f"{G}/trending_pools?duration=5m&page=2", f"{G}/trending_pools?duration=1h&page=1"]
    for page, url in enumerate(urls, 1):
        try:
            d = get(url)
        except Exception as e:
            errors.append(f"page {page}: {e}")
            if "new_pools" in url: continue
            continue
        pools = d.get("data", [])
        if not pools:
            break
        oldest_h = 0
        for p in pools:
            a = p["attributes"]
            created = parse_ts(a.get("pool_created_at"))
            if not created:
                continue
            age_h = (now - created).total_seconds() / 3600
            oldest_h = max(oldest_h, age_h)
            checked += 1
            def fail(r):
                reasons[r] = reasons.get(r, 0) + 1
            if age_h >= 4:
                fail("age"); continue
            q = p["relationships"]["quote_token"]["data"]["id"].split("_", 1)[1]
            if q != SOL:
                fail("not_sol"); continue
            addr = a["address"]
            if addr in seen:
                fail("already_alerted"); continue
            seen.add(addr)
            liq = f(a.get("reserve_in_usd")) or 0
            mcap = f(a.get("market_cap_usd")) or f(a.get("fdv_usd")) or 0
            win = "h24" if age_h > 6 else "h6"
            t = a["transactions"][win]
            buys, sells, buyers, sellers = t["buys"], t["sells"], t["buyers"], t["sellers"]
            vol = f(a["volume_usd"][win]) or 0
            avg = vol / (buys + sells) if (buys + sells) else 0
            if liq < 5000: fail("liq"); continue
            if not (15000 <= mcap <= 30000): fail("mcap"); continue
            if liq < 0.15 * mcap: fail("liq_below_15pct_mcap"); continue  # v3 (Rohit, Oct 7 21:11 IST): liq >= 15% of mcap (was liq >= mcap)
            if buyers < 50: fail("buyers"); continue  # v2 (user-approved). 300 was proposed in the Oct 7 audit, NOT approved
            if buys >= 1.5 * buyers: fail("wash"); continue
            if sells < 0.1 * buys or sellers < 5: fail("sells"); continue
            if avg < 20: fail("dust"); continue
            mint = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
            passing.append(dict(addr=addr, mint=mint, name=a["name"], mcap=mcap, liq=liq,
                                tag=("SCALP (thin pool, few min)" if liq < mcap else ""),  # Rohit Oct 7 21:44: thin pool = few-min scalp
                                buyers=buyers, buys=buys, sells=sells, sellers=sellers,
                                avg=round(avg, 2), age_min=round(age_h * 60),
                                created_ist=created.astimezone(IST).strftime("%-I:%M %p IST")))
        time.sleep(2)
    # v3 (Rohit, Oct 7 21:11-21:12 IST): >= 8 distinct buyer wallets in the 5 min before alert.
    # Many small bundles with different wallets are fine; ONE big bundle is not:
    # reject if a single block holds >= 5 distinct buyers AND >= 50% of the 5-min buyers.
    kept = []
    for pp in passing:
        time.sleep(2)
        try:
            tr = get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pp['addr']}/trades")["data"]
        except Exception as e:
            errors.append(f"trades {pp['addr']}: {e}"); reasons["trades_unavailable"] = reasons.get("trades_unavailable", 0) + 1; continue
        cut = now - timedelta(minutes=5)
        byblock, allw = {}, set()
        # ACCELERATION LABEL (Rohit Oct 8 16:34 IST, physics read: velocity vs acceleration): distinct buyers 5-10 min ago.
        cut2 = now - timedelta(minutes=10); prevw = set(); oldest = None
        for t in tr:
            a = t["attributes"]
            ts = parse_ts(a.get("block_timestamp"))
            if ts and (oldest is None or ts < oldest): oldest = ts
            if a.get("kind") == "buy" and ts and cut2 <= ts < cut: prevw.add(a.get("tx_from_address"))
            if a.get("kind") != "buy" or not ts or ts < cut: continue
            byblock.setdefault(a.get("block_number"), set()).add(a.get("tx_from_address")); allw.add(a.get("tx_from_address"))
        big = max((len(w) for w in byblock.values()), default=0)
        pp["buyers_5m"] = len(allw); pp["largest_bundle_5m"] = big
        pp["buyers_prev5m"] = len(prevw) if (oldest is not None and oldest <= cut2) else None  # None = trade page doesn't reach 10 min back
        # Busy pools: GT's trade page often covers < 10 min, so also split the covered window (max 5 min) into halves.
        if oldest is not None:
            # anchor on the newest trade, not the runner clock (runner clock lags GT -> negative windows, fix Oct 8 20:30)
            newest = max((parse_ts(t["attributes"].get("block_timestamp")) for t in tr if t["attributes"].get("block_timestamp")), default=now)
            start = max(newest - timedelta(minutes=5), oldest); mid = start + (newest - start) / 2
            h1, h2 = set(), set()
            for t in tr:
                a = t["attributes"]; ts = parse_ts(a.get("block_timestamp"))
                if a.get("kind") != "buy" or not ts or ts < start: continue
                (h2 if ts >= mid else h1).add(a.get("tx_from_address"))
            pp["buyers_half_early"], pp["buyers_half_late"] = len(h1), len(h2)
            pp["half_window_s"] = round((newest - start).total_seconds() / 2)
        if len(allw) < 8 or (big >= 5 and big >= 0.5 * len(allw)):
            try:
                with open(os.path.join(os.path.dirname(ALERTED), "pool_rejects_v3.jsonl"), "a") as fh:
                    fh.write(json.dumps(dict(ts=now.isoformat(), addr=pp["addr"], mint=pp["mint"], name=pp["name"], mcap=pp["mcap"], buyers_5m=len(allw), largest_bundle_5m=big)) + "\n")
            except Exception: pass
        if len(allw) < 8:
            reasons["buyers_5m_lt8"] = reasons.get("buyers_5m_lt8", 0) + 1; continue
        if big >= 5 and big >= 0.5 * len(allw):
            reasons["one_big_bundle"] = reasons.get("one_big_bundle", 0) + 1; continue
        kept.append(pp)
    passing = kept
    for pp in passing:
        time.sleep(2)
        try:
            info = get(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{pp['mint']}/info")["data"]["attributes"]
            pp["symbol"] = info.get("symbol") or pp["name"].split(" / ")[0]
            pp["socials"] = bool(info.get("websites") or info.get("twitter_handle") or info.get("telegram_handle"))
        except Exception:
            pp["symbol"] = pp["name"].split(" / ")[0]
            pp["socials"] = None
        # Anti-rug LABELS (Rohit Oct 8 09:16 IST): dev % and top-10 holder %, label only until v3 n=50
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from holder_check import holder_labels
            hl = holder_labels(pp["mint"], pp["addr"]); pp.update(dev_pct=hl["dev_pct"], top10_pct=hl["top10_pct"])
            from farm_devs import is_farm  # FARM DEV label (Rohit Oct 8 22:46 IST), label only
            pp["creator"] = hl.get("creator"); pp["farm_dev"] = is_farm(hl.get("creator"))
            if commit:
                with open(os.path.join(os.path.dirname(ALERTED), "v3_holder_labels.jsonl"), "a") as fh:
                    fh.write(json.dumps(dict(t=time.time(), mint=pp["mint"], pool=pp["addr"], **hl)) + "\n")
        except Exception as e:
            errors.append(f"holders {pp['mint']}: {e}")
    # TOP10 CUT (Rohit Oct 9 09:47 IST "go"): drop a pass whose top-10 non-pool wallets hold >30% of supply.
    # In-sample n=29: kept 10/15 clean 2x, 6/15 5x+; cut 4/14 clean, 0 runners. Unknown top10 (holder check failed) is KEPT.
    kept1 = []
    for pp in passing:
        t10 = pp.get("top10_pct")
        if t10 is not None and t10 > 30:
            reasons["top10_over_30"] = reasons.get("top10_over_30", 0) + 1
            if commit:
                with open(os.path.join(os.path.dirname(ALERTED), "pool_rejects_v3.jsonl"), "a") as fh:
                    fh.write(json.dumps(dict(t=time.time(), pool=pp["addr"], mint=pp.get("mint"), sym=pp.get("symbol"), mc=pp.get("mcap"), top10_pct=t10, dev_pct=pp.get("dev_pct"), reason="top10_over_30")) + "\n")
                alerted.append(pp["addr"])
            continue
        kept1.append(pp)
    passing = kept1
    # COPYCAT CUT (Rohit Oct 8 15:52 IST "eliminate v3 copycat ticker"; backtest 4/18 clean vs 15/27):
    # drop a pass whose ticker was already alerted earlier by any signal, or by an earlier pass this tick.
    SEEN = os.path.join(os.path.dirname(ALERTED), "seen_symbols.json")
    try: seen = set(json.load(open(SEEN)))
    except Exception:
        seen = set()
        try:
            for l in open(os.path.join(ROOT, "grading", "outcomes.jsonl")):
                s_ = (json.loads(l).get("sym") or "").strip().lower()
                if s_: seen.add(s_)
        except Exception: pass
    kept2 = []
    for pp in passing:
        s_ = (pp.get("symbol") or "").strip().lower()
        if s_ and s_ in seen:
            reasons["copycat_ticker"] = reasons.get("copycat_ticker", 0) + 1
            if commit:
                with open(os.path.join(os.path.dirname(ALERTED), "pool_rejects_v3.jsonl"), "a") as fh:
                    fh.write(json.dumps(dict(t=time.time(), pool=pp["addr"], mint=pp.get("mint"), sym=pp.get("symbol"), mc=pp.get("mcap"), reason="copycat_ticker")) + "\n")
                alerted.append(pp["addr"])
            continue
        if s_: seen.add(s_)
        kept2.append(pp)
    passing = kept2
    if commit:
        json.dump(sorted(seen), open(SEEN, "w"))
    if commit and passing:
        alerted += [pp["addr"] for pp in passing]
        os.makedirs(os.path.dirname(ALERTED), exist_ok=True)
        json.dump(alerted, open(ALERTED, "w"))
    if not os.path.exists(ALERTED):
        json.dump([], open(ALERTED, "w"))
    print(json.dumps(dict(checked=checked, rejects=reasons, errors=errors, passing=passing), indent=1))


if __name__ == "__main__":
    main()
