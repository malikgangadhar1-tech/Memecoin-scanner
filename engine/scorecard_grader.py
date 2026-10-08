#!/usr/bin/env python3
"""wallet-live/scorecard_grader.py - grade signal_scorecard rows at +24h (daily).

For every ungraded signal older than 24h: multiple_24h = current mcap_usd /
mcap_at_signal. pools rows are never deleted (dead=1 flag), so even rugged
pools keep their last mcap for grading — no survivorship bias from drops.

TIMING HONESTY: the grader runs once daily, so a signal is graded at the
first run AFTER signal+24h — the actual horizon lands anywhere in [24h, 48h).
graded_horizon_h records the true elapsed hours at grading time, so the
ledger never pretends a 40h multiple is a 24h multiple. multiple_24h is the
multiple at the recorded horizon.

Prints a running summary: n graded, median multiple, % >=2x, % >=5x.
Silent-ish; the ledger compounds over weeks into the hit-rate record.
"""
import os
import sqlite3
import time
from datetime import datetime, timezone
from statistics import median

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
GRADE_AFTER_H = 24


def main():
    con = sqlite3.connect(DB, timeout=120)
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - GRADE_AFTER_H * 3600))
    now_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    todo = con.execute(
        """SELECT signal_type, pool, signal_ts, mcap_at_signal FROM signal_scorecard
           WHERE graded_at IS NULL AND signal_ts < ? AND mcap_at_signal > 0""",
        (cutoff,)).fetchall()
    n = 0
    for stype, pool, sts, mcap0 in todo:
        r = con.execute("SELECT mcap_usd FROM pools WHERE pool=?", (pool,)).fetchone()
        if not r or not r[0]:
            continue
        mult = r[0] / mcap0
        sig_dt = datetime.strptime(sts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        horizon_h = (datetime.now(timezone.utc) - sig_dt).total_seconds() / 3600.0
        con.execute("""UPDATE signal_scorecard SET mcap_24h=?, multiple_24h=?,
                       graded_at=?, graded_horizon_h=? WHERE signal_type=? AND pool=? AND signal_ts=?""",
                    (r[0], mult, now_s, round(horizon_h, 1), stype, pool, sts))
        n += 1
    con.commit()
    mults = [r[0] for r in con.execute(
        "SELECT multiple_24h FROM signal_scorecard WHERE multiple_24h IS NOT NULL")]
    if mults:
        print(f"scorecard: graded_this_run={n} total={len(mults)} "
              f"median_x={median(mults):.2f} "
              f"hit2x={sum(1 for m in mults if m >= 2) / len(mults):.0%} "
              f"hit5x={sum(1 for m in mults if m >= 5) / len(mults):.0%}")
    else:
        print(f"scorecard: graded_this_run={n} total=0 (ledger building)")
    con.close()


if __name__ == "__main__":
    main()
