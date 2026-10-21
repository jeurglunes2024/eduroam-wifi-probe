#!/usr/bin/env bash
# check_radios.sh - setup acceptance check for the eduroam Wi-Fi probe (Pi only).
#
# Run as root after provision.sh and a reboot. It confirms:
#   - the Pi model and its built-in radio (driver, bands)
#   - regulatory domain, power save off, firmware roaming off,
#     and the radio is out of NetworkManager's hands
#   - Ethernet is up for management access
#   - the radio can see eduroam, listing the strongest BSSIDs
#
# Usage: sudo ./check_radios.sh [ssid] [iface]   (defaults: eduroam wlan0)

set -uo pipefail
SSID="${1:-eduroam}"
IFACE="${2:-wlan0}"
PASS=0; FAIL=0

ok()   { printf '  \e[32mPASS\e[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \e[31mFAIL\e[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
info() { printf '  ---- %s\n' "$*"; }
hdr()  { printf '\n\e[1m%s\e[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }

hdr "System"
info "model: $(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
info "kernel: $(uname -r)"

hdr "Regulatory domain"
reg=$(iw reg get 2>/dev/null | awk '/^country/{print $2; exit}' | tr -d ':')
if [[ -n "$reg" && "$reg" != "00" ]]; then ok "country $reg"; else bad "regulatory domain not set (country 00)"; fi

hdr "Wi-Fi radio ($IFACE)"
if [[ ! -e /sys/class/net/$IFACE ]]; then
  bad "$IFACE not present"
else
  phy=$(basename "$(readlink -f "/sys/class/net/$IFACE/phy80211")")
  drv=$(basename "$(readlink -f "/sys/class/net/$IFACE/device/driver")")
  info "phy=$phy  driver=$drv  mac=$(cat "/sys/class/net/$IFACE/address")"

  pinfo=$(iw phy "$phy" info)
  bands=""
  grep -q '^\s*Band 1:' <<<"$pinfo" && bands+="2.4 "
  grep -q '^\s*Band 2:' <<<"$pinfo" && bands+="5 "
  grep -q '^\s*Band 4:' <<<"$pinfo" && bands+="6 "
  info "bands (GHz): ${bands:-none}"
  [[ $bands == *"2.4 "* ]] && ok "2.4 GHz supported" || bad "no 2.4 GHz"
  [[ $bands == *"5 "* ]]   && ok "5 GHz supported"   || bad "no 5 GHz (Pi 3B+ or newer needed)"

  ps=$(iw dev "$IFACE" get power_save 2>/dev/null | awk '{print $NF}')
  [[ $ps == off ]] && ok "power save off" || bad "power save is '${ps:-unknown}'"

  if [[ $drv == brcmfmac ]]; then
    roam=$(cat /sys/module/brcmfmac/parameters/roamoff 2>/dev/null || echo "?")
    [[ $roam == 1 || $roam == Y ]] && ok "firmware roaming off" || bad "firmware roaming on (roamoff=$roam)"
  fi

  if command -v nmcli >/dev/null; then
    nmcli -t -f DEVICE,STATE dev 2>/dev/null | grep -q "^$IFACE:unmanaged" \
      && ok "$IFACE unmanaged by NetworkManager" || bad "$IFACE still managed by NetworkManager"
  fi
fi

hdr "Management access"
eth=$(ip -o -4 addr show 2>/dev/null | awk '$2 ~ /^(eth|end|enx|usb)/ {print $2" "$4; exit}')
if [[ -n $eth ]]; then ok "Ethernet up: $eth"; else bad "no IPv4 on Ethernet or USB (you'll need it for SSH and Grafana)"; fi

hdr "$SSID visibility (scan on $IFACE)"
if [[ -e /sys/class/net/$IFACE ]]; then
  ip link set "$IFACE" up 2>/dev/null
  scan=""
  for _ in 1 2 3; do              # the radio may be busy right after link up
    scan=$(iw dev "$IFACE" scan 2>/dev/null) && [[ -n $scan ]] && break
    sleep 2
  done
  rows=$(awk -v want="$SSID" '
    /^BSS /     { if (bss && ssid==want) print sig, freq, bss; bss=substr($2,1,17); ssid=""; sig=""; freq="" }
    /\tfreq:/   { freq=int($2) }
    /\tsignal:/ { sig=$2 }
    /\tSSID:/   { sub(/^\tSSID: /,""); ssid=$0 }
    END         { if (bss && ssid==want) print sig, freq, bss }' <<<"$scan" | sort -rn)
  n=$(grep -c . <<<"$rows")
  if [[ $n -gt 0 ]]; then
    ok "$n $SSID BSSIDs visible"
    printf '        %-9s %-6s %-8s %s\n' "signal" "band" "freq" "bssid"
    while read -r sig freq bss; do
      if   (( freq < 3000 )); then band=2.4
      elif (( freq < 5925 )); then band=5
      else band=6; fi
      printf '        %-9s %-6s %-8s %s\n' "$sig dBm" "$band" "$freq" "$bss"
    done < <(head -n 10 <<<"$rows")
    info "nearest AP candidate: $(head -n1 <<<"$rows" | awk '{print $3" ("$1" dBm, "$2" MHz)"}')"
  else
    bad "no $SSID BSSIDs seen (no coverage here, radio blocked, or scan failed)"
  fi
fi

hdr "Result: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
