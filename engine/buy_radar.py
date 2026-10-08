#!/usr/bin/env python3
"""wallet-live/buy_radar.py - top-wallet BUY radar with velocity cross-check (every 15 min).

The copy-trading trigger done right: a 70+ JUDGE wallet buying is the
quality signal, but the wallet alone is not enough (4pF8 buys ~11/day, most
are $3-10 dust probes that go nowhere). This script pings only when a top
wallet's buy lands in a pool that is ALSO heating up on velocity — quality
x momentum, not quality alone.

Bars (inspector's judgment, looser than SPRINTER since the wallet is the
signal; documented, tunable):
  pool age < 6h, buyers30 >= 20, buy share >= 55%, buyvol30 >= $1500,
  avg buy >= $15, mcap <= $100K, liquidity >= $8K.

Dedup: judge_alerts (alert_type='TOP_BUY', 24h per pool). Watermark:
buy_radar_watermark.txt advances to the newest buy SCANNED every run
(same lesson as exits.py: never re-scan the tail).

Every ping is also written to signal_scorecard for the 24h grader.

Prints JUDGE_TOP_BUY lines only for new alerts. Silent otherwise.
"""
import os
import sqlite3
import time
import calendar
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
WM = os.path.join(HERE, "buy_radar_watermark.txt")
TOP_SCORE = 70
DEDUP_H = 24

MAX_AGE_H = 6
MIN_BUYERS_30M = 20
MIN_BUY_SHARE = 0.55
MIN_BUYVOL_USD = 1500
MIN_AVG_BUY_USD = 15
MAX_MCAP_USD = 100_000
MIN_LIQ_USD = 8_000
MIN_LIQ_MCAP_RATIO = 0.30  # pool must hold >=15% of token supply (liq/mcap = 2x pool supply share)
MAX_LIQ_MCAP_RATIO = 1.00  # kill excess-liquidity pools: >100% = pool holds >50% of supply (dead/dusted seed)


def load_watermark():
    if os.path.exists(WM):
        try:
            return open(WM).read().strip()
        except Exception:
            pass
    return "2026-01-01T00:00:00Z"


def ts_epoch(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0


def coordination_tag(con, pool, now_s, window_min=60):
    """Classify buy coordination on a pool over the trailing window.
    TIGHT = >=2 distinct wallets in the SAME SECOND with sizes within 2x
            (bot-ring signature: last night's ring did same-second ~$583 buys).
    LOOSE = >=2 distinct wallets in the same minute with sizes within 3x.
    NONE  = no synchronized multi-wallet buying.
    Tag, don't suppress: the same signature appears on real runners (SPCXx, AUTONOM)."""
    since = datetime.datetime.fromtimestamp(
        now_s - window_min * 60, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = con.execute(
        """SELECT wallet, vol_usd, substr(ts, 1, 19) AS sec FROM trades
           WHERE pool=? AND kind='buy' AND ts >= ?""", (pool, since)).fetchall()
    by_sec = {}
    for w, v, s in rows:
        by_sec.setdefault(s, []).append((w, float(v or 0)))
    tight = None
    for s, lst in by_sec.items():
        uniq = {w for w, _ in lst}
        if len(uniq) < 2:
            continue
        vols = [v for _, v in lst]
        lo, hi = min(vols), max(vols)
        if hi <= max(lo * 2, 1.0) and (tight is None or len(uniq) > tight[1]):
            tight = (s, len(uniq), lo, hi)
    if tight:
        s, n, lo, hi = tight
        return f"coord=TIGHT:{n}w_${lo:.0f}-${hi:.0f}@{s[11:]}"
    by_min = {}
    for w, v, s in rows:
        by_min.setdefault(s[:16], []).append((w, float(v or 0)))
    for m, lst in by_min.items():
        uniq = {w for w, _ in lst}
        if len(uniq) < 2:
            continue
        vols = [v for _, v in lst]
        lo, hi = min(vols), max(vols)
        if hi <= max(lo * 3, 1.0):
            return f"coord=LOOSE:{len(uniq)}w_${lo:.0f}-${hi:.0f}@{m[11:]}"
    return "coord=NONE"

def log_alert_tag(con, alert_type, pool, sig_ts, ctag):
    """Persist the coordination tag for an alert: JUDGE.md spec says tags go
    on the printed alert line AND into the alert_tags table."""
    con.execute("""CREATE TABLE IF NOT EXISTS alert_tags(
        alert_type TEXT, pool TEXT, signal_ts TEXT, tag TEXT)""")
    con.execute("INSERT INTO alert_tags VALUES (?,?,?,?)",
                (alert_type, pool, sig_ts, ctag))


def main():
    con = sqlite3.connect(DB, timeout=120)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS signal_scorecard(
        signal_type TEXT, pool TEXT, name TEXT, signal_ts TEXT, wallet TEXT,
        mcap_at_signal REAL, buyvol30_usd REAL, buyers30 INTEGER,
        mcap_24h REAL, multiple_24h REAL, graded_at TEXT,
        PRIMARY KEY (signal_type, pool, signal_ts))""")

    top = {r[0] for r in con.execute(
        "SELECT wallet FROM judge_scores WHERE score>=?", (TOP_SCORE,))}
    if not top:
        print("buy_radar: no top wallets scored yet")
        return
    names = dict(con.execute("SELECT pool, name FROM pools"))
    meta = {r["pool"]: dict(r) for r in con.execute(
        "SELECT pool, mcap_usd, liquidity_usd, first_seen, created_at FROM pools")}
    now_s = time.time()

    wm = load_watermark()
    # new buys by top wallets since watermark; watermark advances on scan, not on hits
    bought_pools = {}  # pool -> list of (wallet, ts, vol)
    max_ts = wm
    for w, pool, ts, vol in con.execute(
            """SELECT wallet, pool, ts, vol_usd FROM trades
               WHERE kind='buy' AND ts>? AND wallet IS NOT NULL ORDER BY ts""", (wm,)):
        if ts and ts > max_ts:
            max_ts = ts
        if w in top:
            bought_pools.setdefault(pool, []).append((w, ts, vol or 0))
    open(WM, "w").write(max_ts)

    if not bought_pools:
        print(f"buy_radar: top={len(top)} new_top_buys=0")
        return

    # 30-min velocity per bought pool (same math as fresh_velocity.py)
    vel = {}
    for r in con.execute("""
            SELECT t.pool,
              COUNT(DISTINCT CASE WHEN t.ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')
                                    AND t.kind='buy' THEN t.wallet END) AS buyers30,
              SUM(CASE WHEN t.ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')
                        AND t.kind='buy' THEN 1 ELSE 0 END) AS buys30,
              SUM(CASE WHEN t.ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')
                       THEN 1 ELSE 0 END) AS txns30,
              SUM(CASE WHEN t.ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')
                        AND t.kind='buy' THEN t.vol_usd ELSE 0 END) AS buyvol30
            FROM trades t WHERE t.pool IN ({})
            GROUP BY t.pool""".format(",".join("?" * len(bought_pools))),
            tuple(bought_pools)):
        vel[r["pool"]] = r

    fired = 0
    for pool, buys in bought_pools.items():
        v = vel.get(pool)
        if not v:
            continue
        buyers30 = v["buyers30"] or 0
        buys30 = v["buys30"] or 0
        txns30 = v["txns30"] or 0
        buyvol = v["buyvol30"] or 0
        share = (buys30 / txns30) if txns30 else 0
        avg_buy = (buyvol / buys30) if buys30 else 0
        m = meta.get(pool, {})
        mcap = m.get("mcap_usd") or 0
        liq = m.get("liquidity_usd") or 0
        age_h = None
        for s in (m.get("first_seen"), m.get("created_at")):
            e = ts_epoch(s) if s else 0
            if e:
                age_h = (now_s - e) / 3600
                break
        if not (buyers30 >= MIN_BUYERS_30M and share >= MIN_BUY_SHARE
                and buyvol >= MIN_BUYVOL_USD and avg_buy >= MIN_AVG_BUY_USD
                and mcap <= MAX_MCAP_USD and liq >= MIN_LIQ_USD
                and mcap > 0 and liq / mcap >= MIN_LIQ_MCAP_RATIO
                and liq / mcap <= MAX_LIQ_MCAP_RATIO
                and age_h is not None and age_h < MAX_AGE_H):
            continue
        dup = con.execute(
            """SELECT 1 FROM judge_alerts WHERE alert_type='TOP_BUY'
               AND pool=? AND created_at>?""",
            (pool, time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                 time.gmtime(now_s - DEDUP_H * 3600)))).fetchone()
        if dup:
            continue
        sig_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s))
        con.execute("INSERT INTO judge_alerts VALUES (?,?,?,?)",
                    ("TOP_BUY", pool, sig_ts, sig_ts))
        nm = names.get(pool, pool)
        con.execute("""INSERT OR IGNORE INTO signal_scorecard
            (signal_type, pool, name, signal_ts, wallet, mcap_at_signal,
             buyvol30_usd, buyers30) VALUES (?,?,?,?,?,?,?,?)""",
            ("TOP_BUY", pool, nm, sig_ts, ",".join(sorted({b[0][:8] for b in buys})),
             mcap, buyvol, buyers30))
        wallets = ",".join(sorted({b[0][:8] for b in buys}))
        ctag = coordination_tag(con, pool, now_s)
        log_alert_tag(con, "TOP_BUY", pool, sig_ts, ctag)
        print(f"JUDGE_TOP_BUY {nm} pool={pool} wallets={wallets} "
              f"buyers30={buyers30} share={share:.0%} buyvol=${buyvol:,.0f} "
              f"mcap=${mcap:,.0f} {ctag}")
        fired += 1
    con.commit()
    print(f"buy_radar: top={len(top)} pools_with_new_buys={len(bought_pools)} fired={fired}")
    con.close()


if __name__ == "__main__":
    main()
