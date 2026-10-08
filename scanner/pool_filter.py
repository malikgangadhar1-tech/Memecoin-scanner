#!/usr/bin/env python3
"""Deterministic pool filter v2 (port of the LLM-run new-pool screen).

Source: GeckoTerminal public API (free, no key).
Usage:  python3 scanner/pool_filter.py [--dry]
  --dry : fetch + screen + print, but write nothing (no ledger / csv / log / outcome updates).
Prints one JSON object: {"alerts":[...],"pages":n,"errors":[...]} (plus "outcomes" updates info).

Grader note: grading/grade.py already reads data/alert_outcomes.csv as the pool-filter source
(scanner "newpool"), so pool alerts are NOT also written to scanner/alerts_log.jsonl
(that would double-count them). Pass --jsonl to force the jsonl write anyway.
"""
import csv, datetime, json, os, re, sys, time, urllib.request, urllib.error

P = "/workspace/projects/7ed675e0-9a24-4f62-9d59-e27bf0c6ccd0"
LEDGER = f"{P}/data/pool_alerted.json"
CSV = f"{P}/data/alert_outcomes.csv"
JSONL = f"{P}/scanner/alerts_log.jsonl"
GT = "https://api.geckoterminal.com/api/v2"
WSOL = "So11111111111111111111111111111111111111112"
HEADER = ["pool", "ticker", "alerted_at", "alert_mcap", "alert_liq", "alert_buyers", "alert_buys",
          "alert_sells", "mcap_1h", "mcap_6h", "mcap_24h", "peak_mcap_seen", "verdict"]
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MAX_PAGES, MAX_AGE_H, GAP_S, BACKOFF_S, RETRIES = 10, 4.0, 6.5, 60, 2
MAX_OUTCOME_FETCHES = 5
BUDGET_S = 7 * 60 + 15  # hard stop well under 8 min

DRY = "--dry" in sys.argv
FORCE_JSONL = "--jsonl" in sys.argv
T0 = time.time()
errors = []
_last_call = [0.0]


def left():
    return BUDGET_S - (time.time() - T0)


def gt_get(path, retries=RETRIES):
    """GET with >=GAP_S spacing, 60s backoff on 429 (max `retries` retries). Returns json or None."""
    for attempt in range(retries + 1):
        wait = GAP_S - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        if left() < 15:
            errors.append(f"time budget exhausted before {path}")
            return None
        _last_call[0] = time.time()
        try:
            req = urllib.request.Request(GT + path, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=20))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                if left() < BACKOFF_S + 20:
                    errors.append(f"429 on {path}, no time left to back off")
                    return None
                time.sleep(BACKOFF_S)
                continue
            errors.append(f"HTTP {e.code} on {path}" + (" (retries exhausted)" if e.code == 429 else ""))
            return None
        except Exception as e:
            if attempt < retries:
                time.sleep(3)
                continue
            errors.append(f"{type(e).__name__} on {path}: {e}")
            return None
    return None


def parse_ts(s):
    """Parse ISO-8601 (GT raw) or the 'Wed 2026-10-07 1:30 PM IST' / '2026-10-07 13:30 IST' styles."""
    if not s:
        return None
    s = str(s).strip()
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    m = re.search(r"(\d{4}-\d\d-\d\d)[ T](\d{1,2}):(\d\d)(?::\d\d)?\s*(AM|PM)?", s)
    if not m:
        return None
    h = int(m[2])
    if m[4]:
        h = h % 12 + (12 if m[4] == "PM" else 0)
    dt = datetime.datetime.strptime(f"{m[1]} {h}:{m[3]}", "%Y-%m-%d %H:%M")
    tz = datetime.timezone.utc if ("Z" in s[10:] or "UTC" in s and "IST" not in s) else IST
    return dt.replace(tzinfo=tz).timestamp()


def fnum(x):
    try:
        return float(x) if x not in (None, "") else None
    except Exception:
        return None


def ist_str(ts):
    return datetime.datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M IST")


def mcap_of(a):
    m = fnum(a.get("market_cap_usd"))
    return m if m else fnum(a.get("fdv_usd"))


# ---------------- outcome tracking ----------------
def track_outcomes(now):
    info = {"fetched": 0, "updated": []}
    if not os.path.exists(CSV):
        return info
    with open(CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    changed = False
    for r in rows:
        if info["fetched"] >= MAX_OUTCOME_FETCHES or left() < 200:
            break
        t = parse_ts(r.get("alerted_at"))
        if t is None:
            continue
        age = now - t
        wins = (("mcap_1h", 1), ("mcap_6h", 6), ("mcap_24h", 24))
        due = [c for c, h in wins if age >= h * 3600 and not (r.get(c) or "").strip()]
        if not due:
            continue
        # A window is "missed" if the next window has also passed: a late reading would be mislabelled,
        # so it gets "n/a" instead (24h is never missed; a late 24h reading still decides the verdict).
        cur = [c for c, h in wins if age >= h * 3600][-1]
        d = gt_get(f"/networks/solana/pools/{r['pool']}", retries=0)  # no backoff here: screening has priority
        info["fetched"] += 1
        if not d or "data" not in d:
            info["stopped"] = "fetch failed (likely 429); outcome tracking deferred to next run"
            break
        a = d["data"]["attributes"]
        mc = mcap_of(a)
        if mc is None:
            continue
        mc = round(mc)
        for c in due:
            r[c] = str(mc) if c == cur else "n/a"
        peak = max([v for v in (fnum(r.get("peak_mcap_seen")), fnum(r.get("alert_mcap")), mc) if v is not None])
        r["peak_mcap_seen"] = str(round(peak))
        if "mcap_24h" in due and not (r.get("verdict") or "").strip():
            am = fnum(r.get("alert_mcap")) or 0
            res = fnum(a.get("reserve_in_usd"))
            if am and peak >= 2 * am:
                r["verdict"] = "GOOD"
            elif (am and mc < 0.5 * am) or (res is not None and res < 1000):
                r["verdict"] = "BAD"
            else:
                r["verdict"] = "FLAT"
        info["updated"].append({"pool": r["pool"], "ticker": r.get("ticker"), "filled": cur, "na": [c for c in due if c != cur], "mcap": mc,
                                "peak": r["peak_mcap_seen"], "verdict": r.get("verdict") or None})
        changed = True
    if changed and not DRY:
        tmp = CSV + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in HEADER})
        os.replace(tmp, CSV)
    return info


# ---------------- screen ----------------
def screen(p, now):
    """Return (alert_dict or None, reason)."""
    a = p["attributes"]
    rel = p.get("relationships", {})
    qid = (rel.get("quote_token", {}).get("data") or {}).get("id", "")
    if not qid.endswith(WSOL):
        return None, "not SOL-quoted"
    created = parse_ts(a.get("pool_created_at"))
    if created is None:
        return None, "no created_at"
    age_s = now - created
    if age_s >= MAX_AGE_H * 3600:
        return None, "age"
    liq = fnum(a.get("reserve_in_usd")) or 0
    if liq < 5000:
        return None, "liq<5k"
    mc = mcap_of(a)
    if mc is None or not (15000 <= mc <= 30000):
        return None, "mcap range"
    if liq < mc:
        return None, "liq<mcap"
    win = "h24" if age_s > 6 * 3600 else "h6"
    tx = (a.get("transactions") or {}).get(win) or {}
    buys, sells = int(tx.get("buys") or 0), int(tx.get("sells") or 0)
    buyers, sellers = int(tx.get("buyers") or 0), int(tx.get("sellers") or 0)
    if buyers < 50:
        return None, "buyers<50"
    if buys >= 1.5 * buyers:
        return None, "buys>=1.5x buyers"
    if sells < 0.10 * buys or sellers < 5:
        return None, "weak sells"
    vol = a.get("volume_usd") or {}
    buy_vol = fnum((a.get("buy_volume_usd") or {}).get(win)) if isinstance(a.get("buy_volume_usd"), dict) else None
    if buy_vol is not None and buys:
        avg_buy = buy_vol / buys
    else:
        tot = fnum(vol.get(win)) or 0
        avg_buy = tot / (buys + sells) if (buys + sells) else 0
    if avg_buy < 20:
        return None, "avg buy<$20"
    flags = []
    if sellers and sells / sellers > 3:
        flags.append("sells concentrated")
    name = a.get("name") or ""
    ticker = name.split(" / ")[0].strip() if " / " in name else name.strip()
    base_id = (rel.get("base_token", {}).get("data") or {}).get("id", "")
    mint = base_id.split("_", 1)[1] if "_" in base_id else base_id
    pool = a.get("address")
    return {
        "ticker": ticker, "mcap": round(mc), "liq": round(liq), "buyers": buyers, "buys": buys, "sells": sells,
        "age_min": round(age_s / 60), "launched_ist": ist_str(created), "flags": flags,
        "url": f"https://dexscreener.com/solana/{pool}",
        "_pool": pool, "_mint": mint, "_avg_buy": round(avg_buy, 2), "_sellers": sellers,
    }, "pass"


def main():
    now = time.time()
    try:
        alerted = json.load(open(LEDGER)) if os.path.exists(LEDGER) else []
    except Exception as e:
        errors.append(f"ledger unreadable ({e}); treating as empty")
        alerted = []
    alerted_set = set(alerted)
    if os.path.exists(CSV):  # belt and braces: csv pools also count as already alerted
        with open(CSV, newline="") as f:
            alerted_set |= {r["pool"] for r in csv.DictReader(f) if r.get("pool")}

    outcomes = track_outcomes(now)

    pages, seen, hits, reasons, page_stats = 0, set(), [], {}, []
    for page in range(1, MAX_PAGES + 1):
        if left() < 30:
            errors.append("time budget hit; stopped paging")
            break
        d = gt_get(f"/networks/solana/new_pools?page={page}")
        if not d or "data" not in d:
            errors.append(f"page {page} failed; stopped paging")
            break
        pages += 1
        data = d["data"] or []
        now = time.time()
        oldest_ok = False
        ages = [parse_ts(p["attributes"].get("pool_created_at")) for p in data]
        ages = [round((now - t) / 60) for t in ages if t]
        page_stats.append({"page": page, "n": len(data), "new": sum(1 for p in data if p["attributes"].get("address") not in seen),
                           "age_min_range": [min(ages), max(ages)] if ages else None})
        for p in data:
            addr = p["attributes"].get("address")
            if addr in seen:
                continue
            seen.add(addr)
            ct = parse_ts(p["attributes"].get("pool_created_at"))
            if ct is not None and now - ct < MAX_AGE_H * 3600:
                oldest_ok = True
            al, why = screen(p, now)
            reasons[why] = reasons.get(why, 0) + 1
            if al and al["_pool"] not in alerted_set:
                hits.append(al)
                alerted_set.add(al["_pool"])
            elif al:
                reasons["already alerted"] = reasons.get("already alerted", 0) + 1
        if not data or not oldest_ok:  # whole page older than 4h -> stop
            break

    if hits and not DRY:
        new_csv = not os.path.exists(CSV)
        with open(CSV, "a", newline="") as f:
            w = csv.writer(f)
            if new_csv:
                w.writerow(HEADER)
            stamp = ist_str(time.time())
            for h in hits:
                w.writerow([h["_pool"], h["ticker"], stamp, h["mcap"], h["liq"], h["buyers"], h["buys"], h["sells"],
                            "", "", "", h["mcap"], ""])
        alerted.extend(h["_pool"] for h in hits)
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER + ".tmp", "w") as f:
            json.dump(alerted, f)
        os.replace(LEDGER + ".tmp", LEDGER)
        if FORCE_JSONL:
            with open(JSONL, "a") as f:
                for h in hits:
                    f.write(json.dumps({"t": time.time(), "scanner": "pool", "signal": "pool filter v2", "mint": h["_mint"],
                                        "pair": h["_pool"], "symbol": h["ticker"], "mc": h["mcap"]}) + "\n")

    out = {"alerts": [{k: v for k, v in h.items() if not k.startswith("_")} for h in hits],
           "pages": pages, "errors": errors,
           "outcomes": outcomes, "screened": len(seen), "page_stats": page_stats, "reject_reasons": reasons,
           "dry": DRY, "runtime_s": round(time.time() - T0, 1)}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
