#!/usr/bin/env python3
"""Memecoin dispatcher (Oct 7 2026 23:21 IST efficiency refactor). One scheduled task runs this every 10 min.
Steps, each with its own timeout and crash isolation, in order:
  1 revival     scanner/revival.py            -> "Revival watch" B (dev sold at loss) / C (side wallet sold)
  2 pool        scripts/new_pool_filter.py --commit (+ outcome tracking of data/alert_outcomes.csv, in code)
  3 band        scripts/pump_band_watch.py    -> pump.fun coins entering $15-30K (Rohit yes, Oct 7 23:19)
  4 kol         scripts/kol_convergence.py --mark (5 KOL wallets, 3+ distinct buyers in 24h)
  5 walletlive  engine/run_pipeline.sh        -> SW_BUY_CONVERGENCE / RUNNER / JUDGE_* lines
Prints ONLY ready-to-send alert text, or nothing. Diagnostics -> engine/logs/tick.log.
Helius: wallet-live used 150+180 credits per 15-min run; at 10 min it runs with 100+120 (same credits per hour, inside free 1M/mo).
--dry: don't commit dedupe state for pool/kol (revival/band/walletlive have their own ledgers and always commit)."""
import json, os, re, subprocess, sys, time, csv, urllib.request, traceback
from datetime import datetime, timezone, timedelta
P = os.environ.get("SCANNER_ROOT", "/workspace/projects/7ed675e0-9a24-4f62-9d59-e27bf0c6ccd0")
FAST = os.environ.get("FAST_V3") == "1"  # fast_v3.py loop: skip slow GT outcome tracking + wallet-confirm (Oct 10)
IST = timezone(timedelta(hours=5, minutes=30))
DRY = "--dry" in sys.argv
ONLY = sys.argv[sys.argv.index("--only") + 1].split(",") if "--only" in sys.argv else None
LOG = open(f"{P}/engine/logs/tick.log", "a")
def log(*a): LOG.write(datetime.now(IST).strftime("%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n"); LOG.flush()
def run(cmd, timeout, cwd=P, env=None):
    e = dict(os.environ); e.update(env or {})
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=e)
    if r.returncode != 0: log("EXIT", r.returncode, cmd, r.stderr[-1500:])
    return r.stdout
def lastjson(s):
    i = s.find("{")
    return json.loads(s[i:]) if i >= 0 else {}
def ist(ts): return datetime.fromtimestamp(ts, IST).strftime("%H:%M")
def usd(x):
    x = float(x or 0)
    return f"${x/1000:.1f}K" if x < 1e6 else f"${x/1e6:.2f}M"
def get(u):
    try: return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}), timeout=20))
    except Exception as e: log("GET fail", u, e); return None
out = []
def step_revival():
    j = lastjson(run(["python3", "revival.py"], 480, cwd=f"{P}/scanner"))
    lines = []
    for a in j.get("alerts", []):
        if a["signal"] == "Dev sold at a loss":
            moved = a.get("dev_exit") == "MOVED"
            head = "Dev MOVED tokens, got 0 SOL" if moved else "Dev SOLD at a loss"
            soc = " ".join(l for l in (a.get("socials") or []) if l and ("x.com" in l or "twitter" in l or "t.me" in l)) or "no X/TG"
            age = f"{a['age_h']}h" if (a.get("age_h") or 0) >= 1 else f"{round((a.get('age_h') or 0)*60)}m"
            l = (f"{head} — {a['symbol']} ({a.get('name','')}) — dev out {a.get('dev_exit_min')} min after launch — MC {usd(a['mc'])} — "
                 f"holders {a.get('holders')}, 1h buyers {a.get('buyers_1h')} — dev in {round(a.get('dev_sol_in') or 0,2)} SOL, out {round(a.get('dev_sol_out') or 0,2)} SOL — "
                 f"1h buys/sells {a.get('h1_buys')}/{a.get('h1_sells')} — age {age} — {soc} — {a['dexscreener']} — {a['mint']}")
            if moved: l += " — watching the receiving wallet"
            lines.append(l)
        elif a["signal"] == "Side wallet sold":
            lines.append(f"Side wallet SOLD — {a['symbol']} — the wallet that got the dev's tokens dumped {a['sold_pct']}% — MC {usd(a['mc'])} — {a['dexscreener']} — wallet {a['wallet'][:8]}")
    if lines: _tl.out.append("Revival watch\n" + "\n".join(lines))

def track_outcomes():
    f = f"{P}/data/alert_outcomes.csv"
    if not os.path.exists(f): return
    rows = list(csv.DictReader(open(f))); fields = rows[0].keys() if rows else None
    if not rows: return
    now = time.time(); n = 0
    def parse(s):
        m = re.search(r"(\d{4}-\d\d-\d\d) (\d+):(\d\d) (AM|PM)", s or "")
        if m:
            h = int(m[2]) % 12 + (12 if m[4] == "PM" else 0)
            return datetime.strptime(f"{m[1]} {h}:{m[3]}", "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp()
        try: return datetime.fromisoformat(s).timestamp()
        except Exception: return None
    for r in rows:
        t = parse(r["alerted_at"])
        if not t: continue
        due = [c for c, h in (("mcap_1h", 1), ("mcap_6h", 6), ("mcap_24h", 24)) if now - t >= h * 3600 and not r.get(c)]
        if not due or n >= 5: continue
        j = get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{r['pool']}"); n += 1; time.sleep(6)
        if not j: continue
        a = j["data"]["attributes"]; mc = float(a.get("market_cap_usd") or a.get("fdv_usd") or 0); liq = float(a.get("reserve_in_usd") or 0)
        for c in due: r[c] = round(mc)
        r["peak_mcap_seen"] = round(max(mc, float(r.get("peak_mcap_seen") or 0)))
        if r.get("mcap_24h") and not r.get("verdict"):
            am = float(r["alert_mcap"] or 1)
            r["verdict"] = "GOOD" if float(r["peak_mcap_seen"]) >= 2 * am else ("BAD" if float(r["mcap_24h"]) < 0.5 * am or liq < 1000 else "FLAT")
    w = csv.DictWriter(open(f, "w", newline=""), fieldnames=list(fields)); w.writeheader(); w.writerows(rows)

def step_pool():
    if not DRY and not FAST:
        try: track_outcomes()
        except Exception: log("CRASH outcomes", traceback.format_exc()[-800:])
    j = lastjson(run(["python3", "scripts/new_pool_filter.py"] + ([] if DRY else ["--commit"]), 400))
    lines = []
    for p in j.get("passing", []):
        if not DRY:
            f = f"{P}/data/alert_outcomes.csv"; new = not os.path.exists(f)
            with open(f, "a", newline="") as fh:
                w = csv.writer(fh)
                if new: w.writerow("pool,ticker,alerted_at,alert_mcap,alert_liq,alert_buyers,alert_buys,alert_sells,mcap_1h,mcap_6h,mcap_24h,peak_mcap_seen,verdict".split(","))
                w.writerow([p["addr"], p.get("symbol"), datetime.now(IST).isoformat(timespec="seconds"), round(p["mcap"]), round(p["liq"]), p["buyers"], p["buys"], p["sells"], "", "", "", "", ""])
            try:
                with open(f"{P}/data/v3_alerts.jsonl", "a") as fh:
                    fh.write(json.dumps({"t": time.time(), "mint": p.get("mint"), "pool": p["addr"], "sym": p.get("symbol"), "mc": p["mcap"], "mc_fresh": p.get("mc_fresh"), "mc_fresh_t": p.get("mc_fresh_t"),
                        # full setup snapshot for the lab notebook (Rohit Oct 8 16:34: "need the setup info")
                        **{k: p.get(k) for k in ("liq", "buyers", "buys", "sells", "sellers", "avg", "age_min", "buyers_5m", "buyers_prev5m", "buyers_half_early", "buyers_half_late", "half_window_s", "largest_bundle_5m", "socials", "dev_pct", "top10_pct", "tag", "creator", "farm_dev")}}) + "\n")
            except Exception: pass
        soc = {True: "socials yes", False: "socials no"}.get(p.get("socials"), "socials unknown")
        conc = " (sells concentrated)" if p["sellers"] and p["sells"] / p["sellers"] > 3 else ""
        tag = f"{p['tag']} — " if p.get("tag") else ""
        if p.get("farm_dev"): tag = "🚜 FARM DEV — " + tag  # Rohit Oct 8 22:46: label only (top-25 pump.fun farm deployers)
        if (p.get("age_min") is not None) and p["age_min"] < 10: tag = "⭐ FRESH <10m — " + tag  # Rohit Oct 8 16:10: label only (49% vs 30%)
        if p.get("dev_pct") is not None or p.get("top10_pct") is not None:
            dv = p.get("dev_pct"); flag = " ⚠️DEV>1%" if (dv or 0) > 1 else ""
            # Rohit Oct 9 12:17 "go": dev-out as a confirmation stage, label only, graded apart (in-sample dev out 23/39 vs holding 4/14)
            dstate = "✅ DEV OUT" if dv == 0 else (f"DEV HOLDING {dv}%{flag}" if dv is not None else "dev ?%")
            tag += f"{dstate} / top10 {p.get('top10_pct') if p.get('top10_pct') is not None else '?'}% — "
        lines.append(f"{p.get('symbol')} — {tag}mcap {usd(p['mc_fresh']) + ' now (scan ' + usd(p['mcap']) + ')' if p.get('mc_fresh') else usd(p['mcap'])} — liq {usd(p['liq'])} — {p['buyers']} buyers / {p['buys']} buys / {p['sells']} sells{conc} — "
                     f"{p.get('buyers_5m')} buyers in last 5 min{accel(p)} — {p['age_min']}m old, launched {p['created_ist']} — {soc} — https://dexscreener.com/solana/{p['addr']}")
    if lines: _tl.out.append("\n".join(lines))

def accel(p):
    # Label only (Rohit Oct 8 16:34): buyers in the 5 min before that, arrow = speeding up / slowing down
    pv, b5 = p.get("buyers_prev5m"), p.get("buyers_5m")
    if pv is not None and b5 is not None:
        return f" (prev 5 min {pv} {'↑' if b5 > pv else ('↓' if b5 < pv else '=')})"
    e, l, w = p.get("buyers_half_early"), p.get("buyers_half_late"), p.get("half_window_s")
    if e is None or l is None or not w: return ""
    arrow = "↑ speeding up" if l > 1.15 * e else ("↓ slowing" if l < 0.85 * e else "= steady")
    return f" ({arrow}: {e}→{l} buyers per {round(w/60,1)}m)"

def step_band():
    j = lastjson(run(["python3", "scripts/pump_band_watch.py", "--seconds", "20"], 120))
    lines = [f"pump.fun band: {'🚜 FARM DEV — ' if a.get('farm_dev') else ''}{a['symbol']} ({a.get('name','')}) — mcap {usd(a['mc'])} — {a['buyers5']} buyers / {a['buys5']} buys / {a['sells5']} sells in last 5 min, ${a['buyvol5']:,} bought — "
             f"age {a['age_min']}m{' — bonded' if a.get('bonded') else ''} — {a['link']}" for a in j.get("alerts", [])]
    if lines: _tl.out.append("\n".join(lines))

def step_kol():
    j = lastjson(run(["python3", "scripts/kol_convergence.py"] + ([] if DRY else ["--mark"]), 150, env={"BUDGET": "90"}))
    for h in j.get("hits", []):
        m = h["mint"]; d = get(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{m}")
        a = (d or {}).get("data", {}).get("attributes", {}); sym = a.get("symbol") or m[:6]
        mc = a.get("market_cap_usd") or a.get("fdv_usd"); liq = a.get("total_reserve_in_usd")
        who = ", ".join(f"{k} {ist(t)}" for k, t in sorted(h["kols"].items(), key=lambda x: x[1]))
        _tl.out.append(f"KOL convergence: {sym} ({a.get('name','')}) — {len(h['kols'])} KOLs bought: {who} IST — mcap {usd(mc)} — liq {usd(liq)} — https://dexscreener.com/solana/{m} — {m}")
        if not DRY:
            kf = f"{P}/grading/kol_alerts.json"; L = json.load(open(kf)) if os.path.exists(kf) else []
            L.append([m, time.time(), sym]); json.dump(L, open(kf, "w"))

SW = re.compile(r"SW_BUY_CONVERGENCE (\S+)(.*?) \| (\d+) judged wallets \| scores=(\S+) \| tier=(\S+) \| sol=(\S+) \| last buy (\S+) IST \| mcap=(\S+) liq=(\S+) \| coord=(\S+) \| (\S+)")
def step_walletlive():
    s = run(["bash", "engine/run_pipeline.sh"], 560, env={"WATCH_BUDGET": "100", "SW_BUDGET": "120"})
    sw, other = [], []
    for l in s.splitlines():
        m = SW.match(l)
        if m:
            sym, extra, n, sc, tier, sol, t, mc, liq, coord, link = m.groups()
            tier = "MIXED (new)" if tier == "MIXED" else tier
            sw.append(f"Smart wallets: {sym}{extra} — {n} wallets ({sc}; R = Muse runner catcher, I = independent trader) — tier {tier} — {sol} SOL — last buy {t} IST — mcap {mc} / liq {liq} — coord {coord} — {link}")
        elif re.match(r"(RUNNER|JUDGE_EXIT_CLUSTER|JUDGE_CONVERGENCE|JUDGE_TOP_BUY|JUDGE_BUY_CONVERGENCE)\b", l):
            other.append(l.strip())
    if sw or other: _tl.out.append("\n".join(sw + other))

from concurrent.futures import ThreadPoolExecutor
# revival (dev sold at a loss + side wallet sold) TERMINATED by Rohit Oct 8 16:34 IST: "needs way more discretion, I'll look at it later" (3/31 clean 2x)
STEPS = [("pool", step_pool)]  # walletlive (SW) TERMINATED by Rohit Oct 10 20:17 IST "we don't need sw anyways" (it delayed v3 pings ~418s)  # kol TERMINATED by Rohit Oct 10 12:41 IST (3/24 clean)
T0 = time.time()
import threading
_tl = threading.local()
res = {}
def wrap(name, fn):
    t = time.time(); mine = []
    global out
    try:
        fn_out = []
        _tl.out = fn_out
        fn()
    except subprocess.TimeoutExpired: log("TIMEOUT", name)
    except Exception: log("CRASH", name, traceback.format_exc()[-2000:]); print(f"# STEP CRASHED: {name} (see engine/logs/tick.log)", file=sys.stderr)
    log("step", name, f"{time.time()-t:.0f}s")
    res[name] = getattr(_tl, "out", [])
sel = [(n, f) for n, f in STEPS if not ONLY or n in ONLY]
with ThreadPoolExecutor(len(sel)) as ex:
    for n, f in sel: ex.submit(wrap, n, f)
out = [x for n, _ in sel for x in res.get(n, [])]

# "Wallet confirmed" 2nd ping (Rohit Oct 8 16:10, combining edges): a judged/extra SW wallet buys a v3 coin within 60 min of its ping.
def wallet_confirm():
    import sqlite3, csv as _csv
    vf = f"{P}/data/v3_alerts.jsonl"
    if not os.path.exists(vf): return []
    now = time.time(); V = []
    for l in open(vf):
        try: a = json.loads(l)
        except Exception: continue
        if a.get("mint") and now - a["t"] <= 3600: V.append(a)
    if not V: return []
    df = f"{P}/data/wallet_confirmed.json"; done = set(json.load(open(df))) if os.path.exists(df) else set()
    con = sqlite3.connect(f"{P}/engine/watch.db", timeout=30)
    good = {w for (w,) in con.execute("select wallet from judge_scores where score>=65")}
    try: good |= {r["wallet"] for r in _csv.DictReader(open(f"{P}/engine/extra_wallets.csv"))}
    except Exception: pass
    bad = set()
    for fn in ("sw_blocklist.json", "logs/sw_demoted.json"):
        try: bad |= set(json.load(open(f"{P}/engine/{fn}")))
        except Exception: pass
    try: bad |= {l.split()[0] for l in open(f"{P}/engine/sw_blocklist.txt") if l.strip() and not l.startswith("#")}
    except Exception: pass
    lines = []
    for a in V:
        if a["mint"] in done: continue
        rows = [r for r in con.execute("select wallet, ts, sol from sw_trades where mint=? and kind='buy' and sol>=0.05 and ts>=? and ts<=? order by ts", (a["mint"], int(a["t"]) - 120, int(a["t"]) + 3600)) if r[0] in good and r[0] not in bad]
        if not rows: continue
        done.add(a["mint"]); w, ts, sol = rows[0]; nw = len({r[0] for r in rows})
        mc = 0
        try:
            ps = json.load(urllib.request.urlopen(urllib.request.Request("https://api.dexscreener.com/tokens/v1/solana/" + a["mint"], headers={"User-Agent": "Mozilla/5.0"}), timeout=15))
            if ps: mc = max(x.get("marketCap") or 0 for x in ps)
        except Exception: pass
        if not DRY:
            with open(f"{P}/data/wallet_confirmed.jsonl", "a") as fh:
                fh.write(json.dumps({"t": now, "buy_ts": ts, "mint": a["mint"], "pool": a["pool"], "sym": a.get("sym"), "v3_mc": a["mc"], "mc": mc, "wallets": sorted({r[0] for r in rows})}) + "\n")
        ist = datetime.fromtimestamp(ts, IST).strftime("%H:%M")
        lines.append(f"✅ Wallet confirmed: {a.get('sym')} — {nw} smart wallet{'s' if nw > 1 else ''} bought ({w[:6]} {sol:.2f} SOL at {ist} IST, {max(0, round((ts - a['t']) / 60))} min after v3 ping) — v3 ping mcap {usd(a['mc'])} → now {usd(mc) if mc else '?'} — https://dexscreener.com/solana/{a['pool']}")
    if not DRY: json.dump(sorted(done), open(df, "w"))
    return lines
try:
    _wc = [] if FAST else wallet_confirm()
    if _wc: out.append("\n".join(_wc))
except Exception: log("CRASH wallet_confirm", traceback.format_exc()[-800:])

# Narrative LABEL via Rohit's Gemini (Flash-Lite, his key, not the Hark pool). One batched call per tick, label only:
# it never adds, drops or reorders an alert. Daily cap 400 calls (free tier 450/day). Any failure = no label.
def narrate(blocks):
    lines = [l for b in blocks for l in b.split("\n") if " — " in l and ("pump.fun band:" in l or "buyers in last 5 min" in l)]
    if not lines: return blocks
    uf = f"{P}/data/gemini_usage.json"; day = datetime.now(IST).strftime("%Y-%m-%d")
    u = json.load(open(uf)) if os.path.exists(uf) else {}
    if u.get(day, 0) >= 400: return blocks
    u = {day: u.get(day, 0) + 1}; json.dump(u, open(uf, "w"))
    try:
        sys.path.insert(0, "/workspace/tools"); from gemini import ask
        prompt = ("Solana memecoin alerts, one per line. For each line return a 2-4 word narrative label for the coin "
                  "(the meme, trend or news it rides, e.g. 'Elon/X meme', 'AI agent', 'animal meme', 'political'), and add ' (copycat)' "
                  "if the same ticker or name appears on more than one line. Reply as JSON: {\"labels\": [label per line, same order]}.\n\n" + "\n".join(lines))
        lab = json.loads(ask(prompt, model="gemini-3.5-flash-lite", json_mode=True)).get("labels") or []
        if len(lab) != len(lines): log("gemini label count mismatch", len(lab), len(lines)); return blocks
        m = dict(zip(lines, lab))
        try:
            with open(f"{P}/data/narratives.jsonl", "a") as fh:
                for l, x in m.items(): fh.write(json.dumps({"t": time.time(), "line": l[:120], "label": x}) + "\n")
        except Exception: pass
        return ["\n".join(l + (f" — narrative: {str(m[l])[:40]}" if l in m and m[l] else "") for l in b.split("\n")) for b in blocks]
    except Exception as e:
        log("gemini fail", e); return blocks
if out and "--no-ai" not in sys.argv: out = narrate(out)
log("tick done", f"{time.time()-T0:.0f}s", "alerts" if out else "silent")
if out: print("\n\n".join(out))
