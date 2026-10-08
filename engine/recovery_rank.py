#!/usr/bin/env python3
"""Recovery-rank monitor for memecoins — v1 (2026-10-03).

Idea (Rohit's): after a dump, the coins that recover fastest show relative
strength. For each tracked pool over the trailing 24h:
  high = max mcap, low = min mcap after high, current = latest mcap
  drop%    = (low - high) / high        (must be <= -25%: a real dump)
  retrace% = (current - low) / (high - low)  (must be >= 20%: recovering)
Rank by retrace% desc. Pre-registered thresholds; tuned only by forward data.

Prints RECOVERY_RANK lines for the top 10. Logs every qualifying pool to
recovery_rank_log.csv for later grading (did strong recoveries keep running?).
"""
import os
import sqlite3
import time
import csv

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
LOG = os.path.join(HERE, "recovery_rank_log.csv")

LOOKBACK_H = 24
MIN_SNAPSHOTS = 8
MIN_DROP_PCT = -25.0
MIN_RETRACE_PCT = 20.0
MIN_AGE_AT_HIGH_H = 3
MIN_MCAP_USD = 10_000
TOP_N = 10

def main():
    con = sqlite3.connect(DB, timeout=120)
    con.row_factory = sqlite3.Row
    try:
        has = con.execute(
            "SELECT COUNT(*) FROM pool_mcap_history WHERE ts > datetime('now', '-25 hours')").fetchone()[0]
    except sqlite3.OperationalError:
        print("RECOVERY_RANK warming up (no snapshot history yet)")
        return
    if has == 0:
        print("RECOVERY_RANK warming up (no snapshot history yet)")
        return

    pools = con.execute(
        """SELECT DISTINCT pool FROM pool_mcap_history
           WHERE ts > datetime('now', '-25 hours')""").fetchall()
    meta = {r["pool"]: r for r in con.execute(
        "SELECT pool, name, first_seen FROM pools")}
    scored = []
    for (pool,) in pools:
        snaps = con.execute(
            """SELECT ts, mcap_usd, liquidity_usd FROM pool_mcap_history
               WHERE pool=? AND ts > datetime('now', '-25 hours')
               ORDER BY ts""", (pool,)).fetchall()
        if len(snaps) < MIN_SNAPSHOTS:
            continue
        mcaps = [s["mcap_usd"] for s in snaps if s["mcap_usd"] and s["mcap_usd"] > 0]
        if len(mcaps) < MIN_SNAPSHOTS:
            continue
        hi = max(mcaps)
        i_hi = mcaps.index(hi)
        # pool age at high must be >= 3h (skip birth spikes)
        m = meta.get(pool)
        if m and m["first_seen"]:
            try:
                import calendar
                t_fs = calendar.timegm(time.strptime(snaps[i_hi]["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
                t0 = calendar.timegm(time.strptime(m["first_seen"][:19], "%Y-%m-%dT%H:%M:%S"))
                if (t_fs - t0) / 3600 < MIN_AGE_AT_HIGH_H:
                    continue
            except Exception:
                pass
        tail = mcaps[i_hi:]
        lo = min(tail)
        cur = mcaps[-1]
        if hi <= 0 or cur < MIN_MCAP_USD:
            continue
        drop = (lo - hi) / hi * 100
        if drop > MIN_DROP_PCT:
            continue
        retrace = (cur - lo) / (hi - lo) * 100 if hi != lo else 0
        if retrace < MIN_RETRACE_PCT:
            continue
        liq = snaps[-1]["liquidity_usd"] or 0
        ratio = liq / cur * 100 if cur else 0
        scored.append({
            "pool": pool,
            "name": (m["name"] if m and m["name"] else pool[:8]),
            "drop": drop, "retrace": retrace, "mcap": cur, "ratio": ratio,
        })

    scored.sort(key=lambda x: -x["retrace"])
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "pool", "name", "retrace_pct", "drop_pct", "mcap_usd"])
        for s in scored:
            w.writerow([now, s["pool"], s["name"], round(s["retrace"], 1),
                        round(s["drop"], 1), round(s["mcap"], 0)])
    if not scored:
        print("RECOVERY_RANK none qualifying")
    for s in scored[:TOP_N]:
        print(f"RECOVERY_RANK {s['name']} retrace={s['retrace']:.0f}% "
              f"drop={s['drop']:.0f}% mcap=${s['mcap']:,.0f} "
              f"liq_ratio={s['ratio']:.0f}% pool={s['pool']}")
    con.close()

if __name__ == "__main__":
    main()
