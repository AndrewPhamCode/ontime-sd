#!/usr/bin/env bash
# Install the collector as a launchd agent so it runs 24/7 and restarts on
# crash. See DESIGN.md ADR-0015 for why launchd rather than cron or a cloud VM.
set -euo pipefail

LABEL="sd.ontime.collector"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/ontime-sd"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$PROJECT_DIR/scripts/$LABEL.plist.template"

UV="$(command -v uv || true)"
if [[ -z "$UV" ]]; then
  echo "error: uv not found on PATH. Install it first: brew install uv" >&2
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "error: $PROJECT_DIR/.env does not exist." >&2
  echo "       launchd passes no environment, so the collector reads .env." >&2
  echo "       Copy .env.example to .env first." >&2
  exit 1
fi

# launchd starts with a minimal PATH, so include wherever uv actually lives
# rather than assuming Homebrew.
AGENT_PATH="$(dirname "$UV"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

sed \
  -e "s|__UV__|$UV|g" \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  -e "s|__LOG_DIR__|$LOG_DIR|g" \
  -e "s|__PATH__|$AGENT_PATH|g" \
  "$TEMPLATE" > "$PLIST"

if [[ "${DRY_RUN:-}" == "1" ]]; then
  echo "DRY_RUN=1, wrote $PLIST without loading it"
  exit 0
fi

# bootout first so reinstalling picks up changes. It fails when nothing is
# loaded, which is fine.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

echo "installed $LABEL"
echo "  plist: $PLIST"
echo "  logs:  $LOG_DIR/collector.log"
echo
echo "The collector needs Postgres reachable. With the compose setup that means"
echo "Docker Desktop must be running, and set to start at login, or the"
echo "collector will back off and retry until it is."
echo
echo "Check it:    make service-status"
echo "Follow logs: make service-logs"
echo "Health:      make health"
