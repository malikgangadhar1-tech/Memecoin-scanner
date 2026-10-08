#!/usr/bin/env python3
"""Launch 4 classification shards (avoids pgrep self-match by not containing the pattern)."""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
for s in range(4):
    log = open(f"/tmp/shard{s}_v3.log", "w")
    subprocess.Popen(
        ["python3", os.path.join(HERE, "wallet_forensics.py"), "classify", str(s), "4"],
        cwd=HERE, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    print(f"launched shard {s}", flush=True)
