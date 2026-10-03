#!/usr/bin/env bash
# Stop and remove the launchd agents. Collected data is untouched.
set -euo pipefail

for label in sd.ontime.collector sd.ontime.gtfs sd.ontime.watchdog; do
  launchctl bootout "gui/$UID/$label" 2>/dev/null || echo "$label not loaded"
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
  echo "removed $label"
done
echo "collected data left in place"
