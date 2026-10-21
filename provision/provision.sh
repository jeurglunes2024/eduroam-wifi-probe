#!/usr/bin/env bash
# provision.sh - base image setup for the eduroam Wi-Fi probe (Raspberry Pi only).
#
# Hardware assumption: a Raspberry Pi with its built-in Wi-Fi and nothing else.
# The built-in radio (wlan0) is the eduroam test radio. Management access
# (SSH, and later the Grafana dashboard) goes over Ethernet, because the probe
# takes over wlan0 and will drop any Wi-Fi connection you are using.
#
# Run once on a fresh Raspberry Pi OS Lite (64-bit) as root. Safe to re-run.
#
# What it does:
#   1. Installs packages (iw, wpa_supplicant, tcpdump, iperf3, DNS tools, Python...).
#   2. Sets hostname and Wi-Fi regulatory country.
#   3. Takes wlan0 away from NetworkManager so the probe agent controls it.
#   4. Turns off firmware roaming (brcmfmac roamoff=1) so a test stays on the
#      access point it picked, and turns off Wi-Fi power saving at boot.
#   5. Creates the directories and settings file the agent will use.
#
# Usage:
#   sudo ./provision.sh --hostname eduprobe-01 [--country US] [--iface wlan0] [--force]
#
#   --force   continue even if your SSH session is running over the Wi-Fi radio
#             (it will disconnect at the next reboot).

set -euo pipefail

HOSTNAME_NEW=""
COUNTRY="US"
IFACE="wlan0"
FORCE=0

PROBE_ROOT=/opt/eduroam-probe
PROBE_DATA=/var/lib/eduroam-probe
PROBE_ETC=/etc/eduroam-probe

log()  { printf '\e[1;34m[provision]\e[0m %s\n' "$*"; }
warn() { printf '\e[1;33m[warn]\e[0m %s\n' "$*" >&2; }
die()  { printf '\e[1;31m[error]\e[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname) HOSTNAME_NEW="$2"; shift 2 ;;
    --country)  COUNTRY="${2^^}"; shift 2 ;;
    --iface)    IFACE="$2"; shift 2 ;;
    --force)    FORCE=1; shift ;;
    -h|--help)  usage 0 ;;
    *)          warn "unknown option: $1"; usage 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ -n "$HOSTNAME_NEW" ]] || die "--hostname is required"
[[ "$HOSTNAME_NEW" =~ ^[a-z0-9-]{1,63}$ ]] || die "hostname must be lowercase letters, digits, hyphens"
[[ -e /sys/class/net/$IFACE/wireless || -e /sys/class/net/$IFACE/phy80211 ]] \
  || die "$IFACE is not a Wi-Fi interface (check: iw dev)"

# ---------------------------------------------------------------- lock-out guard
# If this SSH session arrives over the Wi-Fi radio, provisioning will cut it off.
if [[ -n "${SSH_CONNECTION:-}" ]]; then
  client_ip=${SSH_CONNECTION%% *}
  via=$(ip route get "$client_ip" 2>/dev/null | grep -oP 'dev \K\S+' || true)
  if [[ "$via" == "$IFACE" ]]; then
    if [[ $FORCE -eq 1 ]]; then
      warn "SSH is running over $IFACE. You will lose this connection after reboot; reconnect over Ethernet."
    else
      die "Your SSH session is running over $IFACE, which the probe is about to take over.
       Connect over Ethernet and re-run, or pass --force if you understand you'll lose Wi-Fi access."
    fi
  fi
fi

# ---------------------------------------------------------------- packages
log "Installing packages"
export DEBIAN_FRONTEND=noninteractive
echo "iperf3 iperf3/start_daemon boolean false" | debconf-set-selections

apt-get update -q
apt-get install -y -q --no-install-recommends \
  iw wireless-regdb wpasupplicant rfkill ethtool \
  tcpdump iperf3 iputils-ping bind9-dnsutils curl \
  sqlite3 jq git chrony udhcpc \
  python3 python3-venv python3-pip

# ---------------------------------------------------------------- hostname and country
log "Setting hostname to $HOSTNAME_NEW"
hostnamectl set-hostname "$HOSTNAME_NEW"
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOSTNAME_NEW/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "$HOSTNAME_NEW" >> /etc/hosts
fi

log "Setting Wi-Fi regulatory country to $COUNTRY"
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_wifi_country "$COUNTRY" || warn "raspi-config could not set country"
fi
iw reg set "$COUNTRY" || true

# ---------------------------------------------------------------- NetworkManager
if [[ -d /etc/NetworkManager ]]; then
  log "Excluding $IFACE from NetworkManager (Ethernet stays managed)"
  mkdir -p /etc/NetworkManager/conf.d
  cat > /etc/NetworkManager/conf.d/90-eduroam-probe.conf <<EOF
# Managed by eduroam-probe provision.sh
# The probe agent controls the Wi-Fi radio directly with wpa_supplicant and iw.
[keyfile]
unmanaged-devices=interface-name:$IFACE
EOF
fi

# ---------------------------------------------------------------- driver settings
# brcmfmac roams inside the firmware by default, which would let the radio
# hop to a different access point mid-test. roamoff=1 hands roaming to the
# host, so pinning a BSSID in wpa_supplicant actually holds.
drv=$(basename "$(readlink -f "/sys/class/net/$IFACE/device/driver")" 2>/dev/null || true)
if [[ "$drv" == brcmfmac ]]; then
  log "Disabling firmware roaming (brcmfmac roamoff=1)"
  echo "options brcmfmac roamoff=1" > /etc/modprobe.d/eduroam-probe-brcmfmac.conf
else
  warn "$IFACE uses driver '$drv', not brcmfmac; skipping roamoff setting"
fi

log "Installing radio setup service (power save off, unblock rfkill)"
cat > /usr/local/sbin/probe-radio-setup <<EOF
#!/usr/bin/env bash
# Managed by eduroam-probe provision.sh
rfkill unblock wifi || true
iw dev $IFACE set power_save off 2>/dev/null || true
EOF
chmod 755 /usr/local/sbin/probe-radio-setup

cat > /etc/systemd/system/probe-radio-setup.service <<EOF
[Unit]
Description=eduroam probe radio setup
After=sys-subsystem-net-devices-$IFACE.device NetworkManager.service
Wants=sys-subsystem-net-devices-$IFACE.device

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/probe-radio-setup
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable probe-radio-setup.service >/dev/null

# ---------------------------------------------------------------- directories
log "Creating probe directories"
install -d -m 755 "$PROBE_ROOT"
install -d -m 750 "$PROBE_DATA" "$PROBE_DATA/results" "$PROBE_DATA/pcap" "$PROBE_DATA/logs"
install -d -m 700 "$PROBE_ETC"   # eduroam credentials go here, root-only

if [[ ! -f $PROBE_ETC/probe.env ]]; then
  cat > "$PROBE_ETC/probe.env" <<EOF
# eduroam probe settings. Fill in the EAP section before the first connection test.
PROBE_ID=$HOSTNAME_NEW
TEST_IFACE=$IFACE
SSID=eduroam
# EAP_IDENTITY=
# EAP_PASSWORD=
# EAP_CA_CERT=$PROBE_ETC/radius-ca.pem
# EAP_DOMAIN_MATCH=
# IPERF_SERVER=
EOF
  chmod 600 "$PROBE_ETC/probe.env"
fi

log "Done. Reboot (sudo reboot), reconnect over Ethernet, then run check_radios.sh."
