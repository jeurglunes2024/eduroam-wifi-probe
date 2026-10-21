#!/usr/bin/env bash
# install_trigger.sh - install the "trigger a scan from the dashboard" service.
#
# Installs trigger.py as a systemd service on port 8088, generates a token,
# and prints the dashboard button URLs to paste into a Grafana Text panel.
# Needs the run wrapper from install_service.sh (installs it if missing).
#
# Usage:  sudo ./install_trigger.sh            # install/start, print URLs
#         sudo ./install_trigger.sh --uninstall

set -euo pipefail
PORT=8088
BIN=/usr/local/bin/eduroam-probe
WRAP=/usr/local/sbin/eduroam-probe-run
TRIG=/usr/local/sbin/eduroam-probe-trigger
TOKEN=/etc/eduroam-probe/trigger.token
UNIT=/etc/systemd/system/eduroam-probe-trigger.service

log() { printf '\e[1;34m[trigger]\e[0m %s\n' "$*"; }
die() { printf '\e[1;31m[error]\e[0m %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
here="$(dirname "$0")"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now eduroam-probe-trigger.service 2>/dev/null || true
  rm -f "$UNIT" "$TRIG"
  systemctl daemon-reload
  log "Removed trigger service (token left in place)."
  exit 0
fi

[ -x "$BIN" ] || die "$BIN not found - install the agent first"
install -m 755 "$here/eduroam-probe-run" "$WRAP"
install -m 755 "$here/trigger.py" "$TRIG"

if [ ! -s "$TOKEN" ]; then
  umask 077
  openssl rand -hex 16 > "$TOKEN"
  chmod 600 "$TOKEN"
  log "Generated access token at $TOKEN"
fi

cat > "$UNIT" <<EOF
[Unit]
Description=eduroam probe - dashboard trigger endpoint
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 $TRIG
Environment=TRIGGER_PORT=$PORT
Restart=on-failure
Nice=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now eduroam-probe-trigger.service >/dev/null
sleep 1
systemctl is-active --quiet eduroam-probe-trigger.service || die "service failed: journalctl -u eduroam-probe-trigger"

tok="$(cat "$TOKEN")"
ip="$(ip -o -4 addr show 2>/dev/null | awk '$2 ~ /^(eth|end|enx)/ {split($4,a,"/"); print a[1]; exit}')"
ip="${ip:-<pi-ip>}"
log "Running on port $PORT."
echo
echo "Add a Text panel (mode: HTML) to the Overview dashboard with these buttons:"
echo "-----------------------------------------------------------------------"
cat <<HTML
<a href="http://$ip:$PORT/run?mode=scan-only&token=$tok" target="_blank"
   style="display:inline-block;padding:8px 14px;margin:4px;background:#1f6feb;color:#fff;
   border-radius:6px;text-decoration:none;font-family:sans-serif">Scan now (passive)</a>
<a href="http://$ip:$PORT/run?mode=nearest-ap&token=$tok" target="_blank"
   style="display:inline-block;padding:8px 14px;margin:4px;background:#238636;color:#fff;
   border-radius:6px;text-decoration:none;font-family:sans-serif">Full test, nearest AP</a>
HTML
echo "-----------------------------------------------------------------------"
echo "In Grafana: Add > Visualization > Text, set Mode = HTML, paste the above, Apply."
echo "The token is in the URL, so keep this dashboard private."
