#!/usr/bin/env bash
# Install the launchd agents:
#   sd.ontime.collector  realtime collection, runs continuously (ADR-0015)
#   sd.ontime.gtfs       static schedule refresh, weekly (ADR-0031)
#
# They are separate agents because the collector must never stop and the loader
# is a weekly job that finishes.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/ontime-sd"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABELS=(sd.ontime.collector sd.ontime.gtfs)

UV="$(command -v uv || true)"
if [[ -z "$UV" ]]; then
  echo "error: uv not found on PATH. Install it first: brew install uv" >&2
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "error: $PROJECT_DIR/.env does not exist." >&2
  echo "       launchd passes no environment, so both agents read .env." >&2
  echo "       Copy .env.example to .env first." >&2
  exit 1
fi

# launchd starts with a minimal PATH, so include wherever uv actually lives
# rather than assuming Homebrew.
AGENT_PATH="$(dirname "$UV"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$LOG_DIR" "$AGENTS_DIR"

for label in "${LABELS[@]}"; do
  template="$PROJECT_DIR/scripts/$label.plist.template"
  plist="$AGENTS_DIR/$label.plist"

  if [[ ! -f "$template" ]]; then
    echo "error: missing template $template" >&2
    exit 1
  fi

  sed \
    -e "s|__UV__|$UV|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    -e "s|__PATH__|$AGENT_PATH|g" \
    "$template" > "$plist"

  if [[ "${DRY_RUN:-}" == "1" ]]; then
    echo "DRY_RUN=1, wrote $plist without loading it"
    continue
  fi

  # bootout first so reinstalling picks up changes. It fails when nothing is
  # loaded, which is fine.
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$plist"
  echo "installed $label"
done

[[ "${DRY_RUN:-}" == "1" ]] && exit 0

cat <<NOTES

  logs: $LOG_DIR/collector.log
        $LOG_DIR/gtfs.log

Both agents need Postgres reachable. With the compose setup that means Docker
Desktop must be running, and set to start at login. The collector currently
exits if the database is unreachable at startup and launchd restarts it every
30 seconds until it is; feed outages after startup are handled by backoff
instead.

The schedule loader runs Sundays at 03:30. It is idempotent, so running it by
hand at any time is safe:  make load-gtfs

Check them:   make service-status
Follow logs:  make service-logs
Health:       make health
NOTES
