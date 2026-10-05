#!/usr/bin/env bash
# Move the laptop's collected history onto the cloud host.
#
# The awkward part is that both collectors are running during the move, on
# purpose: stopping the laptop first would leave a gap, and realtime data cannot
# be backfilled. So the cloud database already has rows of its own by the time
# this runs, and a plain pg_restore into it would collide.
#
# The shape that handles it: restore the dump into a staging database on the
# remote host, then merge table by table with ON CONFLICT DO NOTHING. Every
# table involved has a natural primary key, which is what makes the merge safe
# to run more than once.
#
# Derived tables are not moved. prediction_errors, segment_stats and arrivals
# are recomputed from the raw capture by `make arrivals && make compare`, and
# shipping 1.2 GB of them across is wasted time. Static GTFS is re-downloaded by
# the loader on the far side.
set -euo pipefail

HOST="${1:-}"
if [[ -z "$HOST" ]]; then
  echo "usage: $0 <ssh-target>        e.g. $0 ec2-user@52.1.2.3" >&2
  exit 2
fi

LOCAL_URL="${LOCAL_DATABASE_URL:-postgresql://ontime:ontime@localhost:5433/ontime_sd}"
DUMP="/tmp/ontime-sd-migrate-$(date +%Y%m%d%H%M%S).dump"
STAGING="ontime_sd_import"

# Raw capture, in dependency order. These are the tables that cannot be
# recollected, which is the entire reason this script exists.
TABLES=(feed_versions poll_log vehicle_positions predictions)

echo "==> checking both ends"
psql "$LOCAL_URL" -tAc 'select 1' >/dev/null
ssh "$HOST" 'sudo -n docker exec ontime-sd-postgres pg_isready -U ontime -d ontime_sd' >/dev/null
echo "    local and remote databases both reachable"

echo "==> local row counts"
for t in "${TABLES[@]}"; do
  printf '    %-20s %s\n' "$t" "$(psql "$LOCAL_URL" -tAc "select count(*) from $t")"
done

echo "==> dumping raw capture (this is the slow part)"
pg_dump "$LOCAL_URL" --format=custom --compress=9 --no-owner --no-privileges \
  $(printf -- '--table=%s ' "${TABLES[@]}") --file="$DUMP"
echo "    $(du -h "$DUMP" | cut -f1) written to $DUMP"

echo "==> shipping"
scp -q "$DUMP" "$HOST:/tmp/ontime-migrate.dump"

echo "==> restoring into staging database on the remote host"

# The remote half is shipped as a FILE and executed, not piped to `bash -s`.
#
# It used to arrive on stdin as a heredoc. `docker exec -i` inherits stdin, so
# the first psql call consumed the rest of the script, bash ran out of input,
# and the migration exited 0 having moved nothing while printing its own
# success banner. The cloud database still held two days when this claimed to
# have shipped five. A file cannot be eaten by a command that reads stdin.
REMOTE_SCRIPT="$(mktemp -t ontime-migrate-remote)"
trap 'rm -f "$REMOTE_SCRIPT"' EXIT

cat > "$REMOTE_SCRIPT" <<'REMOTE_EOF'
#!/usr/bin/env bash
# Runs on the collector host. Arguments: <staging-db> <table>...
set -euo pipefail

STAGING="$1"; shift
TABLES=("$@")

# ec2-user is deliberately not in the docker group. It already has passwordless
# sudo, so adding it would widen nothing and is one more thing to keep true
# across instance replacements.
C() { sudo -n docker exec -i ontime-sd-postgres "$@"; }

C psql -U ontime -d postgres -q -c "drop database if exists $STAGING" 2>/dev/null || true
C psql -U ontime -d postgres -q -c "create database $STAGING"
C pg_restore -U ontime -d "$STAGING" --no-owner --no-privileges < /tmp/ontime-migrate.dump
echo "    staging restored"

for t in "${TABLES[@]}"; do
  before=$(C psql -U ontime -d ontime_sd -tAc "select count(*) from $t")

  # psql meta-commands cannot be mixed into a -c string alongside SQL, so the
  # rows go through a CSV inside the container and the merge runs as a script on
  # stdin, where \copy works and the temp table survives the whole session.
  C psql -U ontime -d "$STAGING" -q -c "\copy (select * from $t) to '/tmp/mig_$t.csv' csv"
  C psql -U ontime -d ontime_sd -q -v ON_ERROR_STOP=1 <<SQL
create temp table _m (like $t including defaults);
\copy _m from '/tmp/mig_$t.csv' csv
insert into $t select * from _m on conflict do nothing;
SQL
  C rm -f "/tmp/mig_$t.csv"

  after=$(C psql -U ontime -d ontime_sd -tAc "select count(*) from $t")
  printf '    %-20s %s -> %s\n' "$t" "$before" "$after"
done

C psql -U ontime -d postgres -q -c "drop database $STAGING"
rm -f /tmp/ontime-migrate.dump
echo "    staging dropped"
REMOTE_EOF

scp -q "$REMOTE_SCRIPT" "$HOST:/tmp/ontime-migrate-remote.sh"
# </dev/null so nothing remote can read this shell's stdin either.
ssh "$HOST" "bash /tmp/ontime-migrate-remote.sh $STAGING ${TABLES[*]}" </dev/null

rm -f "$DUMP"

cat <<NOTES

==> moved. What is left to do on the remote host:

    make arrivals     rebuild inferred arrivals from the merged GPS traces
    make compare      rebuild prediction_errors, which was deliberately not shipped

Both collectors are still running. Check the cloud one has the history:

    ssh $HOST 'sudo -n docker exec ontime-sd-postgres psql -U ontime -d ontime_sd -c "select min(ts), max(ts), count(*) from vehicle_positions"'

Once that looks right, stop the laptop agents so collection is not split:

    make service-uninstall
NOTES
