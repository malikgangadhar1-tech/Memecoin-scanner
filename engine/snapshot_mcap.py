#!/usr/bin/env python3
"""Snapshot current pool mcaps into pool_mcap_history for the recovery-rank monitor.
Silent plumbing cron (every 15 min). Never pings.
"""
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")

def main():
    con = sqlite3.connect(DB, timeout=300)
    con.execute("""CREATE TABLE IF NOT EXISTS pool_mcap_history
                   (pool TEXT, ts TEXT, mcap_usd REAL, liquidity_usd REAL)""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_pmch_pool_ts
                   ON pool_mcap_history(pool, ts)""")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = con.execute(
        """SELECT pool, mcap_usd, liquidity_usd FROM pools
           WHERE watching = 1 AND mcap_usd > 0""").fetchall()
    con.executemany("INSERT INTO pool_mcap_history VALUES (?,?,?,?)",
                    [(p, now, m, l) for p, m, l in rows])
    con.commit()
    # prune older than 48h
    con.execute("DELETE FROM pool_mcap_history WHERE ts < datetime('now', '-48 hours')")
    con.commit()
    print(f"snapshot_mcap: snapshotted {len(rows)} pools at {now}")
    con.close()

if __name__ == "__main__":
    main()
