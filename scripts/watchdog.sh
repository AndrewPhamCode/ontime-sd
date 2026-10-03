#!/usr/bin/env bash
# Notice when collection has stopped, and say so out loud.
#
# The collector is supervised by launchd and restarts on its own, so this is not
# a restarter. It exists because the failure that actually happened was silent:
# Docker Desktop was not running, Postgres was unreachable, the collector
# crash-looped for nine hours, and nothing said anything. Realtime feed data
# cannot be backfilled, so an outage nobody notices is permanent data loss.
#
# Checks /healthz, which the collector serves and which reports 503 when no feed
# has succeeded recently. A refused connection is also a failure: it means the
# process is not up at all.
set -uo pipefail

HEALTH_PORT="${HEALTH_PORT:-8080}"
STATE_FILE="${STATE_FILE:-$HOME/Library/Logs/ontime-sd/watchdog.state}"
# Re-notify at most this often while a known outage continues, so a long outage
# does not produce a notification every five minutes.
RENOTIFY_SECONDS="${RENOTIFY_SECONDS:-3600}"
# Consecutive failures required before saying anything. The collector is
# restarted by launchd, and a reinstall or a crash restart leaves /healthz
# unanswered for a few seconds. One failed probe is therefore not an outage;
# at a five minute interval, two in a row means roughly ten minutes down.
FAILURES_BEFORE_ALERT="${FAILURES_BEFORE_ALERT:-2}"

now=$(date +%s)
stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

log() { printf '{"ts": "%s", "level": "%s", "logger": "watchdog", "message": "%s"}\n' "$stamp" "$1" "$2"; }

notify() {
  # osascript is the only dependency-free way to reach Notification Center, and
  # a GUI launchd agent can use it. Failure to notify must not fail the check.
  /usr/bin/osascript -e "display notification \"$1\" with title \"OnTime SD\" subtitle \"$2\"" >/dev/null 2>&1 || true
}

body=$(curl -fsS -m 10 "http://localhost:${HEALTH_PORT}/healthz" 2>/dev/null)
curl_status=$?

if [[ $curl_status -eq 0 ]]; then
  healthy=1
  detail="collector healthy"
else
  healthy=0
  if [[ $curl_status -eq 22 ]]; then
    detail="collector is up but reports stale feeds (503)"
  else
    detail="collector is not answering on :${HEALTH_PORT}"
  fi
fi

# Read the previous state so notifications fire on transitions, not every run.
prev_state="unknown"
prev_notified=0
prev_failures=0
if [[ -f "$STATE_FILE" ]]; then
  read -r prev_state prev_notified prev_failures < "$STATE_FILE" 2>/dev/null || true
fi
prev_failures=${prev_failures:-0}

if [[ $healthy -eq 1 ]]; then
  if [[ "$prev_state" == "down" ]]; then
    log INFO "collection recovered"
    notify "Collection has recovered." "Recovered"
  fi
  echo "ok 0 0" > "$STATE_FILE"
  exit 0
fi

failures=$(( prev_failures + 1 ))

# Unhealthy. The known cause is the database being unreachable because Docker
# Desktop is not running, so check that specifically and say which it is.
cause="$detail"
if ! docker info >/dev/null 2>&1; then
  cause="$detail; the Docker engine is also down, so Postgres is unreachable"
fi

if [[ $failures -lt $FAILURES_BEFORE_ALERT ]]; then
  # Probably a restart. Record it and wait for the next check to decide.
  log WARN "$cause (probe $failures of $FAILURES_BEFORE_ALERT, not alerting yet)"
  echo "$prev_state $prev_notified $failures" > "$STATE_FILE"
  exit 0
fi

log ERROR "$cause"

elapsed=$(( now - prev_notified ))
if [[ "$prev_state" != "down" || $elapsed -ge $RENOTIFY_SECONDS ]]; then
  notify "$cause" "Collection has stopped"
  echo "down $now $failures" > "$STATE_FILE"
else
  echo "down $prev_notified $failures" > "$STATE_FILE"
fi
exit 1
