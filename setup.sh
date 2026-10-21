#!/usr/bin/env bash
# setup.sh - one-shot rebuild of the eduroam probe on a freshly flashed Pi (4 or 5).
#
# Run this on the FIRST boot, while the Pi still has internet (Wi-Fi or Ethernet),
# from inside the unzipped eduroam-probe folder:
#     cd eduroam-probe && sudo ./setup.sh
#
# It runs provisioning, installs InfluxDB+Grafana, installs the agent and
# dashboards, writes your known eduroam settings, enables the USB gadget, and
# sets the timezone. Everything that needs the internet happens before the
# radio is handed to the probe, so do it before rebooting.
#
# Order matters: provision.sh marks wlan0 unmanaged but that only takes effect
# at the next reboot, so the net stays up for the apt/influx/grafana steps here.

set -euo pipefail
HOSTNAME_NEW="${1:-eduprobe-01}"
here="$(cd "$(dirname "$0")" && pwd)"
ENV=/etc/eduroam-probe/probe.env

log() { printf '\e[1;32m[setup]\e[0m %s\n' "$*"; }
die() { printf '\e[1;31m[error]\e[0m %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root (sudo ./setup.sh)"

# Known project settings (non-secret). Edit here if anything changes.
set_env() {
  local k="$1" v="$2"
  if grep -q "^$k=" "$ENV" 2>/dev/null; then
    sed -i "s|^$k=.*|$k=$v|" "$ENV"
  elif grep -q "^# *$k=" "$ENV" 2>/dev/null; then
    sed -i "s|^# *$k=.*|$k=$v|" "$ENV"
  else
    echo "$k=$v" >> "$ENV"
  fi
}

log "1/6 Base provisioning"
"$here/provision/provision.sh" --hostname "$HOSTNAME_NEW"

log "2/6 InfluxDB + Grafana"
"$here/provision/install_dashboard.sh"

log "3/6 Agent, dashboards, AP names"
install -m 755 "$here/agent/probe.py" /opt/eduroam-probe/probe.py
ln -sf /opt/eduroam-probe/probe.py /usr/local/bin/eduroam-probe
[ -f /etc/eduroam-probe/ap_names.csv ] || install -m 644 "$here/agent/ap_names.csv" /etc/eduroam-probe/ap_names.csv
install -o grafana -g grafana -m 644 \
  "$here"/dashboard/overview.json "$here"/dashboard/access-point.json "$here"/dashboard/rf-survey.json \
  /var/lib/grafana/dashboards/eduroam/

log "4/6 eduroam defaults"
# Sensible defaults. Fill in the site-specific values in probe.env after setup
# (see probe.env.sample): EAP_IDENTITY, EAP_DOMAIN_MATCH, EAP_SERVER_CERT_HASH,
# and KNOWN_OUIS. If you maintain a private fork, set them here instead.
set_env EAP_METHOD PEAP
set_env EAP_PHASE2 auth=MSCHAPV2
grep -q '^LOCATION=' "$ENV" || set_env LOCATION unspecified

log "5/6 USB gadget (CDC-ECM, macOS-friendly)"
if ! dpkg -l rpi-usb-gadget 2>/dev/null | grep -q ^ii; then
  apt-get install -y -q rpi-usb-gadget || die "could not install rpi-usb-gadget (need internet)"
fi
rpi-usb-gadget on || true
# Belt and braces: make sure the Imager's legacy g_ether is NOT forcing itself,
# which is the gadget macOS won't enumerate.
if grep -q 'modules-load=dwc2,g_ether' /boot/firmware/cmdline.txt 2>/dev/null; then
  sed -i 's/ *modules-load=dwc2,g_ether/ modules-load=dwc2/' /boot/firmware/cmdline.txt
  log "   removed legacy g_ether from cmdline"
fi

log "6/6 Timezone"
timedatectl set-timezone America/New_York || true

echo
log "Done. Next:"
echo "  - Set where the probe sits:   sudo sed -i 's|^LOCATION=.*|LOCATION=\"Test desk\"|' $ENV"
echo "  - For unattended logins:       eduroam-probe hash-password   (paste EAP_PASSWORD_HASH into $ENV)"
echo "  - Continuous monitor:          sudo ./provision/install_service.sh"
echo "  - Dashboard trigger button:    sudo ./provision/install_trigger.sh"
echo "  - Reboot, then plug the Pi 4 USB-C into the Mac:  sudo reboot"
echo
echo "After reboot: dashboard at http://10.12.194.1:3000 over USB, or the Ethernet IP."
echo "Verify first:  sudo ./provision/check_radios.sh   and   vcgencmd get_throttled  (want 0x0)"
