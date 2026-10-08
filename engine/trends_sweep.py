#!/usr/bin/env python3
"""trends_sweep.py - ask Gemini what's going viral right now, append to trending_phrases.txt.
Run 2x daily. Uses Rohit's Gemini Pro plan via burn_ledger (single credential:
custom.gemini2, the sole survivor after the 2026-10-05 disconnects), zero Muse tokens.
Replaces trends_sweep.sh (2026-10-05: bash CLI was single-credential)."""
import os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "trending_phrases.txt")
sys.path.insert(0, os.path.expanduser("~/workspace/token-burn"))
from burn_ledger import generate_logged

PROMPT = ("List the top 10 most viral right now (last 24-48 hours) stories, memes, "
          "hashtags, cultural moments, or controversies on TikTok, X/Twitter, and "
          "Instagram that memecoin creators are likely to name coins after. Focus on "
          "things with mass posting behavior (solidarity waves, scandals, viral videos, "
          "celebrity moments). Output ONLY one short phrase per line, no numbering, "
          "no commentary, no quotes. Keep each phrase 1-4 words.")

text, model = generate_logged(PROMPT, source="narrative-trends-sweep")
if not text:
    print(f"sweep failed: {model}", file=sys.stderr)
    sys.exit(1)

lines = [l.strip().strip('"').strip("'") for l in text.splitlines()]
lines = [l for l in lines if l and len(l.split()) <= 6]
if len(lines) < 3:
    print("sweep failed: too few phrases", file=sys.stderr)
    sys.exit(1)

existing = set()
if os.path.exists(OUT):
    with open(OUT) as f:
        existing = set(l.strip().lower() for l in f if l.strip() and not l.startswith("#"))

new = [l for l in lines if l.lower() not in existing]
ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
with open(OUT, "a") as f:
    f.write(f"\n# sweep {ts} (model {model})\n")
    for l in new:
        f.write(l + "\n")
print(f"sweep done: {ts} ({len(new)} new phrases, model {model})")
