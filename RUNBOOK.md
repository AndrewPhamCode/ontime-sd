# RUNBOOK

Operating the OnTime SD collector. Written for the person on call, which for now
is Andrew.

## What this is

Two launchd agents on Andrew's Mac.

`sd.ontime.collector` polls two MTS GTFS-Realtime feeds every 30 seconds and
writes to Postgres. It is expected never to stop, because Phase 5 needs weeks of
history and history cannot be backfilled.

`sd.ontime.gtfs` loads the static schedule, Sundays at 03:30. It is a job that
finishes, and it is deliberately a separate agent so that a loader failure or a
malformed feed cannot disturb collection. See ADR-0031.

- Vehicle positions go to `vehicle_positions`, stored losslessly.
- Official MTS predictions go to `predictions`, stored change-only.
- Every poll attempt, including skips and failures, goes to `poll_log`.
- The static schedule goes to the GTFS tables, keyed by `feed_version`, and every
  load attempt goes to `gtfs_load_log`.

Database: Postgres 16 in Docker Compose on host port 5433. Docker Desktop must be
running, and set to start at login, or the collector cannot reach it.

## First checks, in order

```
make service-status     # is launchd running them
make health             # what the collector thinks of itself
make coverage           # what the realtime data says actually happened
make schedule           # which feed versions are loaded, and recent load runs
make service-logs       # JSON logs, one object per line
```

`make coverage` is the most informative of the four. The process can be running
and healthy while collecting nothing useful, and only `poll_log` shows that.

## Symptoms

### /healthz returns 503

Means no feed has succeeded in the last 5 minutes. The process is alive, since
something answered, so this is a feed, network, or database problem.

1. `make coverage` and look at the last poll per feed and the status breakdown.
2. If the last statuses are `http_error`, check whether MTS is reachable at all:
   `curl -sS -o /dev/null -w '%{http_code}\n' "$MTS_FEED_BASE_URL/vehicle-positions-for-agency/MTS.pb?key=$MTS_API_KEY"`
3. If they are `db_error`, check Docker Desktop is running and `make up`.
4. Backoff caps at 5 minutes, so the collector recovers on its own within 5
   minutes of the underlying problem clearing. Do not restart it to force
   recovery: a restart drops the in memory prediction cache and causes a burst of
   redundant writes on the next poll.

### The service is not running at all

```
make service-status     # prints "not loaded" if launchd does not have it
make service-install    # installs and starts it
```

If launchd has it but it keeps exiting, the log tells you why. `ThrottleInterval`
is 30 seconds, so a crash loop restarts twice a minute rather than continuously.
The most common cause is `.env` missing or `DATABASE_URL` wrong, since launchd
passes no environment and the process reads `.env` from the project directory.

### Gaps in poll_log

Expected on a laptop. The Mac sleeping, losing network, or Docker not being up
yet after login all produce gaps, and `make coverage` lists any over two minutes.

The collector is wrapped in `caffeinate -si` by its launchd agent, so it holds a
sleep assertion for its whole life. Verify with:

```
pmset -g assertions | grep -E 'PreventSystemSleep|PreventUserIdleSystemSleep'
```

Both should read 1. If they do not, the agent is not running the wrapped command:
reinstall with `make service-install`.

Even with the assertion held, two things still cause gaps and neither can be
fixed from user space:

- **Closing the lid** sleeps the machine regardless.
- **Running on battery** means only the `-i` assertion applies; `-s` is honoured
  on AC power only.

So for full coverage the machine stays open and plugged in. Measured cost of not
doing this: coverage fell to 35%, losing about 15 hours of data per day. Also keep
Docker Desktop set to start at login, or the collector cannot reach the database.

Moving to an always on host is the real fix, per ADR-0015.

Do not backfill a gap. There is no source to backfill from: realtime data not
captured is gone.

### parse_error appears

Take this seriously. It means MTS returned a 200 with bytes that are not a
parseable `FeedMessage`. Either the response was truncated, or the feed format
changed.

1. Fetch the human readable version and look at it:
   `curl -sS "$MTS_FEED_BASE_URL/trip-updates-for-agency/MTS.pbtext?key=$MTS_API_KEY" | head -50`
2. If it parses fine by hand, the earlier failure was a truncated response and
   is transient.
3. If the shape has changed, the parser in `ontime_sd/feeds.py` needs updating.
   Collection of the other feed continues meanwhile, since the feeds are
   independent.

### http_error spike

Check the status codes in `poll_log.http_code`.

- 401 or 403: the API key is wrong, expired, or revoked. Check `.env`.
- 429: we are polling too fast for what MTS allows. Raise
  `POLL_INTERVAL_SECONDS` and note it, because a longer interval reduces
  sampling resolution for Phase 3.
- 500 or 502 or 503: MTS side. Backoff handles it. Nothing to do but confirm
  recovery.
- No status code recorded at all: a transport failure, meaning DNS, network, or
  connection refused. Check local connectivity first.

### predictions is growing much faster than expected

Change-only storage should keep this table proportional to how often MTS revises
predictions, not to how often we poll. `make coverage` prints predictions written
against successful polls.

If the ratio looks closer to "every stop time every poll", the in memory cache is
not suppressing repeats. Likely causes: the process is restarting repeatedly, so
the cache is always cold, or `PREDICTION_CHANGE_THRESHOLD_SECONDS` was set to 0.
Check `poll_log` for a restart pattern and check `.env`.

### Disk filling up

Check row counts first:

```
make psql
select count(*) from vehicle_positions;
select count(*) from predictions;
select pg_size_pretty(pg_total_relation_size('vehicle_positions'));
```

Vehicle positions are the bulk of it and are stored losslessly on purpose, since
they are the ground truth for Phase 3. Do not delete them to free space. Move the
volume or add disk.

### The schedule is expired or missing

`make schedule` ends with a status line. `NO SCHEDULE LOADED` or `EXPIRED` means
Phase 3 and 4 cannot resolve trips, because the feed's `feed_end_date` has passed
and trips running today may not exist in any loaded version.

```
make load-gtfs          # safe any time, skips when the feed is unchanged
make load-gtfs-force    # reload the same bytes in place
```

The loader is idempotent. It does a HEAD request first and does nothing when
`Last-Modified` and `Content-Length` are unchanged, so running it by hand costs
almost nothing.

### The weekly load keeps failing

Check `make schedule` for the status and error of recent attempts.

- `http_error` with a status: MTS side, or the URL moved. Confirm by hand:
  `curl -sSI https://www.sdmts.com/google_transit_files/google_transit.zip`
- `http_error` with no status: network or DNS. Nothing to do but retry.
- `parse_error`: the archive downloaded but is not the GTFS this project expects,
  either truncated or genuinely changed. The message names the offending value.
  Run `uv run pytest -m network` to check the real feed against the assumptions
  the design rests on. If MTS changed the feed shape, `DESIGN.md` ADR-0025 and
  ADR-0026 need revisiting before trusting a load.
- `db_error`: Docker Desktop or Postgres. `make up`.

A failed load changes nothing. The load is one transaction, so the previous
version stays in place and the next run simply tries again. There is never a
partially loaded schedule.

### Every weekly run says skipped_unchanged

That is the healthy steady state, not a problem. MTS republishes roughly monthly,
so most weekly runs correctly have nothing to do. What would be wrong is no rows
at all in `gtfs_load_log`, which means the agent is not running:
`make service-status`.

### The schedule tables are getting large

Expected. Every load keeps its own copy, about 1.62M rows, because Phase 4 has to
compare a prediction against the schedule that was in effect when it was made
(ADR-0024). At MTS's cadence that is roughly 20M rows a year.

Do not delete old versions while Phase 4 results depend on them. When pruning is
genuinely needed, one delete does it and the cascade takes the rest:

```
delete from feed_versions where feed_version = '<sha256>';
```

## When the MTS API key arrives

This is the one planned change to make immediately, because a design assumption
depends on it.

1. Put the key in `.env` as `MTS_API_KEY` and set
   `MTS_FEED_BASE_URL=https://realtime.sdmts.com/api/api/gtfs_realtime`.
2. Before anything else, fetch both feeds as `.pbtext` and read them:
   ```
   curl -sS "https://realtime.sdmts.com/api/api/gtfs_realtime/trip-updates-for-agency/MTS.pbtext?key=$MTS_API_KEY" | head -80
   ```
3. Check whether trip updates carry a `stop_time_update` per upcoming stop with
   absolute arrival times, or a single entry with only a `delay`. CLAUDE.md
   records this as an unverified assumption and Phase 4 depends on the answer.
   The collector already handles both shapes, per ADR-0019, so collection is not
   blocked either way, but record which one is real and delete the dead branch.
4. Watch `poll_log` for the `no_stop_sequence` drop counter in the logs. If the
   real feed identifies stops by `stop_id` without a sequence, predictions are
   being dropped and Phase 2 becomes a hard prerequisite for Phase 4 rather than
   just a useful step.
5. Restart the collector: `make service-install` reinstalls and restarts both
   agents.
6. Make sure a schedule is loaded before relying on Phase 3 or 4:
   `make schedule` should say `current`.

Never commit the key. It belongs only in `.env`, which is gitignored.

## Things that are deliberately not alerts

- A single feed failing while the other works. `/healthz` stays 200 by design,
  per ADR-0022, because restarting would not fix it and would cost the cache.
  It is visible in `make coverage` and worth investigating, not worth a restart.
- `skipped_unchanged` polls. These are healthy: MTS publishes on its own cadence,
  so polling faster than it publishes legitimately sees the same header twice.
- Sub 30 second prediction revisions being discarded. That is the intended lossy
  compression, per ADR-0017.

## Collection stopped and nobody noticed

This happened: Docker Desktop was not running, Postgres was unreachable, the
collector crash looped for nine hours, and no part of the system said anything.
Realtime feed data cannot be backfilled, so an outage nobody notices is
permanent data loss. Three things now cover it.

**Docker Desktop starts at login.** Its `AutoStart` setting was `false`, which
meant that after any reboot the database never came back. It is now `true`, and
the Postgres container carries `restart: unless-stopped`, so the whole chain
recovers on its own: Docker starts, Postgres starts, and launchd's `KeepAlive`
has the collector reconnect.

**The watchdog agent** (`sd.ontime.watchdog`) checks `/healthz` every five
minutes and posts a macOS notification when collection stops, and again when it
recovers. It does not restart anything, because launchd already does. It alerts
only after two consecutive failed probes, so a restart blip is not an outage.

    log:    ~/Library/Logs/ontime-sd/watchdog.log
    state:  ~/Library/Logs/ontime-sd/watchdog.state   (ok|down, last notify, consecutive failures)
    by hand: bash scripts/watchdog.sh ; echo $?        (0 healthy, 1 down)

If it is alerting and you want to know why, `make health` shows the collector's
own view and `make coverage` shows what was lost.

**System sleep.** The machine was set to sleep after one minute idle, on AC as
well as battery. That interacts badly with a crash loop: the collector holds a
`caffeinate` assertion only while it is alive, so a database outage drops the
assertion, the machine sleeps, and nothing retries until someone wakes it. On AC
it should never sleep:

    sudo pmset -c sleep 0

Check it with `pmset -g custom`. Battery is left alone deliberately, since the
laptop is not a server when it is unplugged.

## Deploying the collector to AWS

The laptop cannot be a 24/7 collector: closing the lid sleeps it and no user
space assertion overrides that. The Terraform stack in `infra/` moves collection
to an always on host. Roughly $23 to $26 a month on demand.

### One time, before the first apply

The AWS profile is required with no default, because this machine holds
credentials for an unrelated project and a default would make deploying into the
wrong account a single forgotten flag.

    aws configure --profile ontime          # or: aws login
    aws sts get-caller-identity --profile ontime

The API key goes into SSM by hand, not through Terraform, so it never enters the
state file:

    aws ssm put-parameter --profile ontime --region us-west-2 \
      --name /ontime-sd/mts-api-key --type SecureString \
      --value "$(grep '^MTS_API_KEY=' .env | cut -d= -f2-)"

Then a key to reach the box with, and the variables:

    ssh-keygen -t ed25519 -f ~/.ssh/ontime-sd -C ontime-sd
    cp infra/terraform.tfvars.example infra/terraform.tfvars
    # fill in ssh_cidr (curl -s https://checkip.amazonaws.com), ssh_public_key, alarm_email

### Apply

    make infra-init
    make infra-plan          # read it; this is the step that starts costing money
    make infra-apply
    make infra-output        # address, SSH command, cost estimate

First boot takes a few minutes: it formats the data volume, installs Docker and
uv, clones the repo, reads the key from SSM, runs the migrations and starts the
systemd units. Watch it:

    ssh ec2-user@<ip> 'sudo tail -f /var/log/cloud-init-output.log'
    ssh ec2-user@<ip> 'systemctl status ontime-collector'

### Move the history across

Both collectors run during this, deliberately: stopping the laptop first would
leave a gap, and realtime data cannot be backfilled. The script restores into a
staging database and merges with `on conflict do nothing`, so an overlap is
harmless and the script is safe to run twice.

    make migrate-to-aws HOST=ec2-user@<ip>

Then rebuild the derived tables on the host, which were deliberately not shipped:

    ssh ec2-user@<ip> 'cd /opt/ontime-sd && make arrivals && make compare'

Once the cloud database looks right, stop the laptop agents so collection is not
split across two machines:

    make service-uninstall

### What runs where afterwards

| Thing | Laptop | Cloud |
| --- | --- | --- |
| Collector | removed | `ontime-collector.service`, `Restart=always` |
| Weekly GTFS refresh | removed | `ontime-gtfs.timer`, Sundays 03:30 |
| Staleness alerting | removed | CloudWatch alarm, via a per minute metric |
| Postgres | can stay for local work | the system of record |
| API and map | still local | not deployed yet |

### Getting a shell on the host

Session Manager is the primary path, because it needs no SSH key, no open port
and no security group rule, and it keeps working when a home address rotates:

    make ssh-aws

SSH still works when the security group matches where you are. Home addresses
rotate, sometimes within minutes, so when SSH times out that is the first thing
to check:

    curl -s https://checkip.amazonaws.com     # compare with the rule
    make infra-allow-me                       # point the rule at where you are now

Running a command without a shell at all, which is how the first deploy was
repaired:

    aws ssm send-command --profile ontime --region us-west-2 \
      --instance-ids <id> --document-name AWS-RunShellScript \
      --parameters 'commands=["systemctl status ontime-collector"]'

### Reaching the cloud database

Postgres listens on localhost only and the security group has no 5432 rule.
Open a tunnel instead:

    ssh -N -L 5434:localhost:5433 ec2-user@<ip>
    psql postgresql://ontime:ontime@localhost:5434/ontime_sd

### When the alarm fires

`ontime-sd-collection-stale` means no successful poll for ten minutes. Missing
data is treated as breaching, so it also fires when the metric stops arriving at
all, which is what a dead host looks like.

    ssh ec2-user@<ip> 'systemctl status ontime-collector; tail -50 /var/log/ontime-sd/collector.err.log'
    ssh ec2-user@<ip> 'docker ps; df -h /var/lib/ontime-sd'

A full data volume is the slow failure this design has. `make coverage` on the
host shows what was lost. The volume can be grown in place:
raise `data_volume_gb`, apply, then `sudo resize2fs /dev/nvme1n1`.

### Deploying the web application

The site is one CloudFront distribution with two origins: the built React app
from a private S3 bucket, and the API from the instance that already holds the
database. Same origin is the point. The frontend calls `/api/...` with relative
paths, so there is no CORS configuration anywhere and nothing in the client that
knows where the API lives.

    make infra-apply        # creates the bucket, the distribution and the deploy role
    make infra-output       # site_url is the public address

Deploys happen on push to main through `.github/workflows/deploy.yml`. The
workflow runs the Python suite against a real Postgres and the frontend checks
first, then publishes. It authenticates with GitHub OIDC, so there are no AWS
keys in the repository: the role trusts only this repo on refs/heads/main.

To deploy by hand:

    cd web && npx vite build
    aws s3 sync web/dist "s3://$(terraform -chdir=infra output -raw web_bucket)" --delete
    aws cloudfront create-invalidation \
      --distribution-id "$(terraform -chdir=infra output -raw distribution_id)" --paths '/*'

### When the site is up but the data is wrong

Work out which layer first, because they fail differently:

| Symptom | Likely layer |
| --- | --- |
| Page loads, panels say "could not load" | API or nginx on the instance |
| Page itself 404s or shows stale assets | S3 or the CloudFront cache |
| Numbers load but look stale | the API's own 60s cache, by design |

    curl -s https://<site>/healthz                 # the API, through CloudFront
    make ssh-aws                                   # then: systemctl status ontime-api nginx
    sudo tail -50 /var/log/ontime-sd/api.err.log

The edge never caches `/api/*`. That is deliberate: the API already caches for
sixty seconds keyed on every parameter that changes the result, and a second
cache in front of it would answer a changed setting with the previous setting's
numbers, which is the bug ADR-0039 exists to prevent.

### Tearing it down

    make infra-destroy

The data volume carries `prevent_destroy`, so it survives and Terraform refuses
to delete it. That is deliberate: it holds the only copy of data that cannot be
recollected. Removing it is a conscious act, after a snapshot.

## Known limitations

- Running on a laptop means sleep gaps. Closing the lid sleeps the machine and
  no user space assertion can prevent that, so coverage is bounded by how often
  the laptop is open and docked. Measurable in `poll_log`, and the reason Phase 7
  moves collection to an always on host.
- The prediction cache is process local and starts cold, so each restart causes
  one burst of redundant writes.
- A service day is inferred from the observation time when the feed omits
  `start_date`, which is wrong for a trip observed after midnight that began the
  previous service day. Phase 2 provides the schedules needed to fix it.

### The deploy workflow cannot assume its role

Symptom, at the "Assume the deploy role" step, on every run:

    Could not assume role with OIDC:
    Not authorized to perform sts:AssumeRoleWithWebIdentity

Check the claim GitHub actually sends BEFORE auditing anything in AWS:

    gh api /repos/AndrewPhamCode/ontime-sd/actions/oidc/customization/sub

If `use_immutable_subject` is true, the `sub_claim_prefix` it returns carries
numeric owner and repository ids and will not equal `repo:owner/name`. The trust
policy has to match the prefix verbatim. Put it in `infra/terraform.tfvars`:

    github_sub_prefix = "repo:<owner>@<owner_id>/<name>@<repo_id>"

then apply and re-run the workflow without needing a new commit:

    make infra-apply
    gh workflow run deploy.yml

Confirm the role is what you think it is:

    aws iam get-role --role-name ontime-sd-github-deploy --profile ontime \
      --query 'Role.AssumeRolePolicyDocument'

The AWS side being correct is the trap here. The provider, its audience list and
the policy all read fine in the console while the claim never matched. See
ADR-0048.

### aws login puts you on the root user

`aws login` writes `login_session` into the profile it used, so authenticating as
root once pins that profile to root, and every later `--profile ontime` silently
reuses it. `aws sts get-caller-identity --profile ontime` is the only way to know
which identity you actually hold.

    aws logout --profile ontime
    # ~/.aws/config, under [profile ontime]:
    #   login_session = arn:aws:iam::611955806928:user/ontime-deploy
    aws login --profile ontime

Leave `[default]` with no `login_session`, so a bare `aws login` cannot pick root
without being asked. Root is for billing and closing the account, nothing here.
