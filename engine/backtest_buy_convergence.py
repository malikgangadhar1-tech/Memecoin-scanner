#!/usr/bin/env python3
"""Backtest the BUY_CONVERGENCE signal on watch.db history.

Signal: >=2 distinct 65+ wallets buy the same pool within 60 min.
Outcome: pool.runner flag (confirmed runner: mcap >=$200K across checks
>=10min apart, >=30 distinct buy wallets) vs the base runner rate.

CAVEAT (in-sample): judge_scores were computed on this same history, so
good wallets were partly selected FOR hitting these runners. This measures
whether convergence adds selection beyond the individual scores, not a
true out-of-sample edge. Treat as a sanity check, not a validated hit rate.
"""
import os
import sqlite3
import calendar
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
GOOD_SCORE = 65
MIN_WALLETS = 2
WINDOW_S = 60 * 60


def ts_epoch(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0


def main():
    con = sqlite3.connect(DB, timeout=120)
    good = {r[0] for r in con.execute(
        "SELECT wallet FROM judge_scores WHERE score>=?", (GOOD_SCORE,))}
    print(f"good wallets: {len(good)}")

    # single streaming pass: first buy ts per (pool, wallet) for good wallets
    first_buy = {}
    n = 0
    for pool, wallet, ts in con.execute(
            """SELECT pool, wallet, MIN(ts) FROM trades
               WHERE kind='buy' AND wallet IS NOT NULL
               GROUP BY pool, wallet"""):
        n += 1
        if wallet in good and ts:
            e = ts_epoch(ts)
            if e:
                first_buy[(pool, wallet)] = e
    print(f"pool-wallet pairs scanned: {n}, good-wallet pairs: {len(first_buy)}")

    by_pool = defaultdict(list)
    for (pool, wallet), e in first_buy.items():
        by_pool[pool].append(e)

    conv_pools = []
    for pool, times in by_pool.items():
        if len(times) < MIN_WALLETS:
            continue
        times.sort()
        # sliding window: any two within WINDOW_S
        for i in range(len(times)):
            for j in range(i + 1, len(times)):
                if times[j] - times[i] <= WINDOW_S:
                    conv_pools.append(pool)
                    break
            else:
                continue
            break
    print(f"pools with >={MIN_WALLETS} good wallets in {WINDOW_S//60}min: {len(conv_pools)}")

    meta = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
        "SELECT pool, name, runner, mcap_usd FROM pools")}
    total_pools = con.execute("SELECT COUNT(*) FROM pools").fetchone()[0]
    total_runners = con.execute(
        "SELECT COUNT(*) FROM pools WHERE runner=1").fetchone()[0]
    print(f"base: {total_runners} runners / {total_pools} pools "
          f"= {total_runners/max(total_pools,1):.3%}")

    hits = 0
    rows = []
    for pool in conv_pools:
        name, runner, mcap = meta.get(pool, (pool, 0, 0))
        if runner:
            hits += 1
        rows.append((name, pool, runner, mcap or 0))
    print(f"convergence pools that became runners: {hits}/{len(conv_pools)} "
          f"= {hits/max(len(conv_pools),1):.1%}")
    rows.sort(key=lambda r: -(r[3] or 0))
    print("\nTop convergence pools by current mcap:")
    for name, pool, runner, mcap in rows[:15]:
        print(f"  {'RUNNER' if runner else '      '} {name} pool={pool} mcap=${mcap:,.0f}")
    con.close()


if __name__ == "__main__":
    main()
