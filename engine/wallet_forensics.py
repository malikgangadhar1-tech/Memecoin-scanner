#!/usr/bin/env python3
"""wallet-live/wallet_forensics.py - full-universe wallet fingerprinting.

Phase 1 (SQL-only, zero tokens): build wallet_profiles, one row per wallet in
watch.db, from single-pass GROUP BY queries. trades has NO wallet index, so
everything is set-based; per-wallet loops are forbidden.

Phase 2 (triage, SQL-only): rank wallets by interestingness heuristics ->
forensics_candidates.json. Generous by design (recall over precision).

Phase 3 (Gemini burn): classify each candidate INDEPENDENT-TRADER / COPY-TRADER /
SNIPER-BOT / WASH / BUNDLE / INCONCLUSIVE with evidence bundles.
Resumable; writes forensics_classifications/<wallet>.json.

Phase 4 (rings, SQL-only): coordination detection - wallet pairs whose first
buys on the same pool land within 60s, then connected components -> rings.
Writes forensics_rings.json.

Usage: python3 wallet_forensics.py phase1|triage|classify|rings|all
"""
import json, os, sqlite3, subprocess, sys, time
from collections import defaultdict

sys.path.insert(0, os.path.expanduser("~/workspace/token-burn"))
from burn_ledger import generate_logged

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
GEMINI = os.path.expanduser("~/workspace/skills/gemini/bin/gemini")
CANDS = os.path.join(HERE, "forensics_candidates.json")
OUT = os.path.join(HERE, "forensics_classifications")
os.makedirs(OUT, exist_ok=True)
RINGS_OUT = os.path.join(HERE, "forensics_rings.json")

MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview"]

CLASSIFY_PROMPT = """You are a Solana memecoin forensics analyst. Classify this wallet based ONLY on
the on-chain evidence below, recorded live by our own watcher (every trade listed
actually happened). Never invent trades, profits, or relationships.

WALLET STATISTICAL PROFILE (all tracked pools, lifetime):
{profile}

PER-POOL DETAIL (up to 15 most-traded pools):
{detail}

Classify as one of: INDEPENDENT-TRADER / COPY-TRADER / SNIPER-BOT / WASH / BUNDLE / INCONCLUSIVE.
Behavioral tells:
- INDEPENDENT-TRADER: irregular manual sizes, holds minutes-hours, tranches out, varied entry timing, trades over days
- SNIPER-BOT: first buys within seconds of pool birth, identical sizes, sells within seconds-minutes
- WASH: buys ~= sells on same pools, near-zero net, high roundtrip share
- BUNDLE: many tiny same-second buys across wallets (coordinated), dust sizes
- COPY-TRADER: entries consistently minutes after other wallets' entries on same pools

Then give:
1. Verdict (one line) + confidence (high/med/low).
2. Evidence FOR (cite specific numbers).
3. Evidence AGAINST (cite specific numbers).
4. Track-worthy? YES/NO and why in one line.
"""

# Batch classification (2026-10-05): the API bills rate limits per REQUEST, not
# per token (RPM 5, RPD ~20, TPM 250K). Batching N wallets per call multiplies
# throughput N-fold inside the same limits. INPUT could take 200K+ tokens, but
# OUTPUT is the binding constraint (the model must write a verdict per wallet;
# ~8K max output). 40/batch (~17K in, ~6.6K out) pushes output near its safe
# limit; beyond this the model truncates or degrades. Validated stepwise:
# 3 -> 30 -> 40.
BATCH_SIZE = 40

BATCH_CLASSIFY_PROMPT = """You are a Solana memecoin forensics analyst. Classify EACH of the {n} wallets below based ONLY on
the on-chain evidence given, recorded live by our own watcher (every trade listed
actually happened). Never invent trades, profits, or relationships. Analyze each
wallet INDEPENDENTLY - do not let one wallet's pattern influence another's verdict.

{wallets}

Classify each as one of: INDEPENDENT-TRADER / COPY-TRADER / SNIPER-BOT / WASH / BUNDLE / INCONCLUSIVE.
Behavioral tells:
- INDEPENDENT-TRADER: irregular manual sizes, holds minutes-hours, tranches out, varied entry timing, trades over days
- SNIPER-BOT: first buys within seconds of pool birth, identical sizes, sells within seconds-minutes
- WASH: buys ~= sells on same pools, near-zero net, high roundtrip share
- BUNDLE: many tiny same-second buys across wallets (coordinated), dust sizes
- COPY-TRADER: entries consistently minutes after other wallets' entries on same pools

Respond with ONLY a JSON array, one object per wallet in the SAME ORDER as above, echoing each address exactly:
[{{"wallet": "<address>", "verdict": "<ONE of the six>", "confidence": "high|med|low",
   "evidence_for": "<cite specific numbers>", "evidence_against": "<cite specific numbers>",
   "track_worthy": "YES|NO", "track_why": "<one line>"}}]
No markdown fences, no commentary - just the JSON array."""


def phase1():
    con = sqlite3.connect(DB)
    con.execute("DROP TABLE IF EXISTS wallet_profiles")
    print("phase1: building wallet_profiles (set-based, 4 scans)...", flush=True)
    con.execute("""
    CREATE TABLE wallet_profiles AS
    SELECT b.wallet, b.n_trades, b.n_buys, b.n_sells, b.buy_vol, b.sell_vol,
           b.n_pools, b.first_ts, b.last_ts, b.avg_buy, b.buy_std, b.net_usd,
           e.avg_entry_min, e.n_early, h.avg_hold_min
    FROM (
        SELECT wallet, COUNT(*) n_trades,
               SUM(CASE WHEN kind='buy' THEN 1 ELSE 0 END) n_buys,
               SUM(CASE WHEN kind='sell' THEN 1 ELSE 0 END) n_sells,
               SUM(CASE WHEN kind='buy' THEN vol_usd ELSE 0 END) buy_vol,
               SUM(CASE WHEN kind='sell' THEN vol_usd ELSE 0 END) sell_vol,
               COUNT(DISTINCT pool) n_pools, MIN(ts) first_ts, MAX(ts) last_ts,
               AVG(CASE WHEN kind='buy' THEN vol_usd END) avg_buy,
               sqrt(MAX(0, AVG(CASE WHEN kind='buy' THEN vol_usd*vol_usd END)
                        - AVG(CASE WHEN kind='buy' THEN vol_usd END)
                         *AVG(CASE WHEN kind='buy' THEN vol_usd END))) buy_std,
               SUM(CASE WHEN kind='sell' THEN vol_usd ELSE 0 END)
               - SUM(CASE WHEN kind='buy' THEN vol_usd ELSE 0 END) net_usd
        FROM trades GROUP BY wallet
    ) b
    LEFT JOIN (
        SELECT wallet, AVG((julianday(fb)-julianday(p.created_at))*24*60) avg_entry_min,
               SUM(CASE WHEN (julianday(fb)-julianday(p.created_at))*24*60 < 5 THEN 1 ELSE 0 END) n_early
        FROM (SELECT wallet, pool, MIN(ts) fb FROM trades WHERE kind='buy' GROUP BY wallet, pool) x
        JOIN pools p ON p.pool = x.pool
        GROUP BY wallet
    ) e ON e.wallet = b.wallet
    LEFT JOIN (
        SELECT wallet, AVG((julianday(fs)-julianday(fb))*24*60) avg_hold_min
        FROM (SELECT wallet, pool,
                     MIN(CASE WHEN kind='buy' THEN ts END) fb,
                     MIN(CASE WHEN kind='sell' THEN ts END) fs
              FROM trades GROUP BY wallet, pool) y
        WHERE fs IS NOT NULL AND fs > fb
        GROUP BY wallet
    ) h ON h.wallet = b.wallet""")
    con.execute("CREATE UNIQUE INDEX idx_wp_wallet ON wallet_profiles(wallet)")
    con.execute("CREATE INDEX idx_wp_pools ON wallet_profiles(n_pools, buy_vol)")
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM wallet_profiles").fetchone()[0]
    print(f"phase1: complete, {n} wallets profiled", flush=True)
    con.close()


def triage():
    con = sqlite3.connect(DB)
    rows = con.execute("""SELECT wallet, n_trades, n_buys, n_sells, buy_vol, sell_vol,
        n_pools, first_ts, last_ts, avg_buy, buy_std, net_usd, avg_entry_min,
        n_early, avg_hold_min
        FROM wallet_profiles
        WHERE n_pools >= 5 AND buy_vol >= 50 AND net_usd > 50
        ORDER BY net_usd DESC""").fetchall()
    cols = ["wallet", "n_trades", "n_buys", "n_sells", "buy_vol", "sell_vol",
            "n_pools", "first_ts", "last_ts", "avg_buy", "buy_std", "net_usd",
            "avg_entry_min", "n_early", "avg_hold_min"]
    cands = [dict(zip(cols, r)) for r in rows]
    json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "count": len(cands), "candidates": cands},
              open(CANDS, "w"), indent=1)
    print(f"triage: {len(cands)} candidates -> {CANDS}")
    con.close()


def detail_bundle(con, wallet, prof):
    con.execute("""CREATE TABLE IF NOT EXISTS _wp_pool AS
        SELECT wallet, pool,
               SUM(kind='buy') AS nb,
               SUM(CASE WHEN kind='buy' THEN vol_usd ELSE 0 END) AS bv,
               SUM(kind='sell') AS ns,
               SUM(CASE WHEN kind='sell' THEN vol_usd ELSE 0 END) AS sv,
               MIN(CASE WHEN kind='buy' THEN ts END) AS fb
        FROM trades GROUP BY wallet, pool""")
    con.execute("CREATE INDEX IF NOT EXISTS _wpp_w ON _wp_pool(wallet, bv)")
    lines = []
    for pool, name, nb, bv, ns, sv, fb in con.execute(
            """SELECT w.pool, p.name, w.nb, w.bv, w.ns, w.sv, w.fb
               FROM _wp_pool w LEFT JOIN pools p ON p.pool=w.pool
               WHERE w.wallet=? ORDER BY w.bv DESC LIMIT 15""", (wallet,)):
        lines.append(f"- {name or pool[:12]}: {nb} buys ${bv or 0:.0f}, {ns} sells ${sv or 0:.0f}, first buy {fb}")
    return "\n".join(lines)


def ask_gemini(prompt):
    # Routed through the burn ledger: exact token counts per call.
    return generate_logged(prompt, source="wallet-forensics-classify")


def _is_transient(err):
    """True if this error warrants backing off and retrying the same work
    (not skipping it). Covers quota/rate limits AND model overload (503s)."""
    e = err.lower()
    return ("429" in err or "503" in err or "rate" in e or "quota" in e
            or "overloaded" in e or "unavailable" in e)


def _wallet_block(con, c, j, n):
    """One wallet's evidence block for the batch prompt."""
    w = c["wallet"]
    prof = "\n".join(f"- {k}: {v}" for k, v in c.items() if k != "wallet")
    detail = detail_bundle(con, w, c)
    return (f"=== WALLET {j + 1}/{n}: {w} ===\n"
            f"STATISTICAL PROFILE:\n{prof}\nPER-POOL DETAIL:\n{detail}")


def _save_verdict(c, r, model):
    """Save one wallet's structured verdict (downstream-QC friendly)."""
    w = c["wallet"]
    outp = os.path.join(OUT, w + ".json")
    json.dump({"wallet": w, "profile": c, "model": model,
               "classified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "verdict": r.get("verdict"), "confidence": r.get("confidence"),
               "evidence_for": r.get("evidence_for"),
               "evidence_against": r.get("evidence_against"),
               "track_worthy": r.get("track_worthy"),
               "track_why": r.get("track_why"),
               "batch": True}, open(outp, "w"), indent=1)


def _parse_batch(text, batch):
    """Parse the model's JSON array; validate order and addresses."""
    t = text.strip()
    if t.startswith("```"):
        # tolerate markdown fences
        t = t.split("```")[1]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    results = json.loads(t)
    if not isinstance(results, list) or len(results) != len(batch):
        raise ValueError(f"expected {len(batch)} verdicts, got "
                         f"{len(results) if isinstance(results, list) else '?'}")
    for c, r in zip(batch, results):
        if r.get("wallet") != c["wallet"]:
            raise ValueError(f"address mismatch: {r.get('wallet')} != {c['wallet']}")
        if r.get("verdict") not in ("INDEPENDENT-TRADER", "COPY-TRADER",
                                    "SNIPER-BOT", "WASH", "BUNDLE", "INCONCLUSIVE"):
            raise ValueError(f"bad verdict: {r.get('verdict')}")
    return results


def _classify_single(con, c):
    """Fallback: original single-wallet classification."""
    w = c["wallet"]
    outp = os.path.join(OUT, w + ".json")
    prof = "\n".join(f"- {k}: {v}" for k, v in c.items() if k != "wallet")
    detail = detail_bundle(con, w, c)
    text, model = ask_gemini(CLASSIFY_PROMPT.format(profile=prof, detail=detail))
    if not text:
        return False, str(model)
    json.dump({"wallet": w, "profile": c, "model": model,
               "classified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "verdict": text.split("\n")[0], "batch": False,
               "full_text": text}, open(outp, "w"), indent=1)
    return True, model


def classify(shard=0, nshards=1):
    if not os.path.exists(CANDS):
        print("run triage first")
        return
    cands = json.load(open(CANDS))["candidates"]
    cands = [c for i, c in enumerate(cands) if i % nshards == shard]
    con = sqlite3.connect(DB)
    # pending = unclassified wallets for this shard, in order
    pending = [c for c in cands
               if not os.path.exists(os.path.join(OUT, c["wallet"] + ".json"))]
    done, failed = 0, 0
    quota_backoff = 0  # consecutive quota-storm strikes; resets on success
    b = 0
    while b < len(pending):
        batch = pending[b:b + BATCH_SIZE]
        # skip any that got classified since (e.g. by a healed overlapping shard)
        batch = [c for c in batch
                 if not os.path.exists(os.path.join(OUT, c["wallet"] + ".json"))]
        if not batch:
            b += BATCH_SIZE
            continue
        blocks = [_wallet_block(con, c, j, len(batch)) for j, c in enumerate(batch)]
        prompt = BATCH_CLASSIFY_PROMPT.format(n=len(batch),
                                              wallets="\n\n".join(blocks))
        text, model = ask_gemini(prompt)
        if not text:
            err = str(model)
            # TRANSIENT (quota/rate/overload): do NOT advance past this batch.
            # A bare `continue` races through the candidate list classifying
            # nothing while the storm lasts (2026-10-05 incident). Back off,
            # retry same batch.
            if _is_transient(err):
                quota_backoff += 1
                wait = min(300 * quota_backoff, 1800)  # 5m,10m,15m... cap 30m
                print(f"BACKOFF[{shard}] batch@{b}: {err[:60]}; sleeping "
                      f"{wait // 60}m, batch preserved (strike {quota_backoff})",
                      flush=True)
                time.sleep(wait)
                continue  # retry same batch; b unchanged
            print(f"FAIL batch@{b}: {model}", flush=True)
            failed += len(batch)
            b += BATCH_SIZE
            continue
        quota_backoff = 0
        try:
            results = _parse_batch(text, batch)
        except Exception as e:
            print(f"BATCH_PARSE_FAIL[{shard}] batch@{b}: {e}; "
                  f"falling back to singles", flush=True)
            quota_hit = False
            for c in batch:
                ok, info = _classify_single(con, c)
                if ok:
                    done += 1
                    continue
                err = str(info)
                if _is_transient(err):
                    # transient hit mid-fallback: do NOT advance b; the
                    # top-of-loop filter skips already-classified wallets
                    quota_backoff += 1
                    wait = min(300 * quota_backoff, 1800)
                    print(f"BACKOFF[{shard}] single-fallback: {err[:60]}; "
                          f"sleeping {wait // 60}m, batch preserved", flush=True)
                    time.sleep(wait)
                    quota_hit = True
                    break
                failed += 1
            if not quota_hit:
                b += BATCH_SIZE
            continue
        for c, r in zip(batch, results):
            _save_verdict(c, r, model)
            done += 1
        if done % 50 == 0:
            print(f"classify[{shard}]: {done} done, {failed} failed", flush=True)
        b += BATCH_SIZE
        time.sleep(5)  # light pacing; the atomic RPM gate in generate_logged is the authority
    print(f"classify[{shard}]: done={done} failed={failed}")
    con.close()


def rings():
    # wallet pairs whose first buys on the same pool land within 60s.
    # Restricted to triaged candidates (the wallets worth tracking); temp
    # files go to the workspace disk (/tmp is a 512M tmpfs and chokes).
    import tempfile
    tmpdir = os.path.join(HERE, "tmp_rings")
    os.makedirs(tmpdir, exist_ok=True)
    os.environ["TMPDIR"] = tmpdir
    cands = json.load(open(CANDS))["candidates"]
    cw = set(c["wallet"] for c in cands)
    con = sqlite3.connect(DB)
    con.execute("DROP TABLE IF EXISTS _ring_fb")
    con.execute("CREATE TEMP TABLE _ring_fb(wallet TEXT, pool TEXT, fb TEXT)")
    print(f"rings: staging first-buys for {len(cw)} candidates...", flush=True)
    wallets = list(cw)
    for i in range(0, len(wallets), 1000):
        chunk = wallets[i:i + 1000]
        ph = ",".join("?" * len(chunk))
        con.execute(f"""INSERT INTO _ring_fb
            SELECT wallet, pool, MIN(ts) FROM trades
            WHERE kind='buy' AND wallet IN ({ph}) GROUP BY wallet, pool""", chunk)
        if i % 2000 == 0:
            print(f"rings: staged {i}/{len(wallets)}", flush=True)
    con.execute("CREATE INDEX _rfx ON _ring_fb(pool, fb)")
    print("rings: finding co-buy pairs...", flush=True)
    pairs = con.execute("""
        SELECT a.wallet, b.wallet, COUNT(*) n, COUNT(DISTINCT a.pool) pools
        FROM _ring_fb a
        JOIN _ring_fb b
          ON a.pool = b.pool AND a.wallet < b.wallet
         AND ABS(julianday(a.fb) - julianday(b.fb))*86400 <= 60
        GROUP BY a.wallet, b.wallet HAVING pools >= 3""").fetchall()
    print(f"rings: {len(pairs)} pairs with >=3 co-bought pools", flush=True)
    # connected components
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for a, b, n, pools in pairs:
        union(a, b)
    comps = defaultdict(list)
    for a, b, n, pools in pairs:
        comps[find(a)].append((a, b, n, pools))
    rings_out = []
    for root, edges in comps.items():
        members = set()
        for a, b, n, pools in edges:
            members.add(a); members.add(b)
        if len(members) >= 3:
            rings_out.append({"size": len(members), "members": sorted(members),
                              "edges": [(a[:12], b[:12], n, pools) for a, b, n, pools in edges]})
    rings_out.sort(key=lambda r: -r["size"])
    json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "rings": rings_out}, open(RINGS_OUT, "w"), indent=1)
    print(f"rings: {len(rings_out)} rings (3+ members) -> {RINGS_OUT}")
    con.close()


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("phase1", "all"):
        phase1()
    if which in ("triage", "all"):
        triage()
    if which in ("classify", "all"):
        shard = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        nshards = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        classify(shard, nshards)
    if which in ("rings", "all"):
        rings()
