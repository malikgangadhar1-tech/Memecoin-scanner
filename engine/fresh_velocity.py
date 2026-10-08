#!/usr/bin/env python3
"""fresh_velocity.py - SPRINTER detection: catch JANE-like buyer velocity in hour 1.

Reads watch.db (fed by the wallet-watch cron, zero extra API calls) and flags
pools < 4h old showing real buyer breadth arriving fast:
  - distinct buyers in trailing 30 min >= 40
  - buy share of txns >= 55%
  - accelerating vs the prior 30 min (1.3x), or prior window was quiet

This is the speed layer the $200K RUNNER bar doesn't have: it fires on
velocity, not confirmation. A SPRINTER flag means "go run the contract +
narrative checklist NOW", not "buy".

Output: SPRINTER lines (only for pools with >= $5K 30-min buy volume,
>= $20 average buy size, mcap <= $50K, liquidity >= $10K — sub-bar flags
are logged, not printed); appended to sprinter_log.csv (deduped per pool
per 60 min on pinged lines). Silent-ish otherwise.
"""
import os, sqlite3, csv, sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
LOG = os.path.join(HERE, "sprinter_log.csv")

MAX_AGE_H = 4
MIN_BUYERS_30M = 40
MIN_BUY_SHARE = 0.55
ACCEL = 1.3
DEDUP_MIN = 60
# Ping floor (user, 2026-10-02): SPRINTER lines are printed (chat ping) only
# when BOTH hold — keeps dust/bot-swarm noise off his phone while the CSV
# stays complete for research. Sub-floor flags are logged with pinged=0 and
# can still ping later if they heat up past both bars.
MIN_PING_BUYVOL_USD = 5000
MIN_PING_AVG_BUY_USD = 20  # "only good ones" — avg buy size kills $2-dust swarms
MAX_PING_MCAP_USD = 50000  # user 2026-10-02: only early entries, no $200K runners
MIN_PING_LIQ_USD = 10000   # user 2026-10-02: thin pools are unexitable trash


def main():
    try:
        con = sqlite3.connect(DB, timeout=30)
    except Exception as e:
        print(f"db open failed ({e}); will retry next run")
        return
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("""
            SELECT t.pool,
              COUNT(DISTINCT CASE WHEN t.ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')
                                    AND t.kind='buy' THEN t.wallet END) AS buyers30,
              SUM(CASE WHEN t.ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')
                        AND t.kind='buy' THEN 1 ELSE 0 END) AS buys30,
              SUM(CASE WHEN t.ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')
                       THEN 1 ELSE 0 END) AS txns30,
              SUM(CASE WHEN t.ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')
                        AND t.kind='buy' THEN t.vol_usd ELSE 0 END) AS buyvol30,
              COUNT(DISTINCT CASE WHEN t.ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-60 minutes')
                                    AND t.ts < strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')
                                    AND t.kind='buy' THEN t.wallet END) AS buyers_prior,
              p.name, p.base_mint, p.first_seen, p.mcap_usd, p.liquidity_usd, p.dex
            FROM trades t JOIN pools p ON p.pool = t.pool
            WHERE p.first_seen >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-4 hours')
              AND COALESCE(p.dead,0) = 0
              AND t.ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-60 minutes')
            GROUP BY t.pool
        """).fetchall()
    except sqlite3.OperationalError as e:
        print(f"db busy ({e}); will retry next run")
        return

    # dedup: pools pinged in last DEDUP_MIN (only pinged ones count, so a
    # pool that first flags below the ping floor can still ping later when
    # its volume crosses the floor)
    seen_recent = set()
    if os.path.exists(LOG):
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(minutes=DEDUP_MIN)).strftime("%Y-%m-%dT%H:%M:%S")
        with open(LOG) as f:
            for row in csv.DictReader(f):
                if row["ts"] >= cutoff and row.get("pinged", "1") == "1":
                    seen_recent.add(row["pool"])

    flagged = []
    for r in rows:
        buyers30 = r["buyers30"] or 0
        txns30 = r["txns30"] or 0
        buys30 = r["buys30"] or 0
        prior = r["buyers_prior"] or 0
        if buyers30 < MIN_BUYERS_30M:
            continue
        share = (buys30 / txns30) if txns30 else 0
        if share < MIN_BUY_SHARE:
            continue
        if not (prior < 10 or buyers30 >= ACCEL * prior):
            continue
        if r["pool"] in seen_recent:
            continue
        flagged.append(r)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if flagged:
        new_file = not os.path.exists(LOG)
        with open(LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["ts", "pool", "name", "base_mint", "dex",
                            "buyers30", "buy_share", "buyers_prior",
                            "buyvol30_usd", "mcap_usd", "first_seen", "pinged"])
            for r in sorted(flagged, key=lambda x: -(x["buyvol30"] or 0)):
                buys30 = r["buys30"] or 0
                txns30 = r["txns30"] or 0
                share = buys30 / txns30 if txns30 else 0
                buyvol = r["buyvol30"] or 0
                avg_buy = (buyvol / buys30) if buys30 else 0
                mcap = r["mcap_usd"] or 0
                liq = r["liquidity_usd"] or 0
                # mcap=0/NULL (Gecko first-sighting gap) can't prove it's under
                # the ceiling, so it waits; liq=0/NULL can't prove exitability.
                pinged = 1 if (buyvol >= MIN_PING_BUYVOL_USD
                               and avg_buy >= MIN_PING_AVG_BUY_USD
                               and 0 < mcap <= MAX_PING_MCAP_USD
                               and liq >= MIN_PING_LIQ_USD) else 0
                w.writerow([now, r["pool"], r["name"], r["base_mint"], r["dex"],
                            r["buyers30"], f"{share:.2f}", r["buyers_prior"],
                            f"{buyvol:.0f}", r["mcap_usd"],
                            r["first_seen"], pinged])
                if not pinged:
                    continue
                print(f"SPRINTER buyers30={r['buyers30']} share={share:.0%} "
                      f"prior30={r['buyers_prior']} buyvol=${buyvol:,.0f} "
                      f"mcap=${r['mcap_usd'] or 0:,.0f} liq=${liq:,.0f} :: {r['name']} "
                      f"pool={r['pool']} dex={r['dex']}")
        n_pinged = sum(1 for r in flagged
                       if (r["buyvol30"] or 0) >= MIN_PING_BUYVOL_USD
                       and ((r["buyvol30"] or 0) / (r["buys30"] or 1)) >= MIN_PING_AVG_BUY_USD
                       and 0 < (r["mcap_usd"] or 0) <= MAX_PING_MCAP_USD
                       and (r["liquidity_usd"] or 0) >= MIN_PING_LIQ_USD)
        if not n_pinged:
            print(f"ok: {len(flagged)} sprinters all below ping bars "
                  f"(${MIN_PING_BUYVOL_USD:,} vol / ${MIN_PING_AVG_BUY_USD} avg buy / "
                  f"${MAX_PING_MCAP_USD:,} max mcap / ${MIN_PING_LIQ_USD:,} min liq)")
    else:
        print(f"ok: {len(rows)} young pools scored, no sprinters")


if __name__ == "__main__":
    main()
