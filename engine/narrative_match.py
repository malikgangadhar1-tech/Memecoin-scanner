#!/usr/bin/env python3
"""narrative_match.py - match brand-new pools against viral trending phrases.

Two sources (a fresh coin named for a <48h viral story = investigate):
  1. watch.db pools first seen in the last 24h (deep coverage)
  2. GeckoTerminal new_pools pages 1-2 live (catches coins watch.py missed,
     e.g. fast graduates that aged off the discovery pages)

Matching handles concatenated names ("JaneDoe" matches phrase "jane doe")
via contiguous token-subsequence squashing.

Output: MATCH lines (deduped into narrative_hits.csv). Silent otherwise.
"""
import os, re, sqlite3, csv, json, urllib.request, time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
PHRASES = os.path.join(HERE, "trending_phrases.txt")
HITS = os.path.join(HERE, "narrative_hits.csv")
GT = "https://api.geckoterminal.com/api/v2"
UA = {"User-Agent": "Mozilla/5.0"}
MAX_AGE_H = 24


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def toks(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def load_phrases():
    phrases = []
    if not os.path.exists(PHRASES):
        return phrases
    with open(PHRASES) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line not in [p[0] for p in phrases]:
                phrases.append((line, toks(line)))
    return phrases


def match_score(name, phrase_toks):
    """Contiguous token subsequences (len>=2, or single tok len>=5),
    squashed, checked as substring of the normalized name."""
    nn = norm(name)
    n = len(phrase_toks)
    # longest first so "jane doe" beats "jane"
    for L in range(n, 0, -1):
        for i in range(n - L + 1):
            sub = phrase_toks[i:i + L]
            if L == 1 and len(sub[0]) < 5:
                continue
            sq = "".join(sub)
            if L >= 2 and len(sq) < 6:
                continue  # "i am"->"iam" is too weak, avoid Miriam-style hits
            if sq and sq in nn:
                return (1.0 if L >= 2 else 0.7, " ".join(sub))
    return 0.0, ""


def db_pools():
    out = []
    try:
        con = sqlite3.connect(DB, timeout=30)
        con.row_factory = sqlite3.Row
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(hours=MAX_AGE_H)).strftime("%Y-%m-%dT%H:%M:%S")
        for p in con.execute(
                "SELECT pool,name,base_mint,first_seen,mcap_usd FROM pools "
                "WHERE first_seen >= ? AND COALESCE(dead,0)=0", (cutoff,)):
            out.append({"pool": p["pool"], "name": p["name"] or "",
                        "base_mint": (p["base_mint"] or "").replace("solana_", ""),
                        "seen": p["first_seen"], "mcap": p["mcap_usd"],
                        "src": "db"})
        con.close()
    except Exception as e:
        print(f"db read failed ({e}), using API only")
    return out


def api_pools():
    out = []
    for page in (1, 2):
        try:
            req = urllib.request.Request(
                f"{GT}/networks/solana/new_pools?page={page}", headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            time.sleep(0.4)
        except Exception as e:
            print(f"new_pools p{page} failed ({e})")
            continue
        for p in data.get("data", []):
            a = p.get("attributes", {})
            rel = p.get("relationships", {})
            base = ((rel.get("base_token", {}) or {}).get("data", {}) or {}).get("id", "")
            out.append({"pool": a.get("address", ""), "name": a.get("name", ""),
                        "base_mint": base.replace("solana_", ""),
                        "seen": a.get("pool_created_at", ""),
                        "mcap": a.get("market_cap_usd"),
                        "src": "api"})
    return out


def main():
    phrases = load_phrases()
    if not phrases:
        print("no phrases in trending_phrases.txt")
        return
    pools = db_pools() + api_pools()
    seen_pools = set()
    hits = []
    for p in pools:
        if p["pool"] in seen_pools or not p["name"]:
            continue
        seen_pools.add(p["pool"])
        for raw, ptoks in phrases:
            sc, why = match_score(p["name"], ptoks)
            if sc >= 0.7:
                hits.append((sc, why, raw, p))
                break

    seen = set()
    if os.path.exists(HITS):
        with open(HITS) as f:
            for row in csv.DictReader(f):
                seen.add((row["pool"], row["phrase"]))
    new = 0
    with open(HITS, "a", newline="") as f:
        w = csv.writer(f)
        if not seen:
            w.writerow(["ts", "phrase", "why", "pool", "name",
                        "base_mint", "seen", "mcap_usd", "src"])
        for sc, why, raw, p in sorted(hits, key=lambda h: h[0], reverse=True):
            key = (p["pool"], raw)
            if key in seen:
                continue
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            mcap = p["mcap"]
            try:
                mcap = float(mcap or 0)
            except (TypeError, ValueError):
                mcap = 0
            w.writerow([ts, raw, why, p["pool"], p["name"],
                        p["base_mint"], p["seen"], f"{mcap:.0f}", p["src"]])
            print(f"MATCH [{why}] '{raw}' -> {p['name']} "
                  f"pool={p['pool']} mcap=${mcap:,.0f} [{p['src']}]")
            new += 1
    if not new:
        print(f"ok: {len(seen_pools)} young pools checked, no new narrative matches")


if __name__ == "__main__":
    main()
