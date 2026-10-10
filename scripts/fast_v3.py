#!/usr/bin/env python3
"""fast_v3.py: run the v3 pool scanner back-to-back (one scan about every 3-5 min instead of GitHub's 10-min cron)
and push each alert straight to Telegram. Same v3 rules, untouched (scripts/new_pool_filter.py via tick.py --only pool).

Setup (any always-on machine with Python 3.9+, no pip installs needed):
  1. Copy the whole repo folder (malikgangadhar1-tech/Memecoin-scanner) onto the machine.
  2. Set env vars:  TG_TOKEN=<bot token from @BotFather>   TG_CHAT=<your chat id>
  3. Run from the repo folder:   python3 scripts/fast_v3.py
Every alert is also appended to data/v3_alerts.jsonl (with timestamp + MC) so Hark can grade the fast version.
Turn OFF the GitHub v3 step while this runs, or you get every coin twice. Signals only: never trades.
"""
import json, os, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IST = timezone(timedelta(hours=5, minutes=30))
TOKEN, CHAT = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
MIN_GAP = int(os.environ.get("MIN_GAP_S", "60"))  # never start scans closer than this (GeckoTerminal free limit)
LOG = os.path.join(ROOT, "data", "fast_v3.log")

def log(msg):
    line = datetime.now(IST).strftime("%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a") as fh: fh.write(line + "\n")

def tg(text):
    if not (TOKEN and CHAT): log("no TG_TOKEN/TG_CHAT set; alert only logged"); return
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        data = urllib.parse.urlencode({"chat_id": CHAT, "text": chunk, "disable_web_page_preview": "true"}).encode()
        for i in range(3):
            try: urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data, timeout=15); break
            except Exception as e: log(f"telegram fail {e}"); time.sleep(3)

def main():
    for d in ("data", "engine/logs"): os.makedirs(os.path.join(ROOT, d), exist_ok=True)
    env = dict(os.environ, SCANNER_ROOT=ROOT, FAST_V3="1")
    log(f"fast_v3 started, root {ROOT}")
    while True:
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, "scripts/tick.py", "--only", "pool", "--no-ai"], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=420)
            out = r.stdout.strip()
            if out:
                stamp = datetime.now(IST).strftime("%H:%M:%S IST")
                tg(f"v3 {stamp}\n{out}")
                log(f"ALERT sent ({out.count(chr(10)) + 1} lines)")
            if r.returncode != 0: log("scan exit " + str(r.returncode) + " " + r.stderr[-500:])
        except subprocess.TimeoutExpired: log("scan timeout")
        except Exception as e: log(f"loop error {e}")
        took = time.time() - t0
        log(f"scan took {took:.0f}s")
        time.sleep(max(0, MIN_GAP - took))

if __name__ == "__main__":
    main()
