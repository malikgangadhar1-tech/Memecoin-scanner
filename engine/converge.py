#!/usr/bin/env python3
"""wallet-live/converge.py - the pleb's weekly grunt job (runs via cron, calls Gemini CLI).

Reads watch.db, and for each runner pool extracts the first EARLY_N buy-side
wallets by block_timestamp. Wallets early on >=2 runners -> candidates.

Output: candidates.json with, per wallet:
  - wallet address
  - list of runner pools: pool, name, first-buy timestamp, tx_hash, usd size,
    minutes after pool creation
Then hands candidates.json to Gemini (classify script) for labeling.
Silent unless new candidates found.
"""
import json, sqlite3, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
OUT = os.path.join(HERE, "candidates.json")
EARLY_N = 150
MIN_RUNNERS = 2


def main():
    if not os.path.exists(DB):
        print("no db yet")
        return
    con = sqlite3.connect(DB)
    runners = con.execute(
        "SELECT pool, name, created_at, runner_at, mcap_usd FROM pools WHERE runner=1").fetchall()
    if not runners:
        print("no runners yet")
        return

    wallet_hits = {}  # wallet -> list of dicts
    for pool, name, created_at, runner_at, mcap in runners:
        rows = con.execute(
            """SELECT wallet, MIN(ts) AS first_ts FROM trades
               WHERE pool=? AND kind='buy' AND wallet IS NOT NULL
               GROUP BY wallet ORDER BY first_ts LIMIT ?""",
            (pool, EARLY_N)).fetchall()
        for wallet, first_ts in rows:
            ev = con.execute(
                "SELECT tx_hash, vol_usd, base_amount FROM trades WHERE pool=? AND wallet=?"
                " AND kind='buy' ORDER BY ts LIMIT 1", (pool, wallet)).fetchone()
            mins_after = None
            if created_at and first_ts:
                try:
                    import calendar
                    c = calendar.timegm(time.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S"))
                    f = calendar.timegm(time.strptime(first_ts[:19], "%Y-%m-%dT%H:%M:%S"))
                    mins_after = round((f - c) / 60, 1)
                except Exception:
                    pass
            wallet_hits.setdefault(wallet, []).append({
                "pool": pool, "name": name, "first_buy_ts": first_ts,
                "mins_after_launch": mins_after, "tx_hash": ev[0] if ev else None,
                "usd": ev[1] if ev else None, "runner_mcap": mcap, "runner_at": runner_at,
            })

    cands = {w: hits for w, hits in wallet_hits.items() if len(hits) >= MIN_RUNNERS}
    prev = {}
    if os.path.exists(OUT):
        prev = json.load(open(OUT)).get("candidates", {})
    new_wallets = [w for w in cands if w not in prev]
    payload = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "runners": len(runners), "candidates": cands}
    json.dump(payload, open(OUT, "w"), indent=1)
    print(f"runners={len(runners)} candidates={len(cands)} new={len(new_wallets)}")
    for w in new_wallets[:20]:
        coins = ",".join(h["name"].split(" / ")[0] for h in cands[w])
        print(f"NEW_CANDIDATE {w[:12]} on {len(cands[w])} runners: {coins}")
    con.close()


if __name__ == "__main__":
    main()
