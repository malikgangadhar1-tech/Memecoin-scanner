#!/usr/bin/env python3
"""wallet-live/judge.py - JUDGE wallet scoring layer (0-100, reasoning logged).

Deterministic scoring from our own watch.db — math in code, vibes in the model.
Scores the wallets that matter: runner-early buyers, converge candidates, and
fresh active wallets (>=5 buys in last 24h). Dust wallets are not scored.

Components (v1 weights):
  runner_hit   0-35  early pools that became runners / early pools (confidence-ramped)
  entry_timing 0-15  median minutes-after-launch of first buys (sniper discount <30s)
  exit_pnl     0-20  fraction of round-trip pools sold above avg buy price
  size         0-10  median buy size USD (conviction)
  breadth      0-10  distinct pools + day span
Modifiers:
  Gemini classification file (classifications/<wallet>.json): INDEPENDENT-TRADER
  high -> +8 / med -> +4 (cap 100); COPY-TRADER -> -8; SNIPER-BOT -> cap 40;
  WASH/BUNDLE -> cap 15. A manual forensic override (auditor_verdict /
  auditor_confidence in the same file, with reasoning) takes precedence over
  the raw model verdict when present.
  Bot penalties: identical-size buys (>70% same rounded USD, >=10 buys) -> -12;
  burst (>5 trades/min over <60min span) -> -8.
  Churn penalty (v2, synchronized): >=3 buys AND >=3 sells in one pool within
  30 min counts only when >=2 wallets churn the SAME pool (the manufactured-
  fake-PnL pattern); 2+ such pools -> cap 35, 1 pool -> -10. Lone-wallet
  high-frequency scalping with real extracted profit is the SNIPER-BOT
  profile, not a wash network.
  Low-confidence wallets (few samples) are capped at 79 so tiny samples can't
  hit the 80+ "top wallet" bar used by exits.py convergence alerts.

Output: judge_scores table in watch.db:
  wallet PK, score, components JSON, reasoning TEXT, scored_at, n_buys, confidence.
Prints JUDGE_TOP <wallet> <score> lines for newly-minted 80+ wallets.
"""
import calendar
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
CANDS = os.path.join(HERE, "candidates.json")
CLASS_DIR = os.path.join(HERE, "classifications")
EARLY_MIN = 30          # first buy within N min of pool creation = "early"
EARLY_N = 150           # first-N buyers considered for runner pools
FRESH_HOURS = 24
FRESH_MIN_BUYS = 5
TOP_SCORE = 70


def ts_epoch(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def main():
    # timeout=120: the 5-min wallet-watch writer holds the lock briefly; wait it out
    con = sqlite3.connect(DB, timeout=120)
    con.execute("""CREATE TABLE IF NOT EXISTS judge_scores(
        wallet TEXT PRIMARY KEY, score REAL, components TEXT, reasoning TEXT,
        scored_at TEXT, n_buys INTEGER, confidence TEXT)""")

    now = time.time()
    fresh_cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - FRESH_HOURS * 3600))

    # ---- scoring universe ----
    universe = set()
    if os.path.exists(CANDS):
        try:
            universe |= set(json.load(open(CANDS)).get("candidates", {}).keys())
        except Exception:
            pass
    runners = [r[0] for r in con.execute("SELECT pool FROM pools WHERE runner=1")]
    for pool in runners:
        for (w,) in con.execute(
                """SELECT wallet FROM trades WHERE pool=? AND kind='buy' AND wallet IS NOT NULL
                   GROUP BY wallet ORDER BY MIN(ts) LIMIT ?""", (pool, EARLY_N)):
            universe.add(w)
    for (w,) in con.execute(
            """SELECT wallet FROM trades WHERE kind='buy' AND ts>? AND wallet IS NOT NULL
               GROUP BY wallet HAVING COUNT(*)>=?""", (fresh_cutoff, FRESH_MIN_BUYS)):
        universe.add(w)
    universe.discard(None)
    if not universe:
        print("judge: empty universe")
        return
    print(f"judge: scoring universe={len(universe)}", flush=True)

    # ---- pool metadata ----
    pools = {}
    for pool, name, created_at, runner in con.execute(
            "SELECT pool, name, created_at, runner FROM pools"):
        pools[pool] = {"name": name, "created": ts_epoch(created_at) if created_at else None,
                       "runner": bool(runner)}

    # ---- per-wallet aggregates: ONE full scan ----
    agg = {}
    for w, n, nb, buy_usd, mn, mx, npools in con.execute(
            """SELECT wallet, COUNT(*),
                      SUM(CASE WHEN kind='buy' THEN 1 ELSE 0 END),
                      COALESCE(SUM(CASE WHEN kind='buy' THEN vol_usd ELSE 0 END),0),
                      MIN(ts), MAX(ts), COUNT(DISTINCT pool)
               FROM trades WHERE wallet IS NOT NULL GROUP BY wallet"""):
        if w in universe:
            agg[w] = {"n": n, "nbuys": nb or 0, "buy_usd": buy_usd or 0,
                      "mn": mn, "mx": mx, "npools": npools}

    # ---- first-buy per wallet per pool: ONE scan ----
    first_buy = {}
    for w, pool, fts in con.execute(
            """SELECT wallet, pool, MIN(ts) FROM trades
               WHERE kind='buy' AND wallet IS NOT NULL GROUP BY wallet, pool"""):
        if w in universe:
            first_buy.setdefault(w, {})[pool] = fts

    # ---- round-trip pnl + churn detection per wallet per pool: ONE scan ----
    rt = {}
    churn = {}
    pool_churners = {}  # pool -> wallets with >=3 buys AND >=3 sells inside 30 min
    for w, pool, nb, ns, bu, bb, su, sb, mn, mx in con.execute(
            """SELECT wallet, pool,
                      SUM(CASE WHEN kind='buy' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN kind='sell' THEN 1 ELSE 0 END),
                      COALESCE(SUM(CASE WHEN kind='buy' THEN vol_usd ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN kind='buy' THEN base_amount ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN kind='sell' THEN vol_usd ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN kind='sell' THEN base_amount ELSE 0 END),0),
                      MIN(ts), MAX(ts)
               FROM trades WHERE wallet IS NOT NULL GROUP BY wallet, pool"""):
        if w not in universe:
            continue
        if bu > 0 and su > 0 and bb > 0 and sb > 0:
            rt.setdefault(w, {})[pool] = (bu / bb, su / sb)  # avg buy px, avg sell px
        # churn candidate: >=3 buys AND >=3 sells crammed in the same pool within 30 min
        e1, e2 = ts_epoch(mn), ts_epoch(mx)
        if nb >= 3 and ns >= 3 and e1 and e2 and (e2 - e1) < 1800:
            pool_churners.setdefault(pool, set()).add(w)
    # The penalty fires only on SYNCHRONIZED churn: >=2 wallets churning the
    # same pool. That is the manufactured-fake-PnL pattern (GRIFFIN 2026-10-01:
    # five wallets buy/sell-churning within minutes). A lone wallet with >=3
    # buys and >=3 sells in 30 min that extracts real profit is a scalper, not
    # a wash network — the SNIPER-BOT classification cap handles that profile.
    for pool, ws in pool_churners.items():
        if len(ws) >= 2:
            for w in ws:
                churn[w] = churn.get(w, 0) + 1

    # ---- buy-size identicalness (bot marker): ONE scan ----
    ident = {}
    for w, n, ndist in con.execute(
            """SELECT wallet, COUNT(*), COUNT(DISTINCT CAST(vol_usd AS INT))
               FROM trades WHERE kind='buy' AND wallet IS NOT NULL AND vol_usd IS NOT NULL
               GROUP BY wallet"""):
        if w in universe and n:
            ident[w] = 1.0 - ndist / n

    # ---- median buy size per wallet: from first_buy pools (bounded lookups) ----
    # (skip: use mean buy size from agg as the size proxy; median needs per-trade
    #  values. Use mean with a cap — good enough for v1 conviction gauge.)
    prev_top = {r[0] for r in con.execute(
        "SELECT wallet FROM judge_scores WHERE score>=?", (TOP_SCORE,))}
    scored_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new_top = 0
    for w in sorted(universe):
        a = agg.get(w)
        if not a or a["nbuys"] == 0:
            continue
        fb = first_buy.get(w, {})
        comp, notes = {}, []

        # runner_hit
        early, hits, mins = 0, 0, []
        for pool, fts in fb.items():
            p = pools.get(pool)
            if not p or not p["created"]:
                continue
            fe = ts_epoch(fts)
            if fe is None:
                continue
            m = (fe - p["created"]) / 60.0
            if m <= EARLY_MIN:
                early += 1
                mins.append(m)
                if p["runner"]:
                    hits += 1
        rate = hits / early if early else 0.0
        # Bounty per confirmed runner hit (diminishing returns), cap 35.
        # Pure rate scoring starves when few runners exist; each hit is rare evidence.
        bounty = [15, 8, 5, 3]
        pts = sum(bounty[i] if i < len(bounty) else 2 for i in range(min(hits, 6)))
        pts = min(35, pts)
        comp["runner_hit"] = round(pts, 1)
        notes.append(f"runner_hit: {hits}/{early} early pools became runners -> {pts:.1f}/35")

        # entry_timing
        epts = 0.0
        if mins:
            mins.sort()
            med = mins[len(mins) // 2]
            if med < 0.5:
                epts = 6
            elif med <= 5:
                epts = 15
            elif med <= 15:
                epts = 11
            elif med <= 30:
                epts = 7
            else:
                epts = 3
            notes.append(f"entry_timing: median first buy +{med:.1f}min -> {epts}/15")
        else:
            notes.append("entry_timing: no timed first buys -> 0/15")
        comp["entry_timing"] = epts

        # exit_pnl
        trips = rt.get(w, {})
        ppts = 0.0
        if trips:
            prof = sum(1 for bp, sp in trips.values() if sp > bp)
            ppts = 20 * (prof / len(trips)) * min(1.0, len(trips) / 3)
            notes.append(f"exit_pnl: {prof}/{len(trips)} round trips sold above avg buy -> {ppts:.1f}/20")
        else:
            notes.append("exit_pnl: no round trips -> 0/20")
        comp["exit_pnl"] = round(ppts, 1)

        # size conviction (mean buy USD as proxy)
        mean_buy = a["buy_usd"] / a["nbuys"]
        spts = 10 if mean_buy >= 100 else 7 if mean_buy >= 50 else 5 if mean_buy >= 20 else 3 if mean_buy >= 5 else 1
        comp["size"] = spts
        notes.append(f"size: mean buy ${mean_buy:.0f} -> {spts}/10")

        # breadth
        bpts = 0
        if a["npools"] >= 15:
            bpts = 10
        elif a["npools"] >= 8:
            bpts = 7
        elif a["npools"] >= 4:
            bpts = 4
        elif a["npools"] >= 2:
            bpts = 2
        e1, e2 = ts_epoch(a["mn"]), ts_epoch(a["mx"])
        span_d = (e2 - e1) / 86400 if e1 and e2 else 0
        if span_d >= 7:
            bpts = min(10, bpts + 2)
        comp["breadth"] = bpts
        notes.append(f"breadth: {a['npools']} pools over {span_d:.1f}d -> {bpts}/10")

        score = sum(comp.values())

        # classification modifier
        # auditor_verdict (manual forensic override) takes precedence over the
        # raw model verdict when present — the inspector's call, with reasoning
        # recorded in the file (see JUDGE.md "auditor overrides").
        clf_path = os.path.join(CLASS_DIR, w + ".json")
        if os.path.exists(clf_path):
            try:
                cj = json.load(open(clf_path))
                v = (cj.get("auditor_verdict") or cj.get("verdict") or "").upper()
                conf = (cj.get("auditor_confidence") or cj.get("verdict") or "").upper()
                if "INDEPENDENT-TRADER" in v:
                    b = 8 if "HIGH" in conf else 4
                    score = min(100, score + b)
                    notes.append(f"classify: independent-trader -> +{b}")
                elif "COPY-TRADER" in v:
                    score -= 8
                    notes.append("classify: copy-trader -> -8")
                elif "SNIPER-BOT" in v:
                    score = min(score, 40)
                    notes.append("classify: sniper-bot -> capped at 40")
                elif "WASH" in v or "BUNDLE" in v:
                    score = min(score, 15)
                    notes.append("classify: wash/bundle -> capped at 15")
            except Exception:
                pass

        # bot penalties
        ir = ident.get(w, 0)
        if ir > 0.7 and a["nbuys"] >= 10:
            score -= 12
            notes.append(f"bot: {ir:.0%} identical-size buys -> -12")
        if e1 and e2 and (e2 - e1) < 3600 and a["n"] / max(1, (e2 - e1) / 60) > 5:
            score -= 8
            notes.append("bot: burst trading >5/min -> -8")

        # churn/wash penalty: synchronized buy+sell churn manufactures fake pnl
        cp = churn.get(w, 0)
        if cp >= 2:
            score = min(score, 35)
            notes.append(f"churn: synchronized buy+sell churn on {cp} pools -> capped at 35")
        elif cp == 1:
            score -= 10
            notes.append("churn: synchronized buy+sell churn on 1 pool -> -10")

        # confidence + low-sample cap
        if a["nbuys"] >= 20 and early >= 3:
            conf = "high"
        elif a["nbuys"] >= 8:
            conf = "med"
        else:
            conf = "low"
        if conf == "low":
            score = min(score, 79)
            notes.append("confidence low -> capped at 79")
        score = round(max(0, min(100, score)), 1)

        # Hark (Oct 7 2026): our watch.db is young (Fluffington's 2.2M-trade history did
        # not migrate), so thin local evidence must not erase his imported scores.
        # Keep max(imported, fresh) until local history matures.
        try:
            imp = con.execute("SELECT score FROM judge_scores_import WHERE wallet=?", (w,)).fetchone()
        except sqlite3.OperationalError:
            imp = None
        if imp and imp[0] is not None and imp[0] > score:
            notes.append(f"fresh score {score} < imported {imp[0]} -> kept imported (young local history)")
            score = imp[0]
        con.execute(
            "INSERT OR REPLACE INTO judge_scores VALUES (?,?,?,?,?,?,?)",
            (w, score, json.dumps(comp), "\n".join(notes), scored_at, a["nbuys"], conf))
        if score >= TOP_SCORE and w not in prev_top:
            new_top += 1
            print(f"JUDGE_TOP {w[:16]} score={score} conf={conf} buys={a['nbuys']}")
    con.commit()
    n80 = con.execute("SELECT COUNT(*) FROM judge_scores WHERE score>=?",
                      (TOP_SCORE,)).fetchone()[0]
    print(f"judge: scored universe, top{TOP_SCORE}={n80} new_top={new_top}")
    con.close()


if __name__ == "__main__":
    main()
