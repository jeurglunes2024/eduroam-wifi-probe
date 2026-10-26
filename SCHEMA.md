# Data schema

All probe data lands in one InfluxDB 2.x bucket (`eduroam` by default) as three
measurements. This document is the contract: field names, types, and units are
stable so another system can consume the data without reading the probe's code.

Conventions used throughout:
- **Timestamps** are UTC, millisecond precision (InfluxDB `ms`).
- **Tags** are indexed, low-cardinality dimensions you filter or group by. They
  are always strings.
- **Fields** carry the measured values. Units are in the field name
  (`_ms`, `_mhz`, `_dbm`, `_mbps`, `_pct`, `_tu`). Types never change for a given
  field: an integer field stays integer, a float stays float.
- A field is omitted from a point when the value is unknown (e.g. an AP that
  does not advertise channel utilization). Consumers should treat a missing
  field as "not reported," not zero.
- BSSIDs are lowercase, colon-separated. `vendor_oui` is the first three octets.

---

## `wifi_scan` — one point per AP radio heard, per scan

Passive observation of every network in range (all SSIDs, not just eduroam).
Written every scan.

**Tags**

| tag | meaning |
|---|---|
| `probe` | probe host that took the measurement |
| `location` | operator-supplied location tag for this run |
| `ssid` | network name (`<hidden>` if not broadcast) |
| `bssid` | the radio's MAC address (unique per AP radio) |
| `ap_group` | first five octets of the BSSID (groups an AP's radios) |
| `ap_name`, `building` | friendly names from `ap_names.csv`, if set |
| `band` | `2.4`, `5`, or `6` |
| `channel` | primary channel number |
| `vendor_oui` | MAC vendor prefix (first three octets) |
| `country` | regulatory country the AP advertises, if any |
| `security` | `open`, `wep`, `wpa1`, `wpa2-personal`, `wpa2-enterprise`, `wpa3-personal`, `wpa3-enterprise`, or a mix |
| `pmf` | protected management frames: `required`, `capable`, `off` |

**Fields**

| field | type | unit | meaning |
|---|---|---|---|
| `signal_dbm` | float | dBm | received signal strength at the probe |
| `freq_mhz` | int | MHz | channel center frequency |
| `channel_width_mhz` | int | MHz | 20 / 40 / 80 / 160 |
| `beacon_interval_tu` | int | TU (1 TU = 1.024 ms) | beacon period |
| `supported_max_rate_mbps` | float | Mbit/s | highest legacy supported rate advertised |
| `station_count` | int | — | clients the AP reports on this radio (BSS Load) |
| `channel_util_pct` | float | % | channel busy time the AP reports (BSS Load) |
| `wifi_gen` | string | — | `legacy`, `n`, `ac`, `ax` |
| `ht`, `vht`, `he` | bool | — | 802.11n / ac / ax capability advertised |
| `ess` | bool | — | infrastructure (ESS) bit |
| `wps` | bool | — | WPS advertised |
| `security_flags` | string | — | comma-separated findings (see below) |
| `flag_count` | int | — | number of security flags |
| `insecure` | int | — | 1 if any flag is set, else 0 (for easy alerting) |
| `auth_suites` | string | — | raw 802.1X / PSK / SAE suites offered |

`security_flags` values: `open`, `wep`, `wpa1`, `tkip`, `no_pmf`, `pmf_optional`,
`wps_on`, `rogue_enc` (an eduroam SSID that is not WPA-Enterprise),
`unknown_vendor` (OUI not in `KNOWN_OUIS`).

---

## `wifi_scan_raw` — complete raw scan per AP, on change

The full, unmodified `iw dev <iface> scan` block for each BSSID, so nothing the
radio reported is lost and the record can be re-parsed later by any tool. To
keep the store compact it is **deduplicated**: a new point is written only when
the raw content for a BSSID changes (the probe tracks a SHA-256 per BSSID in
`/var/lib/eduroam-probe/raw_seen.json`). Set `CAPTURE_RAW=0` in `probe.env` to
disable raw capture.

**Tags**

| tag | meaning |
|---|---|
| `probe`, `location`, `bssid`, `ap_name` | as in `wifi_scan` |
| `content_sha256` | SHA-256 of the raw block (dedup key / integrity check) |
| `encoding` | always `base64` |

**Fields**

| field | type | meaning |
|---|---|---|
| `raw_b64` | string | the full raw scan block, base64-encoded (single line, decodes on any machine) |
| `raw_len` | int | length in bytes of the decoded text |

Decode example (any language; shown with the InfluxDB CLI + base64):
```bash
influx query 'from(bucket:"eduroam") |> range(start:-7d)
  |> filter(fn:(r)=> r._measurement=="wifi_scan_raw" and r._field=="raw_b64")
  |> last()' --raw | <extract the value> | base64 -d
```

---

## `wifi_test` — one point per eduroam radio tested

Active connection test results (requires credentials). Tags: `probe`,
`location`, `bssid`, `ap_group`, `ap_name`, `building`, `band`, `channel`,
`result` (`ok`/`fail`), `fail_stage`. Fields: per-stage timings
(`assoc_ms`, `eap_ms`, `handshake_ms`, `l2_total_ms`, `dhcp_ms`), network
results (`gw_rtt_avg_ms`, `gw_loss_pct`, `inet_loss_pct`, `dns1_ms`,
`http_total_ms`), link quality (`tx_retries`, `tx_failed`, `beacon_loss`,
`tx_bitrate_mbps`), `success` (bool), and `fail_reason`.

---

## Getting the data out

- **Flux / InfluxDB API** — query any measurement directly; this is the primary
  machine interface.
- **CSV export** — `influx query '<flux>' --raw` returns annotated CSV
  (RFC 4180, with InfluxDB's datatype header), suitable for a spreadsheet or an
  ETL job.
- **Retention** — set a bucket retention policy to age out old data as your
  operation requires; the schema does not assume infinite retention.
