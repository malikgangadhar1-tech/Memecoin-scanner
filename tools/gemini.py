#!/usr/bin/env python3
"""Gemini helper for GitHub Actions: calls Google AI Studio directly with GEMINI_API_KEY.
In code: from gemini import ask; ask(prompt, model="gemini-3.5-flash-lite", json_mode=False)"""
import json, os, sys, time, urllib.request, urllib.error
BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT = "gemini-3.5-flash-lite"
def ask(prompt, model=DEFAULT, json_mode=False, temperature=0.2, retries=4):
    key = os.environ.get("GEMINI_API_KEY")
    if not key: raise RuntimeError("GEMINI_API_KEY not set")
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": temperature}}
    if json_mode: body["generationConfig"]["responseMimeType"] = "application/json"
    req = urllib.request.Request(f"{BASE}/models/{model}:generateContent", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r: d = json.load(r)
            return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and i < retries - 1: time.sleep(5 * (i + 1)); continue
            raise RuntimeError(f"gemini {e.code}: {e.read()[:300]}")
if __name__ == "__main__":
    print(ask(" ".join(sys.argv[1:]) or sys.stdin.read()))
