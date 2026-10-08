#!/usr/bin/env python3
"""wallet-live/classify_candidates.py - the pleb classifies converged wallets.

Reads candidates.json (wallets early on >=2 runner pools). Builds an evidence
bundle for each wallet SOLELY from our own watch.db (trades we recorded live:
per-pool buys/sells, timing, sizes) - no invented data, no RPC archive needed.

Hands each bundle to Gemini (3.5-flash per Rohit's directive; falls back to
3-flash-preview if 3.5 503s) for INDEPENDENT-TRADER / COPY-TRADER / SNIPER-BOT /
WASH / BUNDLE / INCONCLUSIVE classification.

Saves to classifications/<wallet>.json. Resumable. Silent unless failures.
"""
import json, os, sqlite3, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
CANDS = os.path.join(HERE, "candidates.json")
GEMINI = os.path.expanduser("~/workspace/skills/gemini/bin/gemini")
OUT = os.path.join(HERE, "classifications")
os.makedirs(OUT, exist_ok=True)

MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview"]  # directive first, fallbacks on 503

PROMPT = """You are a Solana memecoin forensics analyst. Classify this wallet based ONLY on
the on-chain evidence below, which was recorded live by our own watcher (every trade
listed actually happened; timestamps/sizes are real). Never invent trades, profits,
or relationships.

Context: this wallet was a LAUNCH-WINDOW buyer (among the first ~150 buyers) on
{coins}.

BEHAVIORAL FINGERPRINT of a real independent trader (calibrated from 5 verified
profitable humans):
- buys are manual-sized and irregular (not identical lamports every time)
- holds winners minutes-to-hours, not seconds; takes profit in tranches
- trades a spread of coins over days, not one burst then silence
- entry timing varies (not always block 0-1 = sniper marker)
- pays normal priority fees, not extreme snipe fees

WALLET EVIDENCE (from our tracked pools only):
{evidence}

Classify as one of: INDEPENDENT-TRADER / COPY-TRADER / SNIPER-BOT / WASH / BUNDLE / INCONCLUSIVE.
Then give:
1. Verdict (one line) + confidence (high/med/low).
2. Evidence FOR real independent trading (cite specific data).
3. Evidence AGAINST (bot/copy/wash markers - cite specific data).
4. What would change your mind (one concrete check).
5. Track-worthy? YES/NO and why in one line.
"""


def evidence_bundle(con, wallet):
    # Optimized 2026-10-02: trades has no index on wallet (2M rows), so the old
    # per-pool/per-kind loop did ~75 full table scans per wallet. Now: 3 single
    # full scans + per-pool first-buy lookups that ride idx_trades_pool_ts.
    agg = {}
    pools_seen = set()
    for pool, kind, n, usd in con.execute(
            "SELECT pool, kind, COUNT(*), COALESCE(SUM(vol_usd),0)"
            " FROM trades WHERE wallet=? GROUP BY pool, kind", (wallet,)):
        agg.setdefault(pool, {})[kind] = (n, usd or 0)
        pools_seen.add(pool)
    names = dict(con.execute(
        "SELECT DISTINCT t.pool, p.name FROM trades t JOIN pools p ON p.pool=t.pool"
        " WHERE t.wallet=?", (wallet,)))
    pools_seen |= set(names)
    n, mn, mx = con.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM trades WHERE wallet=?", (wallet,)).fetchone()
    lines = [f"- distinct tracked pools traded: {len(pools_seen)}"]
    for pool in sorted(pools_seen)[:25]:
        b = agg.get(pool, {}).get("buy", (0, 0))
        s = agg.get(pool, {}).get("sell", (0, 0))
        first = con.execute(
            "SELECT ts, vol_usd FROM trades WHERE pool=? AND wallet=? AND kind='buy'"
            " ORDER BY ts LIMIT 1", (pool, wallet)).fetchone()
        lines.append(f"- {names.get(pool, pool)}: {b[0]} buys (${b[1]:.0f}), {s[0]} sells (${s[1]:.0f}),"
                     f" first buy {first[0] if first else '?'} (${first[1] if first and first[1] else 0:.0f})")
    lines.append(f"- total tracked trades: {n}, span {mn} -> {mx}")
    return "\n".join(lines)


def ask_gemini(prompt):
    last_err = ""
    for m in MODELS:
        attempt = 0
        while attempt < 4:
            attempt += 1
            try:
                r = subprocess.run([GEMINI, "--model", m, prompt],
                                   capture_output=True, text=True, timeout=300)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip(), m
                last_err = (r.stderr or "")[-300:]
                low = last_err.lower()
                if "503" in last_err or "overloaded" in low:
                    time.sleep(5)
                    break  # try next model
                if "429" in last_err or "too many requests" in low or "rate limit" in low:
                    time.sleep(60 * attempt)  # back off, retry same model
                    continue
                break  # non-retryable: try next model
            except Exception as e:
                last_err = str(e)[-300:]
                time.sleep(10)
    return None, last_err


def main():
    only = sys.argv[1:]  # optional wallet filter
    if not os.path.exists(CANDS):
        print("no candidates.json yet")
        return
    cands = json.load(open(CANDS))["candidates"]
    if only:
        cands = {w: cands[w] for w in only if w in cands}
    con = sqlite3.connect(DB)
    done, failed = 0, 0
    for w, hits in cands.items():
        outp = os.path.join(OUT, w + ".json")
        if os.path.exists(outp):
            continue
        coins = ", ".join(f"{h['name'].split(' / ')[0]} (first buy {h['first_buy_ts']},"
                          f" +{h['mins_after_launch']}min, ${h['usd'] or 0:.0f})" for h in hits)
        ev = evidence_bundle(con, w)
        text, model = ask_gemini(PROMPT.format(coins=coins, evidence=ev))
        if not text:
            print(f"FAIL {w[:12]}: {model}")
            failed += 1
            continue
        json.dump({"wallet": w, "hits": hits, "model": model,
                   "classified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "verdict": text}, open(outp, "w"), indent=1)
        done += 1
        print(f"CLASSIFIED {w[:12]} via {model}")
        time.sleep(15)
    print(f"done={done} failed={failed} total_candidates={len(cands)}")
    con.close()


if __name__ == "__main__":
    main()
