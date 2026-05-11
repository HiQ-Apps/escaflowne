#!/bin/bash
#
# gui/sync_state.sh — Escaflowne
# Pull state_escaflowne.json from VPS to local Mac for menu bar to read.
#
# Adapted from Celeri's sync_state.sh. Differences:
#   - Remote path: Desktop/escaflowne/state_escaflowne.json
#   - Local target: project_root/state_escaflowne.json
#
# Can be run two ways:
#
# Manually (foreground, see output):
#     ./gui/sync_state.sh
#
# Automatically (background via launchd):
#     Drop com.escaflowne.statesync.plist into ~/Library/LaunchAgents/
#     and launchctl load it. See plist file for setup steps.
#
# Press Ctrl+C to stop (manual mode only).

REMOTE_HOST="melancholi@172.208.66.116"
REMOTE_PATH="Desktop/escaflowne/state_escaflowne.json"
LOCAL_PATH="$(cd "$(dirname "$0")/.." && pwd)/state_escaflowne.json"

echo "Escaflowne state sync"
echo "  Remote: $REMOTE_HOST:$REMOTE_PATH"
echo "  Local:  $LOCAL_PATH"
echo "  Interval: 1 second"
echo ""
echo "Press Ctrl+C to stop."
echo ""

failures=0
while true; do
    if scp -q -o ConnectTimeout=3 -o ServerAliveInterval=5 \
        "${REMOTE_HOST}:${REMOTE_PATH}" \
        "${LOCAL_PATH}" \
        2>/dev/null; then
        if [ $failures -gt 0 ]; then
            echo "  [$(date +%H:%M:%S)] sync recovered"
            failures=0
        fi
    else
        failures=$((failures + 1))
        if [ $((failures % 10)) -eq 1 ]; then
            echo "  [$(date +%H:%M:%S)] sync error (attempt $failures)"
        fi
    fi
    sleep 1
done