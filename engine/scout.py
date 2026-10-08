#!/usr/bin/env python3
"""wallet-live/scout.py - Gemini 3.6 early-traction scout (the pleb's grinding shift).

Runs every 2h via cron. For pools first seen in the last SCOUT_WINDOW_H hours,
computes compact early-buyer stats from our own SQLite (no API calls), then hands
the digest to Gemini 3.6 and asks it to rank the most organic-looking launches
and flag bot-seeded/spoof patterns.

Output: hidden_files/wallet-live/scout_reports/<ts>.json
Prints SCOUT_HIT lines only for pools Gemini scores >= 8/10 organic.
Silent otherwise. The main agent supervises: nothing reaches Rohit without
independent verification.

Size features (avg/median buy, vol-per-buyer) are computed deterministically
in pool_stats — the pleb consumes them, never derives them (fone lesson).
Micro-buy swarms (20+ sized buys, median <$1) are skipped in code via
micro_swarm_skip before the pleb ever sees them.
"""
import json, os, sqlite3, statistics, subprocess, sys, time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.expanduser("~/workspace/token-burn"))
from burn_ledger import generate_logged

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview"]  # directive first, fallbacks on 503
OUTDIR = os.path.join(HERE, "scout_reports")
SCOUT_WINDOW_H = 6
MAX_POOLS = 40          # bound prompt size: top pools by distinct buyers
HIT_THRESHOLD = 8       # organic score >= this prints SCOUT_HIT

os.makedirs(OUTDIR, exist_ok=True)

PROMPT = """You are a Solana memecoin forensics analyst. Below is a JSON digest of newly
launched pools and their EARLY buyer stats, recorded live from birth by our own
watcher. Times are ISO UTC. vol = USD. Digest includes roundtrip_vol_share
(fraction of volume from wallets that both bought and sold — wash indicator),
buys_per_buyer, and PRECOMPUTED size features: avg_buy_usd, median_buy_usd,
buy_vol_per_buyer_usd. USE THESE VALUES AS GIVEN — do not do your own
arithmetic, you are bad at it. Buyer count without size is meaningless.
Dead-burst pools (<10 min trade window, silent 10+ min) and micro-buy swarms
are skipped in code before you see them, not re-flagged every run.

For each pool, score organic_traction 0-10:
- High: many distinct buyers arriving over minutes (not one block), buys spread
  across wallets, buy vol > sell vol, no single wallet dominating (>40% of buy
  vol is a red flag), steady arrival not a single burst, low roundtrip_vol_share
  (few wallets round-tripping buy-then-sell). Healthy size: median_buy_usd
  comfortably above a few dollars — real early buyers risk real size.
- Low: 1-3 wallets doing all buying, one giant launch-second buy (mcap spoof),
  buy then immediate dump pattern, sells >= buys, high roundtrip_vol_share
  (same wallets buying AND selling = manufactured churn/wash, not demand).
- MICRO-BUY SWARM (score <=3, verdict bot-seeded): dozens/hundreds of distinct
  buyers but dust-sized buys — median_buy_usd under ~$2 or avg_buy_usd under
  ~$3. Hundreds of $0.87 "buyers" is bot/airdrop-farmer seeding, not demand,
  no matter how impressive distinct_buyers looks. Median $1-3 with real wallet
  spread is borderline: score cautiously, never above 5.

For each pool, score organic_traction 0-10:
- High: many distinct buyers arriving over minutes (not one block), buys spread
  across wallets, buy vol > sell vol, no single wallet dominating (>40% of buy
  vol is a red flag), steady arrival not a single burst, low roundtrip_vol_share
  (few wallets round-tripping buy-then-sell).
- Low: 1-3 wallets doing all buying, one giant launch-second buy (mcap spoof),
  buy then immediate dump pattern, sells >= buys, high roundtrip_vol_share
  (same wallets buying AND selling = manufactured churn/wash, not demand).

Return ONLY valid JSON, no markdown, no commentary:
{"ranked": [{"pool": "<pool id>", "name": "<name>", "score": <0-10>,
  "verdict": "organic|bot-seeded|spoof|too-early",
  "reasons": ["<short>", ...],
  "watch": "<one-line what to watch next>"}, ...],
 "notes": "<2-sentence overall read>"}
Rank best-first. Include at most 8 pools; skip pools with <5 distinct buyers
unless something is remarkable. Be harsh: most launches are junk.

DIGEST:
"""


def pool_stats(con, pool):
    row = con.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN kind='buy' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN kind='sell' THEN 1 ELSE 0 END),
                  COUNT(DISTINCT CASE WHEN kind='buy' THEN wallet END),
                  COALESCE(SUM(CASE WHEN kind='buy' THEN vol_usd ELSE 0 END),0),
                  COALESCE(SUM(CASE WHEN kind='sell' THEN vol_usd ELSE 0 END),0),
                  MIN(ts), MAX(ts)
           FROM trades WHERE pool=?""", (pool,)).fetchone()
    n, nb, ns, buyers, bvol, svol, tmin, tmax = row
    top = con.execute(
        """SELECT COALESCE(SUM(vol_usd),0) FROM trades WHERE pool=? AND kind='buy'
           AND wallet = (SELECT wallet FROM trades WHERE pool=? AND kind='buy'
           AND wallet IS NOT NULL GROUP BY wallet ORDER BY SUM(vol_usd) DESC LIMIT 1)""",
        (pool, pool)).fetchone()[0]
    early10 = None
    first_seen = con.execute("SELECT first_seen FROM pools WHERE pool=?", (pool,)).fetchone()[0]
    if first_seen:
        try:
            t0 = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            t1 = (t0 + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
            early10 = con.execute(
                "SELECT COUNT(DISTINCT wallet) FROM trades WHERE pool=? AND kind='buy' AND ts<=?",
                (pool, t1)).fetchone()[0]
        except Exception:
            pass
    span_min = None
    if tmin and tmax:
        try:
            a = datetime.fromisoformat(tmin.replace("Z", "+00:00"))
            b = datetime.fromisoformat(tmax.replace("Z", "+00:00"))
            span_min = round((b - a).total_seconds() / 60, 1)
        except Exception:
            pass
    # round-trip volume share: fraction of total volume from wallets that BOTH
    # bought and sold. High share = manufactured churn/wash, not organic demand
    # (TRENCHDOTS lesson: low top-buyer concentration + buy/sell parity can hide
    # 96% round-trip volume).
    tot_vol = bvol + svol
    rt_share = 0.0
    if tot_vol:
        rt_vol = con.execute(
            """SELECT COALESCE(SUM(vol_usd),0) FROM trades WHERE pool=? AND wallet IS NOT NULL
               AND wallet IN (SELECT wallet FROM trades WHERE pool=? AND kind='buy'
                              INTERSECT
                              SELECT wallet FROM trades WHERE pool=? AND kind='sell')""",
            (pool, pool, pool)).fetchone()[0]
        rt_share = round(rt_vol / tot_vol, 3)
    # size features, computed deterministically HERE so the pleb never does
    # arithmetic. fone lesson: buyer count without size is a lie.
    buy_vols = [r[0] for r in con.execute(
        "SELECT vol_usd FROM trades WHERE pool=? AND kind='buy' AND vol_usd IS NOT NULL",
        (pool,)).fetchall()]
    n_sized = len(buy_vols)
    avg_buy = round(sum(buy_vols) / n_sized, 2) if n_sized else 0
    med_buy = round(statistics.median(buy_vols), 2) if n_sized else 0
    vol_per_buyer = round(bvol / buyers, 2) if buyers else 0
    return {"n_trades": n, "n_buys": nb, "n_sells": ns, "distinct_buyers": buyers,
            "buy_vol_usd": round(bvol, 2), "sell_vol_usd": round(svol, 2),
            "top_buyer_share": round(top / bvol, 3) if bvol else 0,
            "buyers_first_10min": early10, "trade_span_min": span_min,
            "buys_per_buyer": round(nb / buyers, 2) if buyers else 0,
            "roundtrip_vol_share": rt_share,
            "avg_buy_usd": avg_buy, "median_buy_usd": med_buy,
            "buy_vol_per_buyer_usd": vol_per_buyer,
            "_n_sized_buys": n_sized}


def micro_swarm_skip(st):
    """Deterministic micro-buy swarm filter.

    Dozens+ of sized buys with dust-sized median (<$1) = bot/airdrop-farmer
    seeding, not demand. fone (2026-10-01): 400 buys, $0.87 avg, 399 distinct
    buyers — the pleb scored it 8/10 on buyer count alone because it never
    did the division. Real early buyers risk real size (AI/SOL runner: $35.94
    avg). Skip before the pleb can drool over the buyer count.
    Borderline zone (median $1-3) is left to the prompt rubric, not hard-skipped:
    genuine $2-3 micro-buys exist.
    """
    n_sized = st.get("_n_sized_buys", 0)
    med = st.get("median_buy_usd") or 0
    return n_sized >= 20 and med < 1.0


def main():
    if not os.path.exists(DB):
        print("no db yet")
        return
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout=60000")  # watcher writes every 5 min; wait, don't crash
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=SCOUT_WINDOW_H)).strftime("%Y-%m-%dT%H:%M:%S")
    pools = con.execute(
        """SELECT pool, name, dex, first_seen, mcap_usd, fdv_usd, vol_h24
           FROM pools WHERE watching=1 AND dead=0 AND runner=0 AND first_seen>=?
           ORDER BY first_seen DESC""", (cutoff,)).fetchall()
    if not pools:
        print(f"ok no fresh pools in last {SCOUT_WINDOW_H}h")
        return

    digest = []
    now_utc = datetime.now(timezone.utc)
    for pool, name, dex, first_seen, mcap, fdv, vol24 in pools:
        st = pool_stats(con, pool)
        if (st["distinct_buyers"] or 0) < 3:
            continue
        # staleness guard: skip pools with no trade in the last 90 min —
        # re-scoring frozen data just re-flags yesterday's news every run
        try:
            last_ts = None
            span = st.get("trade_span_min")
            if st.get("trade_span_min") is not None:
                last_trade = con.execute("SELECT MAX(ts) FROM trades WHERE pool=?", (pool,)).fetchone()[0]
                if last_trade:
                    last_ts = datetime.fromisoformat(last_trade.replace("Z", "+00:00"))
            if last_ts and (now_utc - last_ts).total_seconds() > 90 * 60:
                continue
            # dead-burst filter: a launch wave that ran <10 min and has been
            # silent 10+ min is cooling/dead, not traction. All of 2026-09-30's
            # false positives (PROTO, TRENCHDOTS, Robinhood, GROK, PING) were
            # 2-7 min bursts that died; fone (2026-10-01) was a 2.5 min
            # $0.87-avg-buy swarm flagged at 28 min silent, inside the old
            # 30-min grace hole. A genuinely live wave (last trade <10 min
            # ago) still scores; a resumed pool re-enters via a later scan.
            if (last_ts is not None and span is not None and span < 10
                    and (now_utc - last_ts).total_seconds() > 10 * 60):
                continue
        except Exception:
            pass
        # micro-buy swarm filter (deterministic, before the pleb sees it)
        if micro_swarm_skip(st):
            continue
        public = {k: v for k, v in st.items() if not k.startswith("_")}
        digest.append({"pool": pool, "name": name, "dex": dex, "first_seen": first_seen,
                       "mcap_usd": mcap, "fdv_usd": fdv, "vol_h24": vol24, **public})
    con.close()
    if not digest:
        print("ok pools too thin to judge")
        return
    digest.sort(key=lambda d: d["distinct_buyers"], reverse=True)
    digest = digest[:MAX_POOLS]

    # Via burn_ledger: single credential (custom.gemini2, sole survivor after the
    # 2026-10-05 disconnects) with exact token logging. (2026-10-05: CLI was single-credential.)
    raw, used_model = generate_logged(PROMPT + json.dumps(digest),
                                      source="wallet-scout")
    if not raw:
        err = str(used_model)
        # Quota storm: soft skip, not a hard failure. The shared blackout file
        # keeps us from burning discovery probes; the next 2h run retries.
        # Non-quota errors (auth etc.) stay hard failures so they get flagged.
        if "429" in err or "blackout" in err.lower():
            print(f"ok quota storm, scan skipped ({err[:80]})")
            return
        print(f"ERROR all gemini models failed, last: {used_model}")
        sys.exit(1)
    raw = raw.strip().strip("`")
    if raw.startswith("json"):
        raw = raw[4:].strip()
    try:
        report = json.loads(raw)
    except Exception as e:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
        open(os.path.join(OUTDIR, f"{ts}.raw.txt"), "w").write(raw)
        print(f"ERROR gemini returned unparseable JSON: {e}")
        sys.exit(1)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
    path = os.path.join(OUTDIR, f"{ts}.json")
    json.dump({"generated": ts, "model": used_model, "pools_scanned": len(digest), "report": report},
              open(path, "w"), indent=1)

    hits = [r for r in report.get("ranked", [])
            if isinstance(r.get("score"), (int, float)) and r["score"] >= HIT_THRESHOLD
            and r.get("verdict") == "organic"]
    print(f"ok scanned={len(digest)} hits={len(hits)} report={os.path.basename(path)}")
    for h in hits:
        print(f"SCOUT_HIT {h['name']} score={h['score']} pool={h['pool'][:12]} "
              f"{(h.get('reasons') or [''])[0][:100]}")


if __name__ == "__main__":
    main()
