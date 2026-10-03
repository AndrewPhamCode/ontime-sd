#!/usr/bin/env bash
# Install the launchd agents:
#   sd.ontime.collector  realtime collection, runs continuously (ADR-0015)
#   sd.ontime.gtfs       static schedule refresh, weekly (ADR-0031)
#   sd.ontime.watchdog   notices when collection has stopped (ADR-0040)
#
# They are separate agents because they have different lifetimes: the collector
# must never stop, the loader is a weekly job that finishes, and the watchdog is
# a periodic check.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/ontime-sd"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABELS=(sd.ontime.collector sd.ontime.gtfs sd.ontime.watchdog)

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

  # bootout returns before launchd has finished unloading the job, and
  # bootstrapping into that window fails with "5: Input/output error" and leaves
  # the service down. That is worse than not reinstalling at all, so wait for
  # the old job to actually disappear before loading the new one.
  for _ in $(seq 1 50); do
    launchctl print "gui/$UID/$label" >/dev/null 2>&1 || break
    sleep 0.2
  done

  # Retry once anyway: the wait above closes the common race but launchd can
  # still refuse transiently.
  if ! launchctl bootstrap "gui/$UID" "$plist" 2>/dev/null; then
    sleep 1
    if ! launchctl bootstrap "gui/$UID" "$plist"; then
      echo "error: could not load $label. It is NOT running." >&2
      echo "       retry with: launchctl bootstrap gui/$UID $plist" >&2
      exit 1
    fi
  fi

  # Never report success without checking, since a silent failure here means
  # collection has stopped.
  if ! launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
    echo "error: $label bootstrapped but is not registered. It is NOT running." >&2
    exit 1
  fi
  echo "installed $label"
done

[[ "${DRY_RUN:-}" == "1" ]] && exit 0

cat <<NOTES

  logs: $LOG_DIR/collector.log
        $LOG_DIR/gtfs.log
        $LOG_DIR/watchdog.log

The collector and the loader need Postgres reachable. With the compose setup
that means Docker Desktop must be running and set to start at login, which is
what its AutoStart setting controls. The collector exits if the database is
unreachable at startup and launchd restarts it every 30 seconds until it is;
feed outages after startup are handled by backoff instead.

The schedule loader runs Sundays at 03:30. It is idempotent, so running it by
hand at any time is safe:  make load-gtfs

The watchdog checks /healthz every five minutes and posts a notification when
collection stops or recovers. It does not restart anything; launchd already
does that. It is there so an outage is noticed, because realtime data cannot
be backfilled.

Check them:   make service-status
Follow logs:  make service-logs
Health:       make health
NOTES
