#!/usr/bin/env bash
# Stop and remove the launchd agent. Collected data is untouched.
set -euo pipefail

LABEL="sd.ontime.collector"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || echo "not loaded"
rm -f "$PLIST"
echo "removed $LABEL (collected data left in place)"
