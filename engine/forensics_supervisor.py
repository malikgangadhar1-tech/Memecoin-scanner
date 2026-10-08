#!/usr/bin/env python3
"""wallet-live/forensics_supervisor.py - 24/7 pipeline watchdog.

Runs hourly via cron. Advancement logic:
1. Count tier-1 classifications done (forensics_classifications/*.json).
2. If tier 1 complete and no QC report: tally verdicts, cross-check
   INDEPENDENT-TRADER verdicts vs judge_scores, write forensics_qc_report.json,
   print the hits (this is the surface-worthy event).
3. If QC done and tier 2 not yet built: build tier-2 candidate list
   (profitable OR early-sniper wallets outside tier 1) -> forensics_tier2.json,
   print READY_FOR_TIER2 (main agent launches shards).
4. Otherwise: print one progress line, stay quiet.

State: forensics_pipeline.json in this dir.
"""
import json, os, re, sqlite3, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
CLS = os.path.join(HERE, "forensics_classifications")
CANDS = os.path.join(HERE, "forensics_candidates.json")
STATE = os.path.join(HERE, "forensics_pipeline.json")
QC = os.path.join(HERE, "forensics_qc_report.json")
TIER2 = os.path.join(HERE, "forensics_tier2.json")


def state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}


def save_state(s):
    json.dump(s, open(STATE, "w"), indent=1)


def verdict_of(path):
    try:
        v = json.load(open(path))["verdict"]
    except Exception:
        return "UNREADABLE", "low"
    m = re.search(r"(INDEPENDENT-TRADER|COPY-TRADER|SNIPER-BOT|WASH|BUNDLE|INCONCLUSIVE)",
                  v.upper())
    cls = m.group(1) if m else "UNKNOWN"
    conf = "high" if "HIGH" in v.upper()[:400] else ("low" if "LOW" in v.upper()[:400] else "med")
    track = "YES" in v.upper().split("TRACK-WORTHY")[-1][:60] if "TRACK-WORTHY" in v.upper() else None
    return cls, conf


def run_qc():
    files = [f for f in os.listdir(CLS) if f.endswith(".json")]
    tally = {}
    independents = []
    for fn in files:
        w = fn[:-5]
        cls, conf = verdict_of(os.path.join(CLS, fn))
        tally[cls] = tally.get(cls, 0) + 1
        if cls == "INDEPENDENT-TRADER":
            independents.append({"wallet": w, "confidence": conf})
    # cross-check vs judge_scores
    con = sqlite3.connect(DB)
    try:
        scores = dict(con.execute("SELECT wallet, score FROM judge_scores"))
    except Exception:
        scores = {}
    con.close()
    for ind in independents:
        ind["judge_score"] = scores.get(ind["wallet"])
        ind["in_judge"] = ind["wallet"] in scores
    new_hits = [i for i in independents if not i["in_judge"]]
    report = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "classified": len(files), "tally": tally,
              "independent_traders": independents,
              "new_hits_not_in_judge": new_hits}
    json.dump(report, open(QC, "w"), indent=1)
    return report


def build_tier2():
    tier1 = set(json.load(open(CANDS))["count"] and
                [c["wallet"] for c in json.load(open(CANDS))["candidates"]])
    con = sqlite3.connect(DB)
    cols = ["wallet", "n_trades", "n_buys", "n_sells", "buy_vol", "sell_vol",
            "n_pools", "first_ts", "last_ts", "avg_buy", "buy_std", "net_usd",
            "avg_entry_min", "n_early", "avg_hold_min"]
    rows = con.execute(f"""SELECT {','.join(cols)} FROM wallet_profiles
        WHERE n_pools >= 3 AND buy_vol >= 20 AND net_usd > 0""").fetchall()
    cands = [dict(zip(cols, r)) for r in rows if r[0] not in tier1]
    con.close()
    json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "count": len(cands), "candidates": cands},
              open(TIER2, "w"), indent=1)
    return len(cands)


def alive_shards():
    import subprocess
    out = subprocess.run(["pgrep", "-af", "wallet_forensics.py classify"],
                         capture_output=True).stdout.decode()
    alive = set()
    for line in out.splitlines():
        m = re.search(r"classify ([0-3]) 4", line)
        if m and "pgrep" not in line:
            alive.add(m.group(1))
    return alive


def main():
    st = state()
    # self-healing: relaunch dead classification shards (they die on VM restarts)
    # All 4 shards supervised per user's full-throttle directive (2026-10-05).
    # Re-check liveness immediately before EACH launch: the old code snapshotted
    # once then launched with 45s staggers, racing manual launches into duplicate
    # shards (2026-10-05 04:48 IST incident).
    if not st.get("qc_done"):
        import subprocess
        for shard in ("0", "1", "2", "3"):
            if shard not in alive_shards():
                subprocess.Popen(
                    ["python3", os.path.join(HERE, "wallet_forensics.py"),
                     "classify", shard, "4"],
                    cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                print(f"HEALED: relaunched dead shard {shard}")
                time.sleep(45)  # stagger to avoid synchronized API bursts
    tier1_total = json.load(open(CANDS))["count"] if os.path.exists(CANDS) else 5483
    done = len([f for f in os.listdir(CLS) if f.endswith(".json")]) if os.path.exists(CLS) else 0
    print(f"progress: tier1 {done}/{tier1_total} ({100*done/max(tier1_total,1):.1f}%)")
    if done >= tier1_total and not st.get("qc_done"):
        report = run_qc()
        st["qc_done"] = True
        save_state(st)
        print("PHASE_TRANSITION: tier1 complete, QC report written")
        print(f"tally: {report['tally']}")
        hits = report["new_hits_not_in_judge"]
        print(f"new independent-trader hits not in JUDGE: {len(hits)}")
        for h in hits[:15]:
            print(f"  HIT {h['wallet'][:16]} conf={h['confidence']}")
        return
    if st.get("qc_done") and not st.get("tier2_built"):
        n = build_tier2()
        st["tier2_built"] = True
        save_state(st)
        print(f"PHASE_TRANSITION: tier2 ready — {n} candidates (main agent: launch shards)")
        return
    if st.get("tier2_built"):
        print("pipeline: tier2 queued, awaiting shard launch")


if __name__ == "__main__":
    main()
