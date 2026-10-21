#!/usr/bin/env bash
# install_dashboard.sh - install InfluxDB 2 and Grafana on the probe Pi.
#
# Run AFTER provision.sh but BEFORE rebooting, while the Pi still has internet
# (for example over a phone hotspot). After the reboot the Wi-Fi radio is
# reserved for testing and the Pi may have no internet.
#
# What it does:
#   1. Adds the official InfluxData and Grafana apt repositories (key fingerprint checked).
#   2. Installs influxdb2, the influx CLI, and grafana; enables both services.
#   3. Runs InfluxDB first-time setup: org "fau", bucket "eduroam", random admin
#      password and API token, saved root-only in /etc/eduroam-probe/influx.env.
#   4. Connects Grafana to InfluxDB automatically (provisioned data source) and
#      prepares a folder that dashboards will be loaded from.
#   5. Writes one test data point so you can see the pipeline works end to end.
#
# Usage: sudo ./install_dashboard.sh

set -euo pipefail

ORG=fau
BUCKET=eduroam
PROBE_ETC=/etc/eduroam-probe
INFLUX_ENV=$PROBE_ETC/influx.env
DASH_DIR=/var/lib/grafana/dashboards/eduroam
INFLUX_FPR=24C975CBA61A024EE1B631787C3D57159FC2F927

log()  { printf '\e[1;34m[dashboard]\e[0m %s\n' "$*"; }
warn() { printf '\e[1;33m[warn]\e[0m %s\n' "$*" >&2; }
die()  { printf '\e[1;31m[error]\e[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ -d $PROBE_ETC ]] || die "$PROBE_ETC missing - run provision.sh first"

log "Checking internet access"
curl -fsS --max-time 10 -o /dev/null https://apt.grafana.com/gpg.key \
  || die "no internet. Connect the Pi (hotspot or shared Ethernet) and re-run."

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q --no-install-recommends gnupg ca-certificates curl openssl
install -d -m 755 /etc/apt/keyrings

# ---------------------------------------------------------------- repositories
if [[ ! -f /etc/apt/sources.list.d/influxdata.list ]]; then
  log "Adding InfluxData repository"
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/influx.key" https://repos.influxdata.com/influxdata-archive.key
  gpg --show-keys --with-fingerprint --with-colons "$tmp/influx.key" 2>&1 \
    | grep -q "^fpr:\+$INFLUX_FPR:\$" \
    || die "InfluxData key fingerprint mismatch (they may have rotated keys; check docs.influxdata.com)"
  gpg --dearmor < "$tmp/influx.key" > /etc/apt/keyrings/influxdata-archive.gpg
  echo 'deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main' \
    > /etc/apt/sources.list.d/influxdata.list
  rm -rf "$tmp"
fi

if [[ ! -f /etc/apt/sources.list.d/grafana.list ]]; then
  log "Adding Grafana repository"
  curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor > /etc/apt/keyrings/grafana.gpg
  echo 'deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main' \
    > /etc/apt/sources.list.d/grafana.list
fi

log "Installing influxdb2, influx CLI and grafana (this takes a few minutes on a Pi)"
apt-get update -q
apt-get install -y -q influxdb2 influxdb2-cli grafana

# ---------------------------------------------------------------- InfluxDB
log "Starting InfluxDB"
systemctl enable --now influxdb
for _ in $(seq 1 60); do
  curl -fsS http://localhost:8086/health >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS http://localhost:8086/health >/dev/null || die "InfluxDB did not come up (journalctl -u influxdb)"

if curl -fsS http://localhost:8086/api/v2/setup | grep -q '"allowed": *true'; then
  log "Running InfluxDB first-time setup (org=$ORG bucket=$BUCKET)"
  INFLUX_TOKEN=$(openssl rand -hex 32)
  INFLUX_ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=')
  influx setup --host http://localhost:8086 --org "$ORG" --bucket "$BUCKET" \
    --username probe-admin --password "$INFLUX_ADMIN_PASSWORD" \
    --token "$INFLUX_TOKEN" --force >/dev/null
  # Create the secrets file root-only without changing the umask for the rest
  # of the script (a lingering umask 077 made Grafana's files unreadable).
  install -m 600 /dev/null "$INFLUX_ENV"
  cat > "$INFLUX_ENV" <<EOF
# Written by install_dashboard.sh - InfluxDB on this Pi
INFLUX_URL=http://localhost:8086
INFLUX_ORG=$ORG
INFLUX_BUCKET=$BUCKET
INFLUX_TOKEN=$INFLUX_TOKEN
INFLUX_ADMIN_USER=probe-admin
INFLUX_ADMIN_PASSWORD=$INFLUX_ADMIN_PASSWORD
EOF
  chmod 600 "$INFLUX_ENV"
else
  log "InfluxDB already set up; reusing $INFLUX_ENV"
  [[ -f $INFLUX_ENV ]] || die "InfluxDB is set up but $INFLUX_ENV is missing"
fi
# shellcheck disable=SC1090
source "$INFLUX_ENV"

# ---------------------------------------------------------------- Grafana
log "Connecting Grafana to InfluxDB"
install -d -m 755 /etc/grafana/provisioning/datasources /etc/grafana/provisioning/dashboards
cat > /etc/grafana/provisioning/datasources/eduroam-influx.yaml <<EOF
# Written by install_dashboard.sh
apiVersion: 1
datasources:
  - name: eduroam-influx
    uid: eduroam-influx
    type: influxdb
    access: proxy
    url: http://localhost:8086
    isDefault: true
    jsonData:
      version: Flux
      organization: $ORG
      defaultBucket: $BUCKET
    secureJsonData:
      token: $INFLUX_TOKEN
EOF
chown root:grafana /etc/grafana/provisioning/datasources/eduroam-influx.yaml
chmod 640 /etc/grafana/provisioning/datasources/eduroam-influx.yaml

install -d -o grafana -g grafana -m 755 "$DASH_DIR"
cat > /etc/grafana/provisioning/dashboards/eduroam.yaml <<EOF
# Written by install_dashboard.sh - dashboards are loaded from $DASH_DIR
apiVersion: 1
providers:
  - name: eduroam
    folder: eduroam
    type: file
    allowUiUpdates: true
    options:
      path: $DASH_DIR
EOF
chown root:grafana /etc/grafana/provisioning/dashboards/eduroam.yaml
chmod 640 /etc/grafana/provisioning/dashboards/eduroam.yaml

systemctl enable grafana-server >/dev/null
systemctl reset-failed grafana-server 2>/dev/null || true
systemctl restart grafana-server
for _ in $(seq 1 60); do
  curl -fsS http://localhost:3000/api/health >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS http://localhost:3000/api/health >/dev/null || warn "Grafana not answering yet (journalctl -u grafana-server)"

# ---------------------------------------------------------------- end-to-end test
log "Writing a test point"
influx write --host "$INFLUX_URL" --org "$INFLUX_ORG" --bucket "$INFLUX_BUCKET" \
  --token "$INFLUX_TOKEN" "probe_health,probe=$(hostname) up=1i"
influx query --host "$INFLUX_URL" --org "$INFLUX_ORG" --token "$INFLUX_TOKEN" --raw \
  "from(bucket:\"$INFLUX_BUCKET\") |> range(start:-5m) |> filter(fn:(r)=> r._measurement==\"probe_health\")" \
  | grep -q probe_health && log "Test point stored and read back - pipeline works" \
  || warn "Could not read the test point back"

eth_ip=$(ip -o -4 addr show 2>/dev/null | awk '$2 ~ /^(eth|end|enx)/ {split($4,a,"/"); print a[1]; exit}')
log "Done."
log "Grafana:  http://${eth_ip:-<pi-ethernet-ip>}:3000   (login admin / admin, you'll be asked to change it)"
log "InfluxDB: http://${eth_ip:-<pi-ethernet-ip>}:8086   (credentials in $INFLUX_ENV)"
log "Now reboot: sudo reboot"
