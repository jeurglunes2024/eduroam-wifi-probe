# eduroam Wi-Fi Probe

## Rebuild quickstart (flashing a fresh card)

**In Raspberry Pi Imager**, before writing:
- OS: **Raspberry Pi OS Lite (64-bit)**.
- Hostname: `eduprobe-01`. Enable SSH, set user `admin` + password.
- Set **Wi-Fi** to your phone hotspot or home network, so the first boot is online (this is only for setup; the probe takes the radio over afterwards).
- Locale/timezone: America/New_York.
- **Do NOT enable the USB-gadget toggle in Imager** — it installs the legacy gadget macOS won't see. `setup.sh` sets up the macOS-friendly one.

**On first boot** (online), copy the zip over from your Mac and run one script:
```
scp ~/Downloads/eduroam-probe.zip admin@eduprobe-01.local:~   # or the Pi's IP
ssh admin@eduprobe-01.local
unzip -o eduroam-probe.zip && cd eduroam-probe
sudo ./setup.sh
```
`setup.sh` does everything: provisioning, InfluxDB + Grafana, the agent, the three dashboards, your eduroam settings (identity, PEAP/MSCHAPv2, pinned cert hash, known vendor OUI), the USB gadget, and the timezone. It prints the remaining optional steps (location tag, password hash, continuous-monitor service, trigger button) at the end. Then `sudo reboot` and plug the Pi 4's USB-C into the Mac.

Everything that needs the internet runs inside `setup.sh` before the reboot, so run it while the hotspot/Wi-Fi is still connected.

## Reference

Raspberry Pi tool that connects to the nearest eduroam access point, measures everything it can about the connection, and shows the results in Grafana. See `eduroam_probe_roadmap.pdf` for the plan.

## Hardware

Just a Raspberry Pi (3B+ or newer, for 5 GHz) and its power supply. The built-in Wi-Fi is the test radio. You'll also need:

- an Ethernet cable, for SSH and later the dashboard, because the probe takes over the Wi-Fi radio
- a wall outlet at each test location

## Setup

1. **Flash the SD card.** In Raspberry Pi Imager, choose **Raspberry Pi OS Lite (64-bit)**. In its settings, set the hostname to `eduprobe-01`, create your user, and enable SSH. Leave Wi-Fi blank, since you'll connect over Ethernet.
2. **Boot over Ethernet.** Plug the Pi into your router or switch (or straight into your laptop) and boot it.
3. **Copy the zip over and SSH in:**
   ```bash
   scp eduroam-probe-phase1.zip youruser@eduprobe-01.local:~
   ssh youruser@eduprobe-01.local
   ```
   If `.local` doesn't resolve, use the Pi's IP address from your router.
4. **Provision and reboot:**
   ```bash
   unzip eduroam-probe-phase1.zip
   cd eduroam-probe/provision
   chmod +x *.sh
   sudo ./provision.sh --hostname eduprobe-01
   sudo reboot
   ```
5. **Check it** somewhere with eduroam coverage, still connected over Ethernet:
   ```bash
   cd ~/eduroam-probe/provision
   sudo ./check_radios.sh
   ```
   Setup is done when everything passes and nearby eduroam BSSIDs are listed.

6. **Install the dashboard stack** while the Pi has internet (before the reboot in step 4, or by temporarily rejoining a hotspot):
   ```bash
   sudo ./install_dashboard.sh
   ```

## Probe agent and dashboard

### Install
From the unzipped `eduroam-probe` folder on the Pi:
```bash
sudo install -m 755 agent/probe.py /opt/eduroam-probe/probe.py
sudo ln -sf /opt/eduroam-probe/probe.py /usr/local/bin/eduroam-probe
sudo install -o grafana -g grafana -m 644 dashboard/overview.json dashboard/access-point.json dashboard/rf-survey.json /var/lib/grafana/dashboards/eduroam/
sudo install -m 644 agent/ap_names.csv /etc/eduroam-probe/ap_names.csv   # first time only; don't overwrite your edits
```
Grafana picks up the dashboards within about 10 seconds, in the `eduroam` folder.

### Dashboards
- **eduroam Overview** shows what is broken right now. Health totals, shared-service tiles (RADIUS, DHCP, DNS, HTTPS medians across all APs: red everywhere means it is not the APs), a table of every radio's latest result with failures on top, and failures over time. Click an AP name to open its detail page.
- **eduroam Access Point** is the drill-down for one AP, picked by name. It shows the latest result for each radio, signal, utilization and clients, stage timings over time, gateway latency and loss, link quality, co-channel neighbours, and full test history.
- **eduroam RF Survey** shows everything heard at a location. It has channel crowding, utilization reported by eduroam APs, and every network heard, strongest first.

The Overview also has a **Wi-Fi security** section (see below).

Colour thresholds are shared across all three: connect 1 s / 3 s, login 1 s / 3 s, DHCP 2 s / 5 s, gateway RTT 20 / 100 ms, loss 1 % / 5 %, channel utilization 50 % / 75 %, signal -67 / -75 dBm. They are set in `dashboard/build_dashboards.py` (the `T` table); rerun it to regenerate the JSON.

To check every dashboard query against your InfluxDB: `sudo python3 dashboard/check_dashboards.py dashboard/*.json`

### Naming APs
BSSIDs are hard to read, so name APs in `/etc/eduroam-probe/ap_names.csv` (`id,name,building`). The id is an AP group (first five octets, which names all of that AP's radios) or a single BSSID. `sudo eduroam-probe scan` lists the ids and shows names once set. Unnamed APs appear under their AP id. Names apply to new tests, not old ones.

### Wi-Fi security (passive)
Every scan now records what each network advertises in its beacon. Nothing is sent to the APs; this is read-only observation. Per BSSID the probe stores:
- `security`: encryption family — `open`, `wep`, `wpa1`, `wpa2-personal`, `wpa2-enterprise`, `wpa3-personal`, `wpa3-enterprise`, or a mix.
- `pmf`: protected management frames (802.11w) — `required`, `capable` (optional), or `off`. Without PMF, forged disconnect frames can knock users off; this is a common cause of unexplained drops.
- `wps`: whether WPS is advertised.
- `auth_suites`: the raw 802.1X / PSK / SAE suites offered.
- `security_flags`: the findings (see table).

| Flag | Meaning |
|---|---|
| `open` / `wep` / `wpa1` / `tkip` | Weak or no encryption / outdated cipher |
| `no_pmf` | Encrypted but management frames unprotected |
| `pmf_optional` | PMF offered but not required |
| `wps_on` | WPS enabled |
| `rogue_enc` | A network named `eduroam` that is **not** WPA-Enterprise — almost certainly not the real eduroam |
| `unknown_vendor` | An `eduroam` BSSID whose MAC vendor prefix is not in `KNOWN_OUIS` |

The `rogue_enc` and `unknown_vendor` flags are the impersonation/evil-twin signals. For `unknown_vendor` to work, list your institution's real AP vendor prefixes in `probe.env`:
```
KNOWN_OUIS=aa:bb:cc
```
(Comma-separated OUIs, the first three octets of the BSSID. Add every vendor your institution actually uses; find them with `sudo eduroam-probe scan`. Leave it unset to skip the vendor check.)

Related: a login that fails with `tls_cert_error` means the RADIUS server certificate did not match the pinned `radius.your-institution.edu` — a renewed cert, or an impersonator. The Overview's **Certificate mismatches** tile counts these.

The Overview's **Wi-Fi security** row surfaces all of this: counts of networks with findings, rogue suspects, cert mismatches and radios without PMF, plus a findings table. The probe cannot see a weakness it isn't told about in beacons, and it does not test AP management interfaces or attempt any exploitation — it only reports what the APs broadcast.

### Settings
Add these to `/etc/eduroam-probe/probe.env` (`sudo nano /etc/eduroam-probe/probe.env`):
```
EAP_METHOD=PEAP                         # or TTLS
EAP_PHASE2=auth=MSCHAPV2                # TTLS often uses auth=PAP
EAP_IDENTITY=you@your-institution.edu
EAP_ANON_IDENTITY=anonymous@your-institution.edu     # optional
# No password here by default: `run` prompts for it (kept in memory only).
# Optional, for unattended runs: EAP_PASSWORD_HASH=<from `eduroam-probe hash-password`>
EAP_DOMAIN_MATCH=radius.your-institution.edu           # RADIUS server certificate name
# If your institution publishes no CA, pin the server certificate instead. Get the hash from a run's log:
#   sudo grep -rh "PEER-CERT depth=0" /var/lib/eduroam-probe/logs/ | tail -1
EAP_SERVER_CERT_HASH=<64 hex chars>     # breaks when the institution renews the cert; update it then
# EAP_CA_CERT=/etc/eduroam-probe/radius-ca.pem  # alternative if IT provides the CA
LOCATION=unspecified                    # default location tag
DNS_TEST_HOST=www.google.com
HTTP_TEST_URL=https://www.google.com/generate_204
PING_TARGET=8.8.8.8
# IPERF_SERVER=iperf.your-institution.edu    # optional throughput tests
CAPTURE_RAW=1                           # store full raw scan text per AP (set 0 to disable)
```

### Password handling
Your password is never stored in plaintext unless you choose to put `EAP_PASSWORD=` in the file. By default `run` asks for it once per session and keeps it in memory. With MSCHAPv2 inner auth (the usual PEAP setup), wpa_supplicant receives only its NT hash. The temporary config lives in `/run` (RAM, root-only) and is deleted after every test.

For unattended runs (`--repeat` from a scheduled job), run `eduroam-probe hash-password` and put the `EAP_PASSWORD_HASH=` line it prints into `probe.env`. This hides your real password, but the hash still lets someone log in to eduroam as you, so treat the file as sensitive. A dedicated service account is the proper long-term answer.

### Use
```bash
sudo eduroam-probe doctor                          # check tools and settings
eduroam-probe hash-password                        # optional: make an NT hash for probe.env
sudo eduroam-probe scan                            # list eduroam BSSIDs
sudo eduroam-probe run --scan-only --location "Lab"   # RF data only, no credentials needed
sudo eduroam-probe run --location "Library 2F"     # test every eduroam radio in range (>= -80 dBm)
sudo eduroam-probe run --location "Library 2F" --nearest-ap     # only the nearest AP's radios
sudo eduroam-probe run --location "Library 2F" --min-signal -75 # stricter signal floor
sudo eduroam-probe run --location "Library 2F" --repeat 5 --interval 60
```
A full run scans and tests every eduroam radio at or above the signal floor, strongest first. A run stops early if the server rejects your credentials, so a typo can't lock your account. Set `PROBE_WPA_DEBUG=1` (`sudo PROBE_WPA_DEBUG=1 eduroam-probe run ...`) to save full wpa_supplicant debug output in the logs. For each radio it measures association, EAP authentication, 4-way handshake, DHCP, gateway and internet ping, DNS, HTTPS, link statistics, IPv6, and optionally iperf3. Results go to InfluxDB and to `/var/lib/eduroam-probe/results/` as JSON. Raw wpa_supplicant logs are kept per test in `/var/lib/eduroam-probe/logs/`. If InfluxDB is unreachable, writes queue in `/var/lib/eduroam-probe/pending/` and are retried on the next run.

### Data layout (InfluxDB bucket `eduroam`)
Three measurements. **[SCHEMA.md](SCHEMA.md) is the full contract** — every tag and
field with its type and unit — so another system can consume the data without reading
the agent's code.
- `wifi_scan`: one point per visible BSSID per run (all SSIDs, passive). Tags include probe, location, ssid, bssid, ap_group, band, channel, vendor_oui, country, security, pmf. Fields include signal_dbm, channel_width_mhz, beacon_interval_tu, supported_max_rate_mbps, station_count, channel_util_pct, ht/vht/he, wifi_gen, and the security flags.
- `wifi_scan_raw`: the complete, unmodified `iw scan` block for each AP, base64-encoded and keyed by a SHA-256 content hash. Written only when an AP's raw data changes (deduplicated), so nothing the radio reported is lost but the store stays compact. Disable with `CAPTURE_RAW=0`.
- `wifi_test`: one point per tested eduroam radio. Tags: probe, location, bssid, ap_group, band, channel, result, fail_stage. Fields: stage timings, DHCP lease details, ping, DNS and HTTP results, link stats, success, fail_reason.

`ap_group` is the first five octets of the BSSID, which on most enterprise APs groups a physical AP's radios together. `vendor_oui` is the first three octets (the MAC vendor prefix).

Pull the data back out with the InfluxDB API/Flux, or export annotated CSV with
`influx query '<flux>' --raw`. To read a raw block on any machine, decode its `raw_b64`
field with standard base64. See SCHEMA.md for query examples.

### Triggering a scan from the dashboard
Grafana can't run commands itself, so a small trigger service does it. Install it on the Pi:
```
sudo ./provision/install_trigger.sh
```
It prints an HTML snippet with two buttons and an access token. In Grafana, add a **Text** panel (Add > Visualization > Text), set **Mode = HTML**, paste the snippet, and Apply. The buttons:
- **Scan now (passive)** - a scan-only run, no login.
- **Full test, nearest AP** - logs in and tests the nearest AP's radios.

Clicking one starts the run in the background and returns immediately; new data shows on the dashboard's next refresh. The run shares the same lock as the timers, so it won't collide with a scheduled run. The token sits in the button URL, so keep the dashboard private (which it is). Remove with `sudo ./provision/install_trigger.sh --uninstall`.

## What provisioning sets up

- **Packages:** `iw`, `wpasupplicant`, `tcpdump`, `iperf3`, DNS tools, `sqlite3`, and Python.
- **Hostname and regulatory country:** `US` by default.
- **`wlan0` taken out of NetworkManager,** so the probe controls it directly. Ethernet stays managed.
- **Firmware roaming off** (`brcmfmac roamoff=1`), so a test stays on the access point it picked.
- **Wi-Fi power saving off** at every boot.
- **Directories:**
  - `/opt/eduroam-probe` for the code
  - `/var/lib/eduroam-probe` for results and logs
  - `/etc/eduroam-probe` for credentials (root-only). `probe.env` there holds the settings to fill in before the first connection test.

The script refuses to run if your SSH session is coming over Wi-Fi, since it would cut you off. Connect over Ethernet instead.

## Troubleshooting

- **Can't find the Pi on the network:** check your router's device list. You can also plug a keyboard and monitor in and run `ip addr`.
- **Regulatory domain shows `00`:** run `sudo raspi-config nonint do_wifi_country US`, then reboot.
- **Roaming check fails:** confirm that `/etc/modprobe.d/eduroam-probe-brcmfmac.conf` exists, then reboot.
- **No eduroam BSSIDs found:** run `rfkill list` to check the radio isn't blocked, and confirm you're within eduroam coverage.
