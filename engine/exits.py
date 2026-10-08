#!/usr/bin/env python3
"""wallet-live/exits.py - EXITS tracker + JUDGE convergence alerts (every 15 min).

Reads judge_scores for 80+ ("top") wallets, then incrementally:
  1. EXITS: new sells by top wallets since the watermark -> judge_exits table.
     Cluster alert: >=cluster_min distinct top wallets selling the SAME pool
     within any 30-min window -> JUDGE_EXIT_CLUSTER <pool> (ping-worthy:
     smart money leaving). cluster_min = min(3, max(2, tier_size)).
  2. CONVERGENCE: buys in the last 30 min by top wallets, grouped by pool.
     >=converge_min distinct top wallets buying the same pool ->
     JUDGE_CONVERGENCE <pool> (ping-worthy: the tweet's trigger,
     quality-filtered). converge_min = min(4, max(2, tier_size)).

Dedup: judge_alerts(alert_type, pool, window_start) — no repeat of the same
type+pool within 24h. Watermark: exits_watermark.txt (ISO ts of the newest
sell SCANNED, top wallet or not) — advanced every run so each scan only
covers ~15 min of new sells instead of re-reading the whole tail back to
the last top-wallet sell.
Prints JUDGE_EXIT_CLUSTER / JUDGE_CONVERGENCE lines only for new alerts.
"""
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
WM = os.path.join(HERE, "exits_watermark.txt")
TOP_SCORE = 70
WINDOW = 30 * 60
DEDUP_H = 24
# Alert bars scale with tier size (floor 2): with a tiny elite tier, requiring
# 4 wallets would silence the layer until history accumulates more runners.
# CLUSTER_MIN/CONVERGE_MIN are computed per run in main().


def load_watermark():
    if os.path.exists(WM):
        try:
            return open(WM).read().strip()
        except Exception:
            pass
    return "2026-01-01T00:00:00Z"


def main():
    # timeout=120: the 5-min wallet-watch writer holds the lock briefly; wait it out
    con = sqlite3.connect(DB, timeout=120)
    con.execute("""CREATE TABLE IF NOT EXISTS judge_exits(
        wallet TEXT, pool TEXT, ts TEXT, vol_usd REAL, tx_hash TEXT,
        PRIMARY KEY (wallet, tx_hash))""")
    con.execute("""CREATE TABLE IF NOT EXISTS judge_alerts(
        alert_type TEXT, pool TEXT, window_start TEXT, created_at TEXT)""")

    top = {r[0] for r in con.execute(
        "SELECT wallet FROM judge_scores WHERE score>=?", (TOP_SCORE,))}
    if not top:
        print("exits: no top wallets scored yet (run judge.py first)")
        return
    cluster_min = min(3, max(2, len(top)))
    converge_min = min(4, max(2, len(top)))
    names = dict(con.execute("SELECT pool, name FROM pools"))

    wm = load_watermark()
    now_s = time.time()

    # ---- 1. new sells by top wallets ----
    # Watermark advances to the newest sell SCANNED (any wallet), not just
    # top-wallet sells — otherwise quiet stretches re-scan an ever-growing
    # tail of non-top sells every 15 min.
    new_exits = []
    max_ts = wm
    for w, pool, ts, vol, tx in con.execute(
            """SELECT wallet, pool, ts, vol_usd, tx_hash FROM trades
               WHERE kind='sell' AND ts>? AND wallet IS NOT NULL ORDER BY ts""", (wm,)):
        if ts and ts > max_ts:
            max_ts = ts
        if w in top:
            new_exits.append((w, pool, ts, vol or 0, tx))
    for e in new_exits:
        con.execute("INSERT OR IGNORE INTO judge_exits VALUES (?,?,?,?,?)", e)
    open(WM, "w").write(max_ts)

    # cluster: >=3 distinct top wallets, same pool, within 30 min
    by_pool = {}
    for w, pool, ts, vol, tx in new_exits:
        by_pool.setdefault(pool, []).append((w, ts, vol))
    for pool, evs in by_pool.items():
        evs.sort(key=lambda e: e[1])
        seen_wallets, start = [], None
        for w, ts, vol in evs:
            t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            if start is None or t - start > WINDOW:
                start, seen_wallets = t, []
            if w not in seen_wallets:
                seen_wallets.append(w)
            if len(seen_wallets) >= cluster_min:
                ws = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))
                dup = con.execute(
                    """SELECT 1 FROM judge_alerts WHERE alert_type='EXIT_CLUSTER'
                       AND pool=? AND created_at>?""",
                    (pool, time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(now_s - DEDUP_H * 3600)))).fetchone()
                if not dup:
                    con.execute(
                        "INSERT INTO judge_alerts VALUES (?,?,?,?)",
                        ("EXIT_CLUSTER", pool, ws,
                         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s))))
                    nm = names.get(pool, pool)
                    print(f"JUDGE_EXIT_CLUSTER {nm} pool={pool} "
                          f"wallets={len(seen_wallets)} window={ws}")
                break

    # ---- 2. convergence: top-wallet buys in last 30 min ----
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s - WINDOW))
    buys = {}
    for w, pool, ts in con.execute(
            """SELECT wallet, pool, ts FROM trades
               WHERE kind='buy' AND ts>? AND wallet IS NOT NULL""", (cutoff,)):
        if w in top:
            buys.setdefault(pool, set()).add(w)
    for pool, wallets in buys.items():
        if len(wallets) >= converge_min:
            dup = con.execute(
                """SELECT 1 FROM judge_alerts WHERE alert_type='CONVERGENCE'
                   AND pool=? AND created_at>?""",
                (pool, time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(now_s - DEDUP_H * 3600)))).fetchone()
            if not dup:
                con.execute(
                    "INSERT INTO judge_alerts VALUES (?,?,?,?)",
                    ("CONVERGENCE", pool, cutoff,
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s))))
                nm = names.get(pool, pool)
                print(f"JUDGE_CONVERGENCE {nm} pool={pool} "
                      f"top_wallets={len(wallets)}")
    con.commit()
    print(f"exits: top={len(top)} new_sells={len(new_exits)}")
    con.close()


if __name__ == "__main__":
    main()
