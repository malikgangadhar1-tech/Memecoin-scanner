#!/bin/bash
# Tier-1 wallet-live pipeline, one iteration. Prints alert lines (RUNNER / JUDGE_*) and status.
cd "$(dirname "$0")"
LOG=logs/pipeline_$(date -u +%Y%m%d).log; mkdir -p logs
{
echo "=== $(date -u +%FT%TZ) ==="
HELIUS_RUN_BUDGET=${WATCH_BUDGET:-150} timeout 400 python3 watch.py; echo "watch exit $?"
SW_BUDGET=${SW_BUDGET:-180} timeout 240 python3 smart_wallets.py; echo "smart_wallets exit $?"
python3 snapshot_mcap.py; echo "snapshot exit $?"
python3 exits.py
python3 buy_radar.py
python3 buy_convergence.py
} 2>&1 | tee -a "$LOG"
