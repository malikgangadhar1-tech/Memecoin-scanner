#!/usr/bin/env python3
"""wallet-live/watch.py - 24/7 forward wallet discovery (100% keyless).

Replaces the historical approach (dead: public RPC truncates getTransaction,
pump.fun API doesn't serve graduated coins, GMGN is bot-walled).

Method: watch NEW pools from birth via GeckoTerminal (free, keyless). Record
their trades as they happen -> our records ARE the launch window. When a
watched pool crosses RUNNER_MCAP, its early buyers are already in the DB.

One cron run (every 5 min):
  1. DISCOVERY: new_pools p1-2 -> upsert pool metadata (2 reqs)
  2. WATCHLIST: pools age<24h & active, or mcap>=WATCH_MCAP
  3. TRADES: newest trades pages for watchlist pools, upsert by tx_hash
  4. MCAP: refresh market cap for watchlist (staggered, >15min old)
  5. RUNNER: mcap>=RUNNER_MCAP -> flag; backfill oldest trades via binary search
  6. PRUNE: age>48h, never runner, mcap<WATCH_MCAP -> dead

Output: silent unless new RUNNER flagged or hard ERROR. Exit 1 on hard error.
"""
import json, sqlite3, time, urllib.request, urllib.error, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "watch.db")
GT = "https://api.geckoterminal.com/api/v2"
UA = {"User-Agent": "Mozilla/5.0"}

RUNNER_MCAP = 200_000   # same bar as the old stage1b
WATCH_MCAP = 50_000      # keep watching anything this big
WATCH_HOURS = 24        # trade-poll window for young pools
PRUNE_HOURS = 48
MIN_TXNS = 8            # h24 txns at intake to join the watchlist
EARLY_BUYERS_N = 150
MAX_WATCHED = 25        # Hark: trimmed to fit 30 req/min budget
TRADE_PAGES = 2         # newest pages per pool per run
BACKFILL_PAGES = 6      # oldest pages to keep on runner backfill
REQ_SLEEP = 6.0         # Hark: sandbox IP gets 429 fast on GeckoTerminal; GT now only for discovery+mcap

_soft_errors = []


def gt(path, tries=3):
    for t in range(tries):
        try:
            req = urllib.request.Request(GT + path, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            time.sleep(REQ_SLEEP)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _soft_errors.append(f"429 on {path}")
                time.sleep(20 * (t + 1))
                continue
            if e.code in (500, 502, 503):
                time.sleep(5 * (t + 1))
                continue
            _soft_errors.append(f"HTTP {e.code} on {path}")
            return None
        except Exception as e:
            _soft_errors.append(f"{type(e).__name__} on {path}: {str(e)[:60]}")
            time.sleep(3 * (t + 1))
    return None


def db():
    # busy_timeout=300s: forensics classify shards (4x) hold write locks on the
    # 3GB DB for minutes; timeout=60s was getting SQLITE_BUSY on the final
    # prune UPDATE during lock storms (2026-10-05 wallet-watch run).
    con = sqlite3.connect(DB, timeout=300)
    con.execute("""CREATE TABLE IF NOT EXISTS pools(
        pool TEXT PRIMARY KEY, name TEXT, base_mint TEXT, dex TEXT,
        created_at TEXT, first_seen TEXT, mcap_usd REAL, fdv_usd REAL,
        vol_h24 REAL, txns_h24 INTEGER, price_usd REAL, liquidity_usd REAL,
        watching INTEGER DEFAULT 0, runner INTEGER DEFAULT 0,
        runner_at TEXT, dead INTEGER DEFAULT 0,
        last_trade_check TEXT, last_mcap_check TEXT, backfilled INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS trades(
        pool TEXT, tx_hash TEXT PRIMARY KEY, wallet TEXT, kind TEXT,
        ts TEXT, vol_usd REAL, base_amount REAL)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_trades_pool_ts ON trades(pool, ts)")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    for col in ("cand_at TEXT", "liquidity_usd REAL"):
        try:
            con.execute(f"ALTER TABLE pools ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    return con


def upsert_pool(con, p):
    a = p["attributes"]
    dex = (p.get("relationships", {}).get("dex", {}).get("data", {}) or {}).get("id", "")
    base = (p.get("relationships", {}).get("base_token", {}).get("data", {}) or {}).get("id", "")
    txns = a.get("transactions", {}) or {}
    h24 = (txns.get("h24", {}) or {})
    con.execute("""INSERT INTO pools(pool,name,base_mint,dex,created_at,first_seen,mcap_usd,fdv_usd,
                   vol_h24,txns_h24,price_usd,liquidity_usd,watching)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)
                   ON CONFLICT(pool) DO UPDATE SET mcap_usd=excluded.mcap_usd,
                     fdv_usd=excluded.fdv_usd, vol_h24=excluded.vol_h24,
                     txns_h24=excluded.txns_h24, price_usd=excluded.price_usd,
                     liquidity_usd=excluded.liquidity_usd""",
                (a["address"], a.get("name", ""), base, dex, a.get("pool_created_at"),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 _f(a.get("market_cap_usd")), _f(a.get("fdv_usd")),
                 _f((a.get("volume_usd", {}) or {}).get("h24")),
                 (h24.get("buys") or 0) + (h24.get("sells") or 0),
                 _f(a.get("base_token_price_usd")), _f(a.get("reserve_in_usd"))))


def _f(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def age_hours(created_at, first_seen):
    import calendar
    now = time.time()
    for s in (created_at, first_seen):
        if s:
            try:
                t = calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
                return (now - t) / 3600
            except Exception:
                continue
    return 9999


HELIUS_RPC = "https://mainnet.helius-rpc.com/" + (("?api-key=" + __import__("os").environ["HELIUS_API_KEY"]) if __import__("os").environ.get("HELIUS_API_KEY") else "")  # Hark injects the key on the wire; GitHub Actions passes HELIUS_API_KEY   # key injected on the wire (X-Api-Key)
CREDIT_BUDGET = int(os.environ.get("HELIUS_RUN_BUDGET", "300"))  # RPC calls per run (1 credit each); 1M/mo free
PER_POOL_TX = 40       # max new txs parsed per pool per run
_credits = [0]


def rpc(method, params):
    if _credits[0] >= CREDIT_BUDGET:
        return None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for t in range(3):
        try:
            _credits[0] += 1
            req = urllib.request.Request(HELIUS_RPC, data=body,
                                         headers={"Content-Type": "application/json", **UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.load(r)
            if "error" in d:
                _soft_errors.append(f"rpc {method}: {str(d['error'])[:60]}")
                return None
            time.sleep(0.12)   # free plan 10 RPS
            return d.get("result")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 * (t + 1)); continue
            _soft_errors.append(f"rpc HTTP {e.code}"); return None
        except Exception as e:
            _soft_errors.append(f"rpc {type(e).__name__}"); time.sleep(1)
    return None


def _parse_swap(tx, base_mint):
    """Return (wallet, kind, base_amount) from a jsonParsed tx, judged by the fee payer's base-token delta."""
    try:
        meta = tx["meta"]
        if meta.get("err"):
            return None
        keys = tx["transaction"]["message"]["accountKeys"]
        payer = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
        def bal(lst):
            s = 0.0
            for b in lst or []:
                if b.get("mint") == base_mint and b.get("owner") == payer:
                    s += float(b["uiTokenAmount"].get("uiAmount") or 0)
            return s
        delta = bal(meta.get("postTokenBalances")) - bal(meta.get("preTokenBalances"))
        if abs(delta) < 1e-12:
            return None
        return payer, ("buy" if delta > 0 else "sell"), abs(delta)
    except Exception:
        return None


def _ingest_sigs(con, pool, sigs, base_mint, price):
    new = 0
    for s in sigs:
        if s.get("err"):
            continue
        sig = s["signature"]
        if con.execute("SELECT 1 FROM trades WHERE tx_hash=?", (sig,)).fetchone():
            continue
        tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        if tx is None:
            if _credits[0] >= CREDIT_BUDGET:
                break
            continue
        p = _parse_swap(tx, base_mint)
        if not p:
            continue
        wallet, kind, amt = p
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s.get("blockTime") or tx.get("blockTime") or time.time()))
        try:
            con.execute("INSERT INTO trades(pool,tx_hash,wallet,kind,ts,vol_usd,base_amount) VALUES(?,?,?,?,?,?,?)",
                        (pool, sig, wallet, kind, ts, (amt * price) if price else None, amt))
            new += 1
        except sqlite3.IntegrityError:
            pass
    return new


def record_trades(con, pool, max_pages):
    """Hark/Helius: newest swaps since last seen signature, capped by PER_POOL_TX and the run's credit budget."""
    r = con.execute("SELECT base_mint, price_usd FROM pools WHERE pool=?", (pool,)).fetchone()
    if not r or not r[0]:
        return 0
    base_mint = r[0].split("_", 1)[-1]   # GeckoTerminal ids look like solana_<mint>
    price = r[1]
    last = con.execute("SELECT v FROM meta WHERE k=?", ("sig:" + pool,)).fetchone()
    opts = {"limit": PER_POOL_TX}
    if last:
        opts["until"] = last[0]
    sigs = rpc("getSignaturesForAddress", [pool, opts]) or []
    if sigs:
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", ("sig:" + pool, sigs[0]["signature"]))
    new = _ingest_sigs(con, pool, sigs, base_mint, price)
    con.execute("UPDATE pools SET last_trade_check=? WHERE pool=?",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), pool))
    return new


def refresh_mcap(con, pool):
    d = gt(f"/networks/solana/pools/{pool}")
    if not d or not d.get("data"):
        return None, None
    a = d["data"]["attributes"]
    mcap = _f(a.get("market_cap_usd")) or _f(a.get("fdv_usd"))
    liq = _f(a.get("reserve_in_usd"))
    con.execute("UPDATE pools SET mcap_usd=?, fdv_usd=?, price_usd=?, liquidity_usd=?, last_mcap_check=? WHERE pool=?",
                (mcap, _f(a.get("fdv_usd")), _f(a.get("base_token_price_usd")),
                 liq,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), pool))
    return mcap, liq


def backfill_oldest(con, pool, max_pages=8, keep=150):
    """Hark/Helius: page back through signatures (1000/page) to find the launch, then parse the oldest `keep` txs."""
    r = con.execute("SELECT base_mint, price_usd FROM pools WHERE pool=?", (pool,)).fetchone()
    if not r or not r[0]:
        return 0
    base_mint, price = r[0].split("_", 1)[-1], r[1]
    before, oldest = None, []
    for _ in range(max_pages):
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [pool, opts])
        if not page:
            break
        oldest = page
        before = page[-1]["signature"]
        if len(page) < 1000:
            break
    sigs = list(reversed(oldest))[:keep]
    saved = globals()["CREDIT_BUDGET"]
    globals()["CREDIT_BUDGET"] = _credits[0] + keep + 10   # runner backfill gets its own allowance
    new = _ingest_sigs(con, pool, sigs, base_mint, price)
    globals()["CREDIT_BUDGET"] = max(saved, _credits[0])
    con.execute("UPDATE pools SET backfilled=1 WHERE pool=?", (pool,))
    return new


def main():
    con = db()
    now_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 1. discovery
    fresh = 0
    for pg in (1, 2):
        d = gt(f"/networks/solana/new_pools?page={pg}")
        if not d or not d.get("data"):
            print(f"ERROR discovery page {pg} failed", flush=True)
            sys.exit(1)
        for p in d["data"]:
            cur = con.execute("SELECT pool FROM pools WHERE pool=?", (p["attributes"]["address"],)).fetchone()
            upsert_pool(con, p)
            if not cur:
                fresh += 1
    con.commit()

    # 2. watchlist: young & active, or big
    rows = con.execute("SELECT pool, created_at, first_seen, mcap_usd, txns_h24, watching FROM pools"
                       " WHERE dead=0 AND runner=0").fetchall()
    watched = []
    for pool, created_at, first_seen, mcap, txns, watching in rows:
        age = age_hours(created_at, first_seen)
        if (mcap or 0) >= WATCH_MCAP or (age <= WATCH_HOURS and (txns or 0) >= MIN_TXNS):
            watched.append((pool, age))
    watched.sort(key=lambda x: x[1])
    watched = watched[:MAX_WATCHED]
    for pool, _ in watched:
        con.execute("UPDATE pools SET watching=1 WHERE pool=?", (pool,))
    con.commit()

    # 3. trades for watchlist
    for pool, age in watched:
        pages = TRADE_PAGES if age <= 12 else 1
        record_trades(con, pool, pages)
    con.commit()

    # 4. mcap refresh (Hark: one batched GeckoTerminal call per 30 pools instead of 1 per pool)
    _now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _addrs = [p for p, _ in watched]
    for i in range(0, len(_addrs), 30):
        d = gt("/networks/solana/pools/multi/" + ",".join(_addrs[i:i + 30]))
        for p in (d or {}).get("data", []) or []:
            a = p["attributes"]
            con.execute("UPDATE pools SET mcap_usd=?, fdv_usd=?, price_usd=?, liquidity_usd=?, last_mcap_check=? WHERE pool=?",
                        (_f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")), _f(a.get("fdv_usd")),
                         _f(a.get("base_token_price_usd")), _f(a.get("reserve_in_usd")), _now_iso, a["address"]))
    con.commit()
    new_runners = []
    for pool, age in watched:
        r = con.execute("SELECT last_mcap_check, mcap_usd, name, cand_at FROM pools WHERE pool=?", (pool,)).fetchone()
        last_chk, mcap, name, cand_at = r
        stale = True
        if last_chk:
            import calendar
            try:
                stale = (time.time() - calendar.timegm(time.strptime(last_chk[:19], "%Y-%m-%dT%H:%M:%S"))) > 900
            except Exception:
                pass
        if stale:
            mcap, _liq = refresh_mcap(con, pool)
        if (mcap or 0) >= RUNNER_MCAP:
            n_buyers = con.execute(
                "SELECT COUNT(DISTINCT wallet) FROM trades WHERE pool=? AND kind='buy'", (pool,)).fetchone()[0]
            # Fresh stamp at DECISION time. now_s is script-start; step 3
            # (trade backfill) can lag it by minutes, which used to stamp
            # cand_at/runner_at early and let the 10-min rule fire early in
            # true wall-clock terms (AUTONOM 2026-10-02, GRIFFIN 2026-10-01).
            import calendar as _cal
            tick_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if not cand_at:
                con.execute("UPDATE pools SET cand_at=? WHERE pool=?", (tick_s, pool))
            else:
                try:
                    cand_age = (_cal.timegm(time.strptime(tick_s[:19], "%Y-%m-%dT%H:%M:%S"))
                                - _cal.timegm(time.strptime(cand_at[:19], "%Y-%m-%dT%H:%M:%S"))) / 60
                except Exception:
                    cand_age = 0
                # confirm: elevated for >=10 min AND real buyer breadth (>=30 wallets)
                # AND distributed buy volume (top single wallet <=50% of buy vol).
                # kills single-print spoofs like NTDA's $477K launch-second buy -> $112M FDV
                # and UDR's $591K launch-second buy -> $173M mcap (98.8% of all buy vol from one wallet).
                if cand_age >= 10 and n_buyers >= 30:
                    # Re-verify elevation on a FRESH mcap read (stagger bypass):
                    # the mcap above may be up to 15 min stale — a dead spoof
                    # print (AUTONOM: $217K on $4.3K liquidity) must not confirm.
                    # Fetch failure -> skip quietly, keep cand_at for next run.
                    fresh_mcap, fresh_liq = refresh_mcap(con, pool)
                    if fresh_mcap is None:
                        continue
                    tick_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    if fresh_mcap < RUNNER_MCAP:
                        con.execute("UPDATE pools SET cand_at=NULL WHERE pool=?", (pool,))  # spike faded / stale spoof print
                        continue
                    # Liquidity floor: kills mcap spoofs printed on dust liquidity
                    # (章鱼 2026-10-06: $381M mcap on $0.0000027 liquidity, 172 dust wallets).
                    # A real $200K+ runner needs real depth behind the price.
                    if (fresh_liq or 0) < 5000:
                        con.execute("UPDATE pools SET cand_at=NULL WHERE pool=?", (pool,))  # spoof print: no liquidity
                        continue
                    top_vol, tot_vol = con.execute(
                        "SELECT (SELECT COALESCE(SUM(vol_usd),0) FROM trades WHERE pool=? AND kind='buy' AND wallet ="
                        " (SELECT wallet FROM trades WHERE pool=? AND kind='buy' AND wallet IS NOT NULL"
                        " GROUP BY wallet ORDER BY SUM(vol_usd) DESC LIMIT 1)),"
                        " COALESCE(SUM(CASE WHEN kind='buy' THEN vol_usd ELSE 0 END),0)"
                        " FROM trades WHERE pool=?", (pool, pool, pool)).fetchone()
                    top_share = (top_vol / tot_vol) if tot_vol else 1.0
                    if top_share > 0.5:
                        con.execute("UPDATE pools SET cand_at=NULL WHERE pool=?", (pool,))  # spoof print: not a runner
                        continue
                    con.execute("UPDATE pools SET runner=1, runner_at=? WHERE pool=?", (tick_s, pool))
                    added = backfill_oldest(con, pool)
                    n_trades = con.execute("SELECT COUNT(*) FROM trades WHERE pool=?", (pool,)).fetchone()[0]
                    new_runners.append((name, pool, fresh_mcap, n_trades, added))
                    print(f"RUNNER {name} | {pool[:12]} | mcap=${fresh_mcap:,.0f} | trades={n_trades} (+{added} backfilled)", flush=True)
        elif (mcap or 0) < RUNNER_MCAP * 0.3 and cand_at:
            con.execute("UPDATE pools SET cand_at=NULL WHERE pool=?", (pool,))  # spike faded: not a runner
    con.commit()

    # 5. prune
    for pool, created_at, first_seen, mcap, txns, watching in rows:
        age = age_hours(created_at, first_seen)
        if age > PRUNE_HOURS and (mcap or 0) < WATCH_MCAP:
            con.execute("UPDATE pools SET dead=1, watching=0 WHERE pool=?", (pool,))
    con.commit()

    # stats line (quiet)
    n_pools = con.execute("SELECT COUNT(*) FROM pools").fetchone()[0]
    n_run = con.execute("SELECT COUNT(*) FROM pools WHERE runner=1").fetchone()[0]
    n_tr = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"ok pools={n_pools} runners={n_run} trades={n_tr} fresh={fresh} watched={len(watched)}", flush=True)
    if _soft_errors:
        print("soft_errors: " + "; ".join(_soft_errors[:5]), flush=True)
    con.close()


if __name__ == "__main__":
    # remove the accidental dead helper
    main()
