#!/usr/bin/env bash
# install_service.sh - run the eduroam probe continuously via systemd timers.
#
# Sets up two timers (install the agent first):
#   eduroam-probe-scan.timer  - every 1 min   - passive scan only (no login)
#   eduroam-probe-test.timer  - every 15 min  - full connection test, nearest AP
#
# Both use the LOCATION value from /etc/eduroam-probe/probe.env as their tag,
# so set that to where the probe sits (e.g. LOCATION="Library 2F desk").
#
# Usage:
#   sudo ./install_service.sh                 # install and start
#   sudo ./install_service.sh --scan 1 --test 15   # custom minutes
#   sudo ./install_service.sh --uninstall     # stop and remove
#
# The 15-minute full test logs in, so it needs EAP_PASSWORD_HASH set in
# probe.env (a service has no terminal to prompt on). Without it, the test
# run still records scan data and just skips the connection part.

set -euo pipefail
SCAN_MIN=1
TEST_MIN=15
UNINSTALL=0
BIN=/usr/local/bin/eduroam-probe
WRAP=/usr/local/sbin/eduroam-probe-run
UNITDIR=/etc/systemd/system
ENV=/etc/eduroam-probe/probe.env

log() { printf '\e[1;34m[service]\e[0m %s\n' "$*"; }
warn() { printf '\e[1;33m[warn]\e[0m %s\n' "$*" >&2; }
die() { printf '\e[1;31m[error]\e[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --scan) SCAN_MIN="$2"; shift 2 ;;
    --test) TEST_MIN="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) die "unknown option: $1" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"

units="eduroam-probe-scan eduroam-probe-test"

if [ "$UNINSTALL" -eq 1 ]; then
  for u in $units; do
    systemctl disable --now "$u.timer" 2>/dev/null || true
  done
  rm -f "$UNITDIR"/eduroam-probe-*.service "$UNITDIR"/eduroam-probe-*.timer "$WRAP"
  systemctl daemon-reload
  log "Removed probe timers and services."
  exit 0
fi

[ -x "$BIN" ] || die "$BIN not found - install the agent first (see README)"
[ -f "$ENV" ] || die "$ENV not found - run provision.sh first"

log "Installing run wrapper"
install -m 755 "$(dirname "$0")/eduroam-probe-run" "$WRAP"

write_service() {  # name, description, args
  cat > "$UNITDIR/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target influxdb.service probe-radio-setup.service
Wants=probe-radio-setup.service

[Service]
Type=oneshot
ExecStart=$WRAP $3
# A single run should never run long; kill it well before the next tick.
TimeoutStartSec=600
Nice=10
EOF
}

write_timer() {  # name, minutes
  cat > "$UNITDIR/$1.timer" <<EOF
[Unit]
Description=Run $1 every $2 min

[Timer]
OnCalendar=*:0/$2:00
AccuracySec=2s
Persistent=false
# Fire shortly after boot too, not only on the next calendar tick.
OnBootSec=90

[Install]
WantedBy=timers.target
EOF
}

write_service eduroam-probe-scan "eduroam probe - passive scan" "--scan-only"
write_service eduroam-probe-test "eduroam probe - full connection test (nearest AP)" "--nearest-ap"
write_timer eduroam-probe-scan "$SCAN_MIN"
write_timer eduroam-probe-test "$TEST_MIN"

systemctl daemon-reload
for u in $units; do
  systemctl enable --now "$u.timer" >/dev/null
done

log "Installed. Scan every ${SCAN_MIN} min, full test every ${TEST_MIN} min, tagged LOCATION from $ENV."

# Warnings that affect whether data actually lands
grep -q '^LOCATION=' "$ENV" || warn "LOCATION is not set in $ENV; records will be tagged 'unspecified'. Set it to where the probe sits."
if ! grep -q '^EAP_PASSWORD_HASH=' "$ENV"; then
  warn "EAP_PASSWORD_HASH is not set; the 15-min full test will record scan data but SKIP the login."
  warn "Run 'eduroam-probe hash-password' and add the line to $ENV to enable connection tests."
fi

echo
log "Status:"
systemctl --no-pager list-timers 'eduroam-probe-*' 2>/dev/null || true
log "Watch runs with:  journalctl -u eduroam-probe-scan -u eduroam-probe-test -f"
