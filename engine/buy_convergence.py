#!/usr/bin/env python3
"""wallet-live/buy_convergence.py - multi-wallet BUY convergence alerts (every 15 min).

The "multiple good wallets ape a coin" notifier. exits.py's CONVERGENCE leg
needs 2+ distinct 70+ wallets in 30 min, but the 70+ tier currently holds a
single wallet (4pF8) — so it can never fire. This script widens the tier to
GOOD_SCORE=65 ("good wallets", ~17 at last score) and the window to 60 min:
>=2 distinct good wallets buying the SAME pool within 60 min fires.

Bars (light — the convergence of two good wallets IS the signal):
  pool age < 12h, mcap <= $200K, liquidity >= $5K.
A single good wallet buying alone does not fire (that's buy_radar.py's job).

Dedup: judge_alerts (alert_type='BUY_CONVERGENCE', 24h per pool). Watermark:
buy_conv_watermark.txt advances to the newest buy SCANNED every run
(same lesson as exits.py: never re-scan the tail).

Every ping is also written to signal_scorecard for the 24h grader.

Prints JUDGE_BUY_CONVERGENCE lines only for new alerts. Silent otherwise.
"""
import os
import sqlite3
import time
import calendar
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
WM = os.path.join(HERE, "buy_conv_watermark.txt")
GOOD_SCORE = 65
MIN_WALLETS = 2
WINDOW_MIN = 60
DEDUP_H = 24

MAX_AGE_H = 12
MAX_MCAP_USD = 200_000
MIN_LIQ_USD = 5_000
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

    good = {r[0] for r in con.execute(
        "SELECT wallet FROM judge_scores WHERE score>=?", (GOOD_SCORE,))}
    if not good:
        print("buy_convergence: no good wallets scored yet")
        return
    names = dict(con.execute("SELECT pool, name FROM pools"))
    meta = {r["pool"]: dict(r) for r in con.execute(
        "SELECT pool, mcap_usd, liquidity_usd, first_seen, created_at FROM pools")}
    now_s = time.time()

    wm = load_watermark()
    # new buys by good wallets since watermark; watermark advances on scan, not on hits
    bought = {}  # pool -> {wallet: (ts, vol)}
    max_ts = wm
    for w, pool, ts, vol in con.execute(
            """SELECT wallet, pool, ts, vol_usd FROM trades
               WHERE kind='buy' AND ts>? AND wallet IS NOT NULL ORDER BY ts""", (wm,)):
        if ts and ts > max_ts:
            max_ts = ts
        if w in good:
            d = bought.setdefault(pool, {})
            # keep earliest buy per wallet per pool
            if w not in d or (ts and ts < d[w][0]):
                d[w] = (ts, vol or 0)
    open(WM, "w").write(max_ts)

    if not bought:
        print(f"buy_convergence: good={len(good)} new_good_buys=0")
        return

    cutoff = now_s - WINDOW_MIN * 60
    fired = 0
    for pool, wd in bought.items():
        # distinct good wallets whose (earliest) buy is inside the window
        in_window = {w: tv for w, tv in wd.items() if ts_epoch(tv[0]) >= cutoff}
        if len(in_window) < MIN_WALLETS:
            continue
        m = meta.get(pool, {})
        mcap = m.get("mcap_usd") or 0
        liq = m.get("liquidity_usd") or 0
        age_h = None
        for s in (m.get("first_seen"), m.get("created_at")):
            e = ts_epoch(s) if s else 0
            if e:
                age_h = (now_s - e) / 3600
                break
        if not (mcap <= MAX_MCAP_USD and liq >= MIN_LIQ_USD
                and mcap > 0 and liq / mcap >= MIN_LIQ_MCAP_RATIO
                and liq / mcap <= MAX_LIQ_MCAP_RATIO
                and age_h is not None and age_h < MAX_AGE_H):
            continue
        dup = con.execute(
            """SELECT 1 FROM judge_alerts WHERE alert_type='BUY_CONVERGENCE'
               AND pool=? AND created_at>?""",
            (pool, time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                 time.gmtime(now_s - DEDUP_H * 3600)))).fetchone()
        if dup:
            continue
        sig_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_s))
        con.execute("INSERT INTO judge_alerts VALUES (?,?,?,?)",
                    ("BUY_CONVERGENCE", pool, sig_ts, sig_ts))
        nm = names.get(pool, pool)
        wlist = ",".join(sorted(w[:8] for w in in_window))
        vols = sorted(tv[1] for tv in in_window.values())
        con.execute("""INSERT OR IGNORE INTO signal_scorecard
            (signal_type, pool, name, signal_ts, wallet, mcap_at_signal,
             buyvol30_usd, buyers30) VALUES (?,?,?,?,?,?,?,?)""",
            ("BUY_CONVERGENCE", pool, nm, sig_ts, wlist, mcap,
             sum(tv[1] for tv in in_window.values()), len(in_window)))
        ctag = coordination_tag(con, pool, now_s)
        log_alert_tag(con, "BUY_CONVERGENCE", pool, sig_ts, ctag)
        print(f"JUDGE_BUY_CONVERGENCE {nm} pool={pool} "
              f"good_wallets={len(in_window)} wallets={wlist} "
              f"buyvol=${sum(tv[1] for tv in in_window.values()):,.0f} "
              f"mcap=${mcap:,.0f} {ctag}")
        fired += 1
    con.commit()
    print(f"buy_convergence: good={len(good)} pools_with_new_buys={len(bought)} fired={fired}")
    con.close()


if __name__ == "__main__":
    main()
