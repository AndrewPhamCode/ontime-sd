#!/usr/bin/env bash
# First boot of the collector host. Written to be idempotent, because
# user_data_replace_on_change means this runs again on every instance rebuild
# while the data volume survives underneath it.
set -euxo pipefail

APP_USER=ontime
APP_DIR=/opt/ontime-sd
DATA_DIR=/var/lib/ontime-sd
DEVICE=/dev/nvme1n1   # EBS attached as /dev/sdf appears as an NVMe device on Nitro

dnf -y update
dnf -y install docker git postgresql16 jq nginx

# ---------------------------------------------------------------- data volume
# Format only if there is no filesystem. A rebuilt instance must mount the
# existing database, never reformat it.
if ! blkid "$DEVICE" >/dev/null 2>&1; then
  mkfs.ext4 -L ontime-data "$DEVICE"
fi
mkdir -p "$DATA_DIR"
grep -q "LABEL=ontime-data" /etc/fstab || echo "LABEL=ontime-data $DATA_DIR ext4 defaults,nofail 0 2" >> /etc/fstab
mount -a

# ------------------------------------------------------------------- packages
install -d -m 0755 /usr/local/lib/docker/cli-plugins
COMPOSE_VERSION=v2.32.4
curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
  "https://github.com/docker/compose/releases/download/$${COMPOSE_VERSION}/docker-compose-linux-aarch64"
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

systemctl enable --now docker

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"
usermod -aG docker "$APP_USER"

# ----------------------------------------------------------------- app source
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --depth 1 origin "${repo_ref}"
  git -C "$APP_DIR" reset --hard FETCH_HEAD
else
  git clone --depth 1 --branch "${repo_ref}" "${repo_url}" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# uv, installed as the app user so the venv it manages is owned correctly
sudo -u "$APP_USER" bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
UV=/home/$APP_USER/.local/bin/uv

# --------------------------------------------------------------------- secret
# The key is fetched at boot rather than baked into the image or the Terraform
# state. Nothing writes it to a log: set +x around the only line that holds it.
set +x
API_KEY="$(aws ssm get-parameter --with-decryption --region "${region}" \
  --name "${api_key_parameter}" --query Parameter.Value --output text)"
umask 077
cat > "$APP_DIR/.env" <<ENVFILE
MTS_API_KEY=$${API_KEY}
DATABASE_URL=postgresql://ontime:ontime@localhost:5433/ontime_sd

# The real feed. Without this the collector falls back to the mock server on
# localhost:8081, which is the right default for a laptop before the key
# arrives and exactly the wrong one here. It polled the mock for ten minutes on
# the first deploy because this line was missing.
MTS_FEED_BASE_URL=https://realtime.sdmts.com/api/api/gtfs_realtime

POLL_INTERVAL_SECONDS=30
PREDICTION_CHANGE_THRESHOLD_SECONDS=30
BACKOFF_BASE_SECONDS=1
BACKOFF_MAX_SECONDS=300
HEALTH_PORT=8080
HEALTH_STALE_AFTER_SECONDS=300
LOG_LEVEL=INFO
ENVFILE
unset API_KEY
set -x
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

# ------------------------------------------------------------------- database
# Port 5433 matches the laptop (ADR-0001) so DATABASE_URL and every script are
# identical in both places. The bind address is explicit: Postgres is reachable
# from this host only, which is why the security group has no 5432 rule.
install -d -o "$APP_USER" -g "$APP_USER" "$DATA_DIR/pgdata"
cat > "$APP_DIR/docker-compose.aws.yml" <<COMPOSE
services:
  postgres:
    image: postgres:16
    container_name: ontime-sd-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: ontime
      POSTGRES_PASSWORD: ontime
      POSTGRES_DB: ontime_sd
    ports:
      - "127.0.0.1:5433:5432"
    volumes:
      - $DATA_DIR/pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ontime -d ontime_sd"]
      interval: 10s
      timeout: 5s
      retries: 10
COMPOSE
chown "$APP_USER:$APP_USER" "$APP_DIR/docker-compose.aws.yml"

sudo -u "$APP_USER" docker compose -f "$APP_DIR/docker-compose.aws.yml" up -d --wait
sudo -u "$APP_USER" bash -lc "cd $APP_DIR && $UV sync && $UV run ontime-migrate"

# -------------------------------------------------------------------- systemd
# The Linux equivalent of the launchd agent: restart always, bounded restart
# rate so a database outage is a retry loop rather than a busy loop.
cat > /etc/systemd/system/ontime-collector.service <<'UNIT'
[Unit]
Description=OnTime SD realtime collector
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ontime
WorkingDirectory=/opt/ontime-sd
ExecStart=/home/ontime/.local/bin/uv run --project /opt/ontime-sd ontime-collector
Restart=always
RestartSec=30
StandardOutput=append:/var/log/ontime-sd/collector.log
StandardError=append:/var/log/ontime-sd/collector.err.log

[Install]
WantedBy=multi-user.target
UNIT

# Weekly static GTFS refresh, the systemd equivalent of sd.ontime.gtfs.
cat > /etc/systemd/system/ontime-gtfs.service <<'UNIT'
[Unit]
Description=OnTime SD static GTFS refresh
After=docker.service

[Service]
Type=oneshot
User=ontime
WorkingDirectory=/opt/ontime-sd
ExecStart=/home/ontime/.local/bin/uv run --project /opt/ontime-sd ontime-load-gtfs
StandardOutput=append:/var/log/ontime-sd/gtfs.log
StandardError=append:/var/log/ontime-sd/gtfs.err.log
UNIT

cat > /etc/systemd/system/ontime-gtfs.timer <<'UNIT'
[Unit]
Description=Refresh static GTFS weekly

[Timer]
OnCalendar=Sun 03:30
Persistent=true

[Install]
WantedBy=timers.target
UNIT

# Staleness metric, the cloud replacement for the local watchdog. It publishes
# seconds since the last successful poll; the alarm lives in CloudWatch so it
# fires even when this host is the thing that is broken.
cat > /usr/local/bin/ontime-publish-staleness <<'METRIC'
#!/usr/bin/env bash
set -uo pipefail
REGION="$(curl -fsS -H "X-aws-ec2-metadata-token: $(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/placement/region)"
age="$(PGPASSWORD=ontime psql -h localhost -p 5433 -U ontime -d ontime_sd -tAc \
  "select coalesce(extract(epoch from now() - max(started_at)), 999999)::int
     from poll_log where status in ('ok','skipped_unchanged')" 2>/dev/null)"
# A database that cannot be queried is itself an outage, so report it as one
# rather than publishing nothing and leaving the alarm with no data.
[[ -z "$age" ]] && age=999999
aws cloudwatch put-metric-data --region "$REGION" \
  --namespace OnTimeSD --metric-name SecondsSinceLastPoll \
  --unit Seconds --value "$age"
METRIC
chmod +x /usr/local/bin/ontime-publish-staleness

cat > /etc/systemd/system/ontime-staleness.service <<'UNIT'
[Unit]
Description=Publish collector staleness to CloudWatch

[Service]
Type=oneshot
ExecStart=/usr/local/bin/ontime-publish-staleness
UNIT

cat > /etc/systemd/system/ontime-staleness.timer <<'UNIT'
[Unit]
Description=Publish collector staleness every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
UNIT

# ------------------------------------------------------------------- the API
# uvicorn stays bound to localhost and nginx faces CloudFront. nginx handles the
# slow client and concurrency problems that a single uvicorn worker is bad at,
# and keeps the application off a public port entirely.
cat > /etc/systemd/system/ontime-api.service <<'UNIT'
[Unit]
Description=OnTime SD read-only API
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ontime
WorkingDirectory=/opt/ontime-sd
ExecStart=/home/ontime/.local/bin/uv run --project /opt/ontime-sd ontime-api
Restart=always
RestartSec=10
StandardOutput=append:/var/log/ontime-sd/api.log
StandardError=append:/var/log/ontime-sd/api.err.log

[Install]
WantedBy=multi-user.target
UNIT

# Only the two paths CloudFront routes here. Anything else gets a flat 404
# rather than revealing that this is a general purpose host.
cat > /etc/nginx/conf.d/ontime.conf <<'NGINX'
server {
    listen 80 default_server;
    server_name _;

    # The instance is only reachable from CloudFront, but a direct request to
    # the IP would still arrive if the prefix list ever changed, so the origin
    # does not serve anything it does not have to.
    location / { return 404; }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }

    # The API's own health endpoint, on 8000. The collector has a separate one
    # on 8080 which stays internal: it is what the CloudWatch staleness metric
    # reads, and it is not something a visitor should be able to poll.
    location = /healthz {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
NGINX

# The stock nginx.conf carries its own default server on port 80, which collides
# with ours. Replacing the whole file is deterministic; editing server blocks out
# of it with sed is not, and a half edited config that still passes nginx -t is
# worse than no config at all.
cat > /etc/nginx/nginx.conf <<'NGINXMAIN'
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log notice;
pid /run/nginx.pid;

events { worker_connections 1024; }

http {
    log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                    '$status $body_bytes_sent "$http_referer" '
                    '"$http_user_agent" "$http_x_forwarded_for"';
    access_log /var/log/nginx/access.log main;

    sendfile on;
    tcp_nopush on;
    keepalive_timeout 65;
    types_hash_max_size 4096;
    server_tokens off;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    include /etc/nginx/conf.d/*.conf;
}
NGINXMAIN

nginx -t

mkdir -p /var/log/ontime-sd
chown -R "$APP_USER:$APP_USER" /var/log/ontime-sd

cat > /etc/logrotate.d/ontime-sd <<'ROTATE'
/var/log/ontime-sd/*.log {
  daily
  rotate 14
  compress
  missingok
  notifempty
  copytruncate
}
ROTATE

systemctl daemon-reload
systemctl enable --now ontime-collector.service
systemctl enable --now ontime-api.service
systemctl enable --now nginx
systemctl enable --now ontime-gtfs.timer
systemctl enable --now ontime-staleness.timer
