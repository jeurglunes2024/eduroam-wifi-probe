#!/usr/bin/env bash
# update.sh - update an already-provisioned eduroam probe in place.
#
# Use this when the probe is already set up and you just want the latest agent
# (e.g. v0.6.0 raw-data capture) without re-running setup.sh. It does NOT touch
# InfluxDB, Grafana, the USB gadget, or your probe.env secrets.
#
# Run it on the Pi, from inside the unzipped eduroam-probe folder:
#     cd eduroam-probe && sudo ./update.sh
#
# Or point it straight at a probe.py you copied over:
#     sudo ./update.sh /home/admin/probe.py
#
# The agent runs from systemd timers (not a long-lived service), so there is
# nothing to restart: the next scheduled scan/test uses the new code.

set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ENV=/etc/eduroam-probe/probe.env
DEST=/opt/eduroam-probe/probe.py

log() { printf '\e[1;32m[update]\e[0m %s\n' "$*"; }
die() { printf '\e[1;31m[error]\e[0m %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root (sudo ./update.sh)"

# Find the new probe.py: an explicit argument, else the repo's agent/, else next to this script.
SRC="${1:-}"
if [ -z "$SRC" ]; then
  if [ -f "$here/agent/probe.py" ]; then SRC="$here/agent/probe.py"
  elif [ -f "$here/probe.py" ];      then SRC="$here/probe.py"
  else die "no probe.py found; pass its path:  sudo ./update.sh /path/to/probe.py"; fi
fi
[ -f "$SRC" ] || die "no such file: $SRC"
[ -d /opt/eduroam-probe ] || die "/opt/eduroam-probe missing - this probe isn't provisioned yet; run setup.sh instead"

log "Checking $SRC compiles"
python3 -m py_compile "$SRC" || die "that probe.py has a syntax error; not installing it"

if [ -f "$DEST" ]; then
  OLD="$(grep -m1 '^VERSION' "$DEST" | cut -d'"' -f2 || true)"
  cp -a "$DEST" "$DEST.bak"           # one-step rollback: cp -a $DEST.bak $DEST
  log "Backed up current agent (v${OLD:-?}) to $DEST.bak"
fi

install -m 755 "$SRC" "$DEST"
ln -sf "$DEST" /usr/local/bin/eduroam-probe
NEW="$(grep -m1 '^VERSION' "$DEST" | cut -d'"' -f2 || true)"
log "Installed agent v${NEW:-?}"

# Make sure raw capture is switched on (new in v0.6.0). Leaves any existing value alone.
if ! grep -q '^CAPTURE_RAW=' "$ENV" 2>/dev/null; then
  echo 'CAPTURE_RAW=1' >> "$ENV"
  log "Added CAPTURE_RAW=1 to $ENV (set to 0 to disable raw scan storage)"
else
  log "CAPTURE_RAW already set in $ENV - leaving it as is"
fi

log "Verifying"
eduroam-probe doctor || log "doctor reported issues above - review before the next run"

echo
log "Done. The next scan writes the richer wifi_scan fields and the wifi_scan_raw"
log "measurement. Kick one off now if you like:"
echo "    sudo eduroam-probe run --scan-only --location \"\$(grep -m1 ^LOCATION= $ENV | cut -d= -f2-)\""
