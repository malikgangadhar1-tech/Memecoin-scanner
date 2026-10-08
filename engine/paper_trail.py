#!/usr/bin/env python3
"""
Paper-trail tracker for scout callout credibility.

Reads scout_reports/*.json, logs every ranked pool (score >= 7) with its
entry mcap at scout time, then tracks peak mcap at +24h and +7d from
pool_mcap_history. Builds a verifiable win-rate / multiple ledger Rohit
can point to when starting a Pumpfun caller account.

Output: paper_trail.csv (one row per scout pick, updated in place)
        paper_trail_summary.txt (win rate, avg multiple, etc.)

Run daily via cron. Idempotent — re-running updates outcomes for
existing rows and appends new picks.
"""

import csv
import glob
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "watch.db")
REPORTS = os.path.join(BASE, "scout_reports")
CSV_PATH = os.path.join(BASE, "paper_trail.csv")
SUMMARY_PATH = os.path.join(BASE, "paper_trail_summary.txt")

MIN_SCORE = 7  # track 7+ (8+ = "hit", 7 = borderline for more data)


def parse_report_time(fname):
    # "2026-10-06T15-44.json" -> datetime UTC
    stem = os.path.basename(fname).replace(".json", "")
    try:
        return datetime.strptime(stem, "%Y-%m-%dT%H-%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    con = sqlite3.connect(DB, timeout=120)
    con.row_factory = sqlite3.Row

    # Load existing ledger
    existing = {}
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH) as f:
            for row in csv.DictReader(f):
                existing[row["pool"]] = row

    # Scan reports
    for rp in sorted(glob.glob(os.path.join(REPORTS, "*.json"))):
        rt = parse_report_time(rp)
        if not rt:
            continue
        try:
            with open(rp) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        ranked = data.get("report", {}).get("ranked", [])
        for p in ranked:
            score = p.get("score", 0)
            if score < MIN_SCORE:
                continue
            pool = p.get("pool")
            if not pool or pool in existing:
                continue
            # Entry mcap: closest history snapshot at/before report time,
            # fallback to pools.mcap_usd
            r = con.execute(
                "SELECT mcap_usd FROM pool_mcap_history WHERE pool=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1", (pool, iso(rt))).fetchone()
            entry_mcap = r["mcap_usd"] if r and r["mcap_usd"] else None
            if not entry_mcap:
                r2 = con.execute(
                    "SELECT mcap_usd FROM pools WHERE pool=?", (pool,)).fetchone()
                entry_mcap = r2["mcap_usd"] if r2 else None
            existing[pool] = {
                "pool": pool,
                "name": p.get("name", ""),
                "score": score,
                "verdict": p.get("verdict", ""),
                "scout_time": iso(rt),
                "entry_mcap": entry_mcap or "",
                "peak_24h": "",
                "peak_7d": "",
                "mult_24h": "",
                "mult_7d": "",
                "reasons": "; ".join(p.get("reasons", [])),
            }

    # Update outcomes for all rows
    now = datetime.now(timezone.utc)
    for pool, row in existing.items():
        try:
            st = datetime.strptime(row["scout_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        entry = float(row["entry_mcap"]) if row["entry_mcap"] else 0

        # 24h peak (only if 24h has passed)
        if now >= st + timedelta(hours=24) and not row["peak_24h"]:
            r = con.execute(
                "SELECT MAX(mcap_usd) AS pk FROM pool_mcap_history "
                "WHERE pool=? AND ts>? AND ts<=?",
                (pool, iso(st), iso(st + timedelta(hours=24)))).fetchone()
            pk = r["pk"] if r and r["pk"] else 0
            if pk and entry:
                row["peak_24h"] = round(pk, 2)
                row["mult_24h"] = round(pk / entry, 2)

        # 7d peak (only if 7d has passed)
        if now >= st + timedelta(days=7) and not row["peak_7d"]:
            r = con.execute(
                "SELECT MAX(mcap_usd) AS pk FROM pool_mcap_history "
                "WHERE pool=? AND ts>? AND ts<=?",
                (pool, iso(st), iso(st + timedelta(days=7)))).fetchone()
            pk = r["pk"] if r and r["pk"] else 0
            if pk and entry:
                row["peak_7d"] = round(pk, 2)
                row["mult_7d"] = round(pk / entry, 2)

    # Write CSV
    fields = ["pool", "name", "score", "verdict", "scout_time", "entry_mcap",
              "peak_24h", "peak_7d", "mult_24h", "mult_7d", "reasons"]
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for pool in sorted(existing, key=lambda p: existing[p]["scout_time"]):
            w.writerow({k: existing[pool].get(k, "") for k in fields})

    # Summary
    rows = list(existing.values())
    m24 = [float(r["mult_24h"]) for r in rows if r["mult_24h"]]
    m7 = [float(r["mult_7d"]) for r in rows if r["mult_7d"]]
    hits = [r for r in rows if int(r["score"]) >= 8]

    def stats(ms):
        if not ms:
            return "n/a"
        wins = sum(1 for m in ms if m >= 2.0)
        return (f"n={len(ms)} avg={sum(ms)/len(ms):.2f}x "
                f"median={sorted(ms)[len(ms)//2]:.2f}x "
                f"win(>=2x)={wins}/{len(ms)}")

    with open(SUMMARY_PATH, "w") as f:
        f.write(f"Paper-trail summary (updated {iso(now)})\n")
        f.write(f"Total picks tracked (score>=7): {len(rows)}\n")
        f.write(f"Hits (score>=8): {len(hits)}\n")
        f.write(f"24h multiples: {stats(m24)}\n")
        f.write(f"7d multiples: {stats(m7)}\n")
        if m24:
            best = max(rows, key=lambda r: float(r["mult_24h"] or 0))
            f.write(f"Best 24h: {best['name']} {best['mult_24h']}x "
                    f"(entry ${float(best['entry_mcap']):,.0f})\n")

    print(f"ok picks={len(rows)} with_24h={len(m24)} with_7d={len(m7)}")
    con.close()


if __name__ == "__main__":
    main()
