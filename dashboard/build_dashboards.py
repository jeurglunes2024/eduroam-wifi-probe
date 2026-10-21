#!/usr/bin/env python3
"""Generate the eduroam probe Grafana dashboards (InfluxDB 2 / Flux).

Writes three linked dashboards:
  overview.json      - what is broken right now, across every AP tested
  access-point.json  - drill-down for one AP (pick it by name)
  rf-survey.json     - everything in the air at a location
"""
import json

DS = {"type": "influxdb", "uid": "eduroam-influx"}
BUCKET = "eduroam"

# ---------------------------------------------------------------- thresholds
# Shared across dashboards so "red" means the same thing everywhere.
T = {
    "l2_total_ms":     (1000, 3000),   # association to keys
    "eap_ms":          (1000, 3000),
    "dhcp_ms":         (2000, 5000),
    "gw_rtt_avg_ms":   (20, 100),
    "gw_loss_pct":     (1, 5),
    "inet_loss_pct":   (1, 5),
    "dns1_ms":         (100, 500),
    "http_total_ms":   (1000, 3000),
    "channel_util_pct": (50, 75),
    "station_count":   (30, 60),
}
NICE = {
    "_time": "last tested", "ap_name": "AP", "building": "building", "location": "location",
    "band": "GHz", "channel": "ch", "bssid": "BSSID", "ap_group": "AP id",
    "success": "result", "fail_stage": "failed at", "fail_reason": "reason",
    "scan_signal_dbm": "signal", "signal_dbm": "signal", "channel_util_pct": "ch util",
    "station_count": "clients", "l2_total_ms": "connect", "eap_ms": "login (EAP)",
    "handshake_ms": "handshake", "assoc_ms": "assoc", "dhcp_ms": "DHCP",
    "gw_rtt_avg_ms": "gateway RTT", "gw_loss_pct": "gateway loss", "inet_loss_pct": "internet loss",
    "dns1_ms": "DNS", "http_total_ms": "HTTPS", "tx_bitrate_mbps": "tx rate", "ssid": "SSID",
    "wifi_gen": "gen", "deauth_reason": "deauth code",
}
UNIT = {"_ms": "ms", "_pct": "percent", "_dbm": "dBm", "_mbps": "Mbits"}


def steps(warn, crit, invert=False):
    if invert:  # low is bad (signal)
        return {"mode": "absolute", "steps": [{"color": "red", "value": None},
                                              {"color": "orange", "value": crit},
                                              {"color": "green", "value": warn}]}
    return {"mode": "absolute", "steps": [{"color": "green", "value": None},
                                          {"color": "orange", "value": warn},
                                          {"color": "red", "value": crit}]}


SIGNAL_STEPS = steps(-67, -75, invert=True)


def column_overrides(columns):
    """Readable names, units, and coloured cells for table columns."""
    out = []
    for c in columns:
        props = []
        if c in NICE:
            props.append({"id": "displayName", "value": NICE[c]})
        for suffix, unit in UNIT.items():
            if c.endswith(suffix):
                props.append({"id": "unit", "value": unit})
                if unit == "ms":
                    props.append({"id": "decimals", "value": 0})
        if c in T:
            props += [{"id": "thresholds", "value": steps(*T[c])},
                      {"id": "custom.cellOptions", "value": {"type": "color-text"}}]
        if c in ("scan_signal_dbm", "signal_dbm"):
            props += [{"id": "thresholds", "value": SIGNAL_STEPS},
                      {"id": "custom.cellOptions", "value": {"type": "color-text"}}]
        if c == "success":
            props += [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
                      {"id": "mappings", "value": [{"type": "value", "options": {
                          "true": {"text": "OK", "color": "green", "index": 0},
                          "false": {"text": "FAIL", "color": "red", "index": 1}}}]},
                      {"id": "custom.width", "value": 70}]
        if c == "fail_stage":
            props.append({"id": "mappings", "value": [{"type": "value", "options": {
                "scan": {"text": "not visible", "index": 0},
                "association": {"text": "association (AP)", "index": 1},
                "authentication": {"text": "login (RADIUS)", "index": 2},
                "handshake": {"text": "handshake (AP)", "index": 3},
                "dhcp": {"text": "DHCP", "index": 4},
                "dns": {"text": "DNS", "index": 5},
                "http": {"text": "HTTPS", "index": 6}}}]})
        if props:
            out.append({"matcher": {"id": "byName", "options": c}, "properties": props})
    return out


# ---------------------------------------------------------------- flux helpers
def rng():
    return f'from(bucket: "{BUCKET}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'


def flt(cond):
    return f"  |> filter(fn: (r) => {cond})\n"


def fields(names):
    return flt("contains(value: r._field, set: [" + ", ".join(f'"{n}"' for n in names) + "])")


def newest_row_per(key, field_list, measurement, pre=""):
    """One row per `key` (e.g. bssid): the newest point, with all of its fields side by side."""
    return (rng() + flt(f'r._measurement == "{measurement}" and exists r.{key}') + pre + fields(field_list) +
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            f'  |> group(columns: ["{key}"])\n'
            '  |> sort(columns: ["_time"])\n'
            '  |> last(column: "_time")\n'
            '  |> group()\n')


def keep(cols):
    return "  |> keep(columns: [" + ", ".join(f'"{c}"' for c in cols) + "])\n"


LOC = flt('r.location =~ /^${location:regex}$/')
AP = flt('r.ap_name =~ /^${ap:regex}$/')


# ---------------------------------------------------------------- panel builder
class Board:
    def __init__(self, uid, title, description):
        self.uid, self.title, self.description = uid, title, description
        self.panels, self.vars, self.pid, self.y = [], [], 0, 0

    def _p(self, kind, title, w, h, x, flux, defaults=None, overrides=None, options=None,
           description=None, links=None):
        self.pid += 1
        p = {"id": self.pid, "type": kind, "title": title, "datasource": DS,
             "gridPos": {"x": x, "y": self.y, "w": w, "h": h},
             "targets": [{"refId": "A", "datasource": DS, "query": flux}],
             "fieldConfig": {"defaults": defaults or {}, "overrides": overrides or []},
             "options": options or {}}
        if description:
            p["description"] = description
        if links:
            p["links"] = links
        self.panels.append(p)
        return p

    def row(self, title):
        self.pid += 1
        self.panels.append({"id": self.pid, "type": "row", "title": title, "collapsed": False,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.y += 1

    def line(self, specs, h):
        """Place panels left to right on one line: specs = [(width, builder_fn), ...]."""
        x = 0
        for w, fn in specs:
            fn(w, h, x)
            x += w
        self.y += h

    # panel kinds
    def stat(self, title, flux, unit="none", thresholds=None, decimals=None, color="value",
             no_value=None, description=None, display_name=None):
        d = {"unit": unit}
        if thresholds:
            d["thresholds"] = thresholds
        else:
            d["color"] = {"mode": "fixed", "fixedColor": "text"}
        if decimals is not None:
            d["decimals"] = decimals
        if no_value is not None:
            d["noValue"] = no_value
        if display_name:
            d["displayName"] = display_name
        opts = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "colorMode": color if thresholds else "none", "graphMode": "none",
                "textMode": "auto", "justifyMode": "auto"}
        return lambda w, h, x: self._p("stat", title, w, h, x, flux, d, options=opts, description=description)

    def table(self, title, flux, columns, extra_overrides=None, description=None, sort_by=None):
        opts = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}
        if sort_by:
            opts["sortBy"] = sort_by
        return lambda w, h, x: self._p("table", title, w, h, x, flux,
                                       {"custom": {"align": "auto", "filterable": True}},
                                       column_overrides(columns) + (extra_overrides or []),
                                       opts, description)

    def series(self, title, flux, unit, display, style="line", thresholds=None, description=None,
               minv=None, maxv=None, stack=False):
        custom = {"drawStyle": style, "lineWidth": 1, "pointSize": 5, "showPoints": "always",
                  "fillOpacity": 60 if style == "bars" else 10, "spanNulls": False}
        if stack:
            custom["stacking"] = {"mode": "normal", "group": "A"}
        d = {"unit": unit, "displayName": display, "custom": custom}
        if thresholds:
            d["thresholds"] = thresholds
            custom["thresholdsStyle"] = {"mode": "dashed"}
        if minv is not None:
            d["min"] = minv
        if maxv is not None:
            d["max"] = maxv
        opts = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                "tooltip": {"mode": "multi", "sort": "desc"}}
        return lambda w, h, x: self._p("timeseries", title, w, h, x, flux, d, options=opts,
                                       description=description)

    def bars(self, title, flux, unit, display, thresholds=None, maxv=None, description=None):
        d = {"unit": unit, "displayName": display, "min": 0, "decimals": 0}
        if maxv is not None:
            d["max"] = maxv
        if thresholds:
            d["thresholds"] = thresholds
        else:
            d["color"] = {"mode": "fixed", "fixedColor": "blue"}
        opts = {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                "valueMode": "color", "namePlacement": "left",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}
        return lambda w, h, x: self._p("bargauge", title, w, h, x, flux, d, options=opts,
                                       description=description)

    # variables
    def var_tag(self, name, label, tag, predicate, multi=True, include_all=True, hide=0):
        v = {"name": name, "label": label, "type": "query", "datasource": DS, "hide": hide,
             "query": ('import "influxdata/influxdb/schema"\n'
                       f'schema.tagValues(bucket: "{BUCKET}", tag: "{tag}", start: -90d, '
                       f'predicate: (r) => {predicate})'),
             "definition": f"{tag} tag values", "refresh": 2, "sort": 1,
             "multi": multi, "includeAll": include_all}
        if include_all:
            v["allValue"] = ".*"
            v["current"] = {"selected": True, "text": ["All"], "value": ["$__all"]}
        self.vars.append(v)

    def var_flux(self, name, flux, hide=2):
        self.vars.append({"name": name, "type": "query", "datasource": DS, "hide": hide,
                          "query": flux, "definition": name, "refresh": 2, "sort": 3,
                          "multi": True, "includeAll": True,
                          "current": {"selected": True, "text": ["All"], "value": ["$__all"]}})

    def json(self):
        return {
            "uid": self.uid, "title": self.title, "description": self.description,
            "tags": ["eduroam"], "timezone": "browser", "schemaVersion": 39, "version": 1,
            "editable": True, "refresh": "1m", "time": {"from": "now-24h", "to": "now"},
            "links": [{"type": "dashboards", "tags": ["eduroam"], "asDropdown": False,
                       "includeVars": False, "keepTime": True, "title": "eduroam dashboards"}],
            "templating": {"list": self.vars}, "panels": self.panels,
        }


TEST_COLS = ["success", "fail_reason", "scan_signal_dbm", "channel_util_pct", "station_count",
             "l2_total_ms", "eap_ms", "dhcp_ms", "gw_rtt_avg_ms", "gw_loss_pct", "inet_loss_pct",
             "dns1_ms", "http_total_ms"]


# ================================================================ 1. OVERVIEW
ov = Board("eduroam-overview", "eduroam Overview",
           "What is broken right now across every AP the probe has tested.")
ov.var_tag("location", "Location", "location", 'r._measurement == "wifi_test"')

latest_ok = (rng() + flt('r._measurement == "wifi_test" and r._field == "success" and exists r.bssid') + LOC +
             '  |> group(columns: ["bssid"])\n  |> sort(columns: ["_time"])\n  |> last()\n  |> group()\n')

# Newest test of every radio, rolled up to one row per AP.
AP_ROLLUP = (newest_row_per("bssid", TEST_COLS, "wifi_test", LOC) + """  |> map(fn: (r) => ({r with
        ap_name: if exists r.ap_name then r.ap_name else r.ap_group,
        building: if exists r.building then r.building else "",
        ok_i: if exists r.success and r.success then 1 else 0,
        conn: if exists r.l2_total_ms then r.l2_total_ms else 0.0,
        dhcp: if exists r.dhcp_ms then r.dhcp_ms else 0.0,
        loss: if exists r.gw_loss_pct then r.gw_loss_pct else 0.0,
        sig: if exists r.scan_signal_dbm then r.scan_signal_dbm else -200.0,
        util: if exists r.channel_util_pct then r.channel_util_pct else 0.0,
        sta: if exists r.station_count then r.station_count else 0,
        bad: if exists r.success and r.success then ""
             else r.band + " GHz: " + (if exists r.fail_stage then r.fail_stage else "unknown") + "  ",
        t: int(v: r._time)}))
  |> group(columns: ["ap_name", "building"])
  |> reduce(
      identity: {radios: 0, radios_ok: 0, worst_connect_ms: 0.0, worst_dhcp_ms: 0.0, worst_gw_loss_pct: 0.0,
                 best_signal_dbm: -200.0, max_util_pct: 0.0, clients: 0, failing: "", last_t: 0},
      fn: (r, accumulator) => ({
          radios: accumulator.radios + 1,
          radios_ok: accumulator.radios_ok + r.ok_i,
          worst_connect_ms: if r.conn > accumulator.worst_connect_ms then r.conn else accumulator.worst_connect_ms,
          worst_dhcp_ms: if r.dhcp > accumulator.worst_dhcp_ms then r.dhcp else accumulator.worst_dhcp_ms,
          worst_gw_loss_pct: if r.loss > accumulator.worst_gw_loss_pct then r.loss else accumulator.worst_gw_loss_pct,
          best_signal_dbm: if r.sig > accumulator.best_signal_dbm then r.sig else accumulator.best_signal_dbm,
          max_util_pct: if r.util > accumulator.max_util_pct then r.util else accumulator.max_util_pct,
          clients: accumulator.clients + r.sta,
          failing: accumulator.failing + r.bad,
          last_t: if r.t > accumulator.last_t then r.t else accumulator.last_t}))
  |> map(fn: (r) => ({r with
        status: if r.radios_ok == r.radios then "OK" else if r.radios_ok == 0 then "DOWN" else "DEGRADED",
        rank: if r.radios_ok == 0 then 0 else if r.radios_ok < r.radios then 1 else 2,
        radios_ok_text: string(v: r.radios_ok) + "/" + string(v: r.radios),
        last_tested: time(v: r.last_t)}))
  |> group()
""")

ov.row("Health (latest test of each AP)")
ov.line([
    (4, ov.stat("APs tested", rng() + flt('r._measurement == "wifi_test" and r._field == "success" and exists r.ap_group') + LOC +
                '  |> group(columns: ["ap_group"])\n  |> last()\n  |> group()\n  |> count()', no_value="0")),
    (4, ov.stat("APs fully healthy", AP_ROLLUP +
                "  |> map(fn: (r) => ({r with _value: if r.radios_ok == r.radios then 100.0 else 0.0}))\n"
                "  |> mean()", unit="percent", decimals=0, color="background",
                thresholds={"mode": "absolute", "steps": [{"color": "red", "value": None},
                                                          {"color": "orange", "value": 80},
                                                          {"color": "green", "value": 95}]},
                description="Share of APs whose every tested radio passed its latest test.")),
    (4, ov.stat("APs with a problem", AP_ROLLUP +
                '  |> filter(fn: (r) => r.radios_ok < r.radios)\n  |> count(column: "radios")',
                no_value="0", color="background",
                thresholds={"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                          {"color": "red", "value": 1}]},
                description="APs where at least one radio failed its latest test (DEGRADED or DOWN).")),
    (4, ov.stat("Radios failing now", latest_ok + "  |> filter(fn: (r) => r._value == false)\n  |> count()",
                no_value="0", color="background",
                thresholds={"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                          {"color": "red", "value": 1}]})),
    (4, ov.stat("Tests run", rng() + flt('r._measurement == "wifi_test" and r._field == "success"') + LOC +
                "  |> group()\n  |> count()", no_value="0")),
    (4, ov.stat("Success rate (all tests)", rng() + flt('r._measurement == "wifi_test" and r._field == "success"') + LOC +
                "  |> map(fn: (r) => ({r with _value: if r._value then 100.0 else 0.0}))\n  |> group()\n  |> mean()",
                unit="percent", decimals=0, color="background",
                thresholds={"mode": "absolute", "steps": [{"color": "red", "value": None},
                                                          {"color": "orange", "value": 80},
                                                          {"color": "green", "value": 95}]})),
], 4)

ov.row("Shared services (median across all APs; red everywhere means it is not the APs)")


def median_tile(title, field, desc):
    return ov.stat(title, rng() + flt(f'r._measurement == "wifi_test" and r._field == "{field}"') + LOC +
                   "  |> group()\n  |> median()", unit="ms", decimals=0, thresholds=steps(*T[field]),
                   color="background", description=desc)


ov.line([
    (6, median_tile("Login / RADIUS (EAP)", "eap_ms",
                    "Time for the RADIUS servers to authenticate. Slow on every AP = RADIUS or directory problem.")),
    (6, median_tile("DHCP", "dhcp_ms",
                    "Time to get an IP address. Slow or failing in one building = that scope or relay.")),
    (6, median_tile("DNS", "dns1_ms", "Lookup time against the DHCP-assigned resolver.")),
    (6, median_tile("HTTPS", "http_total_ms", "Full HTTPS request to the test URL.")),
], 4)

ov.row("Access points, worst first (click an AP name for its per-band detail)")
ap_cols = ["status", "ap_name", "building", "radios_ok_text", "failing", "best_signal_dbm", "worst_connect_ms",
           "worst_dhcp_ms", "worst_gw_loss_pct", "max_util_pct", "clients", "last_tested"]
DASH = [{"type": "value", "options": {"0": {"text": "-", "index": 0}, "-200": {"text": "-", "index": 1}}}]


def colored(name, label, unit, thr, decimals=None, dash=True):
    props = [{"id": "displayName", "value": label}, {"id": "unit", "value": unit},
             {"id": "thresholds", "value": thr}, {"id": "custom.cellOptions", "value": {"type": "color-text"}}]
    if decimals is not None:
        props.append({"id": "decimals", "value": decimals})
    if dash:
        props.append({"id": "mappings", "value": DASH})
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


ov.line([(24, ov.table(
    "Latest status per AP",
    AP_ROLLUP + '  |> sort(columns: ["rank", "ap_name"])\n' + keep(ap_cols),
    [],
    extra_overrides=[
        {"matcher": {"id": "byName", "options": "status"}, "properties": [
            {"id": "custom.cellOptions", "value": {"type": "color-background"}},
            {"id": "custom.width", "value": 100},
            {"id": "mappings", "value": [{"type": "value", "options": {
                "OK": {"color": "green", "index": 0}, "DEGRADED": {"color": "orange", "index": 1},
                "DOWN": {"color": "red", "index": 2}}}]}]},
        {"matcher": {"id": "byName", "options": "ap_name"}, "properties": [
            {"id": "displayName", "value": "AP"},
            {"id": "links", "value": [{"title": "Open this AP",
                                       "url": "/d/eduroam-ap/eduroam-access-point?var-ap=${__value.raw}&${__url_time_range}"}]}]},
        {"matcher": {"id": "byName", "options": "radios_ok_text"}, "properties": [
            {"id": "displayName", "value": "radios OK"}, {"id": "custom.width", "value": 90}]},
        {"matcher": {"id": "byName", "options": "failing"}, "properties": [
            {"id": "displayName", "value": "failing radios (stage)"},
            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
            {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]},
        colored("best_signal_dbm", "best signal", "dBm", SIGNAL_STEPS),
        colored("worst_connect_ms", "worst connect", "ms", steps(*T["l2_total_ms"]), 0),
        colored("worst_dhcp_ms", "worst DHCP", "ms", steps(*T["dhcp_ms"]), 0),
        colored("worst_gw_loss_pct", "worst gateway loss", "percent", steps(*T["gw_loss_pct"]), dash=False),
        colored("max_util_pct", "peak ch util", "percent", steps(*T["channel_util_pct"]), 0),
        {"matcher": {"id": "byName", "options": "clients"}, "properties": [
            {"id": "displayName", "value": "clients (all radios)"}]},
        {"matcher": {"id": "byName", "options": "last_tested"}, "properties": [
            {"id": "displayName", "value": "last tested"}, {"id": "unit", "value": "dateTimeFromNow"}]},
    ],
    description="One row per physical AP, combining the newest test of each of its radios. "
                "DEGRADED = some radios failing, DOWN = all failing. Open the AP for per-band detail."))], 11)

# Latest scan per BSSID, carrying its security fields. Used by the Security row.
SEC_SCAN = (newest_row_per("bssid", ["signal_dbm", "flag_count", "insecure", "security_flags", "auth_suites", "wps"],
                           "wifi_scan", LOC))

ov.row("Wi-Fi security (passive: what each network advertises)")
ov.line([
    (6, ov.stat("Networks with findings", SEC_SCAN + '  |> filter(fn: (r) => r.insecure == 1)\n  |> count(column: "insecure")',
                no_value="0", color="background",
                thresholds={"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                          {"color": "orange", "value": 1}]},
                description="Scanned BSSIDs carrying at least one security flag.")),
    (6, ov.stat("Rogue eduroam suspects", SEC_SCAN +
                '  |> filter(fn: (r) => exists r.security_flags and (r.security_flags =~ /rogue_enc/ or r.security_flags =~ /unknown_vendor/))\n'
                '  |> count(column: "insecure")', no_value="0", color="background",
                thresholds={"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                          {"color": "red", "value": 1}]},
                description="eduroam BSSIDs that are not WPA-Enterprise, or whose MAC vendor is not in KNOWN_OUIS. "
                            "Investigate these: they may be impersonating eduroam.")),
    (6, ov.stat("Certificate mismatches", rng() +
                flt('r._measurement == "wifi_test" and r._field == "fail_reason"') + LOC +
                '  |> filter(fn: (r) => r._value =~ /tls_cert_error/)\n  |> group()\n  |> count()',
                no_value="0", color="background",
                thresholds={"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                          {"color": "red", "value": 1}]},
                description="Logins where the RADIUS server certificate did not match the pinned one. "
                            "A renewed cert, or an impersonator.")),
    (6, ov.stat("eduroam radios without PMF",
                rng() + flt('r._measurement == "wifi_scan" and r._field == "signal_dbm" and r.ssid == "eduroam" and r.pmf == "off"') + LOC +
                '  |> group(columns: ["bssid"])\n  |> last()\n  |> group()\n  |> count()',
                no_value="0", color="background",
                thresholds={"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                          {"color": "orange", "value": 1}]},
                description="eduroam radios with protected management frames (802.11w) off. "
                            "Without PMF, forged disconnect frames can knock users off.")),
], 4)
ov.line([(24, ov.table(
    "Security findings (latest scan per BSSID)",
    SEC_SCAN + '  |> filter(fn: (r) => r.flag_count > 0)\n' +
    keep(["ssid", "ap_name", "building", "band", "channel", "security", "pmf", "wps", "security_flags",
          "auth_suites", "signal_dbm", "bssid", "_time"]) +
    '  |> sort(columns: ["ssid", "signal_dbm"], desc: true)',
    ["ssid", "ap_name", "building", "band", "channel", "security", "pmf", "wps", "security_flags",
     "auth_suites", "signal_dbm", "bssid", "_time"],
    extra_overrides=[
        {"matcher": {"id": "byName", "options": "security_flags"}, "properties": [
            {"id": "displayName", "value": "flags"},
            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
            {"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}},
            {"id": "custom.width", "value": 260}]},
        {"matcher": {"id": "byName", "options": "security"}, "properties": [
            {"id": "custom.cellOptions", "value": {"type": "color-background"}},
            {"id": "mappings", "value": [{"type": "regex", "options": {
                "pattern": ".*enterprise", "result": {"color": "dark-green", "index": 0}}},
                {"type": "value", "options": {
                    "open": {"color": "dark-red", "index": 1}, "wep": {"color": "dark-red", "index": 2},
                    "wpa1": {"color": "dark-red", "index": 3},
                    "wpa2-personal": {"color": "dark-orange", "index": 4},
                    "wpa2/3-personal": {"color": "dark-orange", "index": 5},
                    "wpa3-personal": {"color": "dark-orange", "index": 6}}}]}]},
        {"matcher": {"id": "byName", "options": "pmf"}, "properties": [
            {"id": "mappings", "value": [{"type": "value", "options": {
                "off": {"color": "red", "index": 0}, "capable": {"color": "orange", "index": 1},
                "required": {"color": "green", "index": 2}}}]},
            {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
    ],
    description="Every network (any SSID) carrying a security flag, from its most recent scan. "
                "rogue_enc / unknown_vendor on an eduroam row is the one to act on first."))], 9)

ov.row("Failures")
ov.line([
    (12, ov.series("Failures over time, by stage",
                   rng() + flt('r._measurement == "wifi_test" and r._field == "success" and r.result == "fail"') + LOC +
                   '  |> group(columns: ["fail_stage"])\n'
                   '  |> aggregateWindow(every: v.windowPeriod, fn: count, createEmpty: false)',
                   "none", "${__field.labels.fail_stage}", style="bars", stack=True,
                   description="Stacked count of failed tests by the stage they failed at.")),
    (12, ov.table("Recent failures",
                  rng() + flt('r._measurement == "wifi_test" and r.result == "fail"') + LOC +
                  fields(["fail_reason", "scan_signal_dbm", "deauth_reason"]) +
                  '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n  |> group()\n' +
                  keep(["_time", "ap_name", "band", "fail_stage", "fail_reason", "deauth_reason", "scan_signal_dbm", "location"]) +
                  '  |> sort(columns: ["_time"], desc: true)\n  |> limit(n: 100)',
                  ["_time", "ap_name", "band", "fail_stage", "fail_reason", "deauth_reason", "scan_signal_dbm", "location"])),
], 9)


# ================================================================ 2. ACCESS POINT
ap = Board("eduroam-ap", "eduroam Access Point", "Everything the probe knows about one access point.")
ap.var_tag("ap", "Access point", "ap_name", 'r._measurement == "wifi_scan" and r.ssid == "eduroam"',
           multi=False, include_all=False)
ap.var_flux("chan", 'from(bucket: "eduroam")\n  |> range(start: -30d)\n'
            '  |> filter(fn: (r) => r._measurement == "wifi_scan" and r._field == "signal_dbm" and r.ap_name == "${ap}")\n'
            '  |> keep(columns: ["channel"])\n  |> group()\n  |> distinct(column: "channel")')

ap.row("Radios on this AP (latest)")
radio_cols = ["success", "band", "channel", "fail_stage", "fail_reason", "scan_signal_dbm", "channel_util_pct",
              "station_count", "l2_total_ms", "eap_ms", "dhcp_ms", "gw_rtt_avg_ms", "gw_loss_pct",
              "dns1_ms", "http_total_ms", "bssid", "_time"]
ap.line([(24, ap.table("Latest test per radio",
                       newest_row_per("bssid", TEST_COLS, "wifi_test", AP) + keep(radio_cols) +
                       '  |> sort(columns: ["band"])', radio_cols))], 5)

scan_ap = rng() + flt('r._measurement == "wifi_scan"') + AP
ap.line([
    (8, ap.stat("Signal at probe", scan_ap + flt('r._field == "signal_dbm"') +
                '  |> group(columns: ["band"])\n  |> sort(columns: ["_time"])\n  |> last()', unit="dBm", thresholds=SIGNAL_STEPS,
                display_name="${__field.labels.band} GHz", color="background")),
    (8, ap.stat("Channel utilization", scan_ap + flt('r._field == "channel_util_pct"') +
                '  |> group(columns: ["band"])\n  |> sort(columns: ["_time"])\n  |> last()', unit="percent", decimals=0,
                thresholds=steps(*T["channel_util_pct"]), display_name="${__field.labels.band} GHz",
                color="background")),
    (8, ap.stat("Clients associated", scan_ap + flt('r._field == "station_count"') +
                '  |> group(columns: ["band"])\n  |> sort(columns: ["_time"])\n  |> last()', thresholds=steps(*T["station_count"]),
                display_name="${__field.labels.band} GHz", color="background")),
], 4)

ap.row("Connection stages over time (per band)")


def ap_series(title, field, unit="ms", style="line", desc=None):
    return ap.series(title, rng() + flt(f'r._measurement == "wifi_test" and r._field == "{field}"') + AP +
                     '  |> group(columns: ["band"])\n'
                     '  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)',
                     unit, "${__field.labels.band} GHz", style=style,
                     thresholds=steps(*T[field]) if field in T else None, description=desc)


ap.line([
    (8, ap_series("Connect (association to keys)", "l2_total_ms")),
    (8, ap_series("Login (EAP)", "eap_ms", desc="RADIUS time. Compare with the Overview median before blaming the AP.")),
    (8, ap_series("DHCP", "dhcp_ms")),
], 7)
ap.line([
    (8, ap_series("Gateway latency", "gw_rtt_avg_ms", desc="Round trip to the default gateway: the AP's wired path.")),
    (8, ap_series("Gateway packet loss", "gw_loss_pct", unit="percent", style="bars")),
    (8, ap.series("Link quality (retries, failures, beacon loss)",
                  rng() + flt('r._measurement == "wifi_test"') + AP +
                  fields(["tx_retries", "tx_failed", "beacon_loss"]) +
                  '  |> group(columns: ["band", "_field"])\n'
                  '  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)',
                  "none", "${__field.labels._field} (${__field.labels.band} GHz)", style="bars",
                  description="High retries or beacon loss point to interference or a struggling radio.")),
], 7)

ap.row("RF around this AP")
ap.line([
    (12, ap.series("Signal and utilization over time",
                   scan_ap + fields(["signal_dbm", "channel_util_pct"]) +
                   '  |> group(columns: ["band", "_field"])\n'
                   '  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)',
                   "none", "${__field.labels._field} (${__field.labels.band} GHz)")),
    (12, ap.table("Other networks on this AP's channel(s) (latest)",
                  newest_row_per("bssid", ["signal_dbm", "channel_util_pct", "station_count"], "wifi_scan",
                                 flt('r.channel =~ /^${chan:regex}$/')) +
                  keep(["ssid", "ap_name", "band", "channel", "signal_dbm", "channel_util_pct", "station_count", "bssid"]) +
                  '  |> sort(columns: ["signal_dbm"], desc: true)',
                  ["ssid", "ap_name", "band", "channel", "signal_dbm", "channel_util_pct", "station_count", "bssid"],
                  description="Co-channel neighbours compete for airtime. Many strong ones = interference.")),
], 8)

ap.row("Test history")
hist_cols = ["_time", "success", "band", "fail_stage", "fail_reason", "scan_signal_dbm", "l2_total_ms", "eap_ms",
             "handshake_ms", "dhcp_ms", "gw_rtt_avg_ms", "gw_loss_pct", "inet_loss_pct", "dns1_ms", "http_total_ms",
             "tx_bitrate_mbps", "location"]
ap.line([(24, ap.table("All tests of this AP",
                       rng() + flt('r._measurement == "wifi_test"') + AP +
                       fields(TEST_COLS + ["handshake_ms", "tx_bitrate_mbps"]) +
                       '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n  |> group()\n' +
                       keep(hist_cols) + '  |> sort(columns: ["_time"], desc: true)\n  |> limit(n: 200)',
                       hist_cols))], 10)


# ================================================================ 3. RF SURVEY
rf = Board("eduroam-rf", "eduroam RF Survey", "Everything the probe can hear at a location.")
rf.var_tag("location", "Location", "location", 'r._measurement == "wifi_scan"')
rf.var_tag("ssid", "SSID", "ssid", 'r._measurement == "wifi_scan"')

latest_scan = lambda pre="": newest_row_per("bssid", ["signal_dbm", "channel_util_pct", "station_count", "wifi_gen"],
                                            "wifi_scan", LOC + pre)
rf.row("Summary (latest scan at the selected location)")
rf.line([
    (6, rf.stat("Networks (BSSIDs) heard", latest_scan() + "  |> count(column: \"signal_dbm\")", no_value="0")),
    (6, rf.stat("eduroam radios heard", latest_scan(flt('r.ssid == "eduroam"')) + "  |> count(column: \"signal_dbm\")",
                no_value="0")),
    (6, rf.stat("eduroam radios usable (>= -75 dBm)",
                latest_scan(flt('r.ssid == "eduroam"')) + "  |> filter(fn: (r) => r.signal_dbm >= -75.0)\n"
                "  |> count(column: \"signal_dbm\")", no_value="0")),
    (6, rf.stat("Busiest eduroam channel", latest_scan(flt('r.ssid == "eduroam"')) +
                '  |> max(column: "channel_util_pct")\n  |> keep(columns: ["channel_util_pct"])',
                unit="percent", decimals=0, thresholds=steps(*T["channel_util_pct"]), color="background")),
], 4)

rf.row("Channels")
rf.line([
    (12, rf.bars("Networks per channel (all SSIDs)",
                 latest_scan() + '  |> group(columns: ["band", "channel"])\n  |> count(column: "signal_dbm")\n'
                 '  |> rename(columns: {signal_dbm: "_value"})',
                 "none", "${__field.labels.band} GHz ch ${__field.labels.channel}",
                 description="How crowded each channel is. Every network on a channel shares its airtime.")),
    (12, rf.bars("Channel utilization reported by eduroam APs",
                 latest_scan(flt('r.ssid == "eduroam"')) +
                 '  |> filter(fn: (r) => exists r.channel_util_pct)\n'
                 '  |> group(columns: ["band", "channel"])\n  |> max(column: "channel_util_pct")\n'
                 '  |> rename(columns: {channel_util_pct: "_value"})',
                 "percent", "${__field.labels.band} GHz ch ${__field.labels.channel}",
                 thresholds=steps(*T["channel_util_pct"]), maxv=100)),
], 9)

rf.row("Networks heard")
scan_cols = ["ssid", "ap_name", "building", "band", "channel", "signal_dbm", "channel_util_pct", "station_count",
             "wifi_gen", "bssid", "_time"]
rf.line([(24, rf.table("Latest scan, strongest first",
                       latest_scan(flt('r.ssid =~ /^${ssid:regex}$/')) + keep(scan_cols) +
                       '  |> sort(columns: ["signal_dbm"], desc: true)', scan_cols))], 11)
rf.line([(24, rf.series("Signal over time by BSSID",
                        rng() + flt('r._measurement == "wifi_scan" and r._field == "signal_dbm"') + LOC +
                        flt('r.ssid =~ /^${ssid:regex}$/') +
                        '  |> group(columns: ["ap_name", "band", "bssid"])\n'
                        '  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)',
                        "dBm", "${__field.labels.ap_name} ${__field.labels.band} GHz",
                        thresholds=SIGNAL_STEPS))], 9)


for board, fname in ((ov, "overview.json"), (ap, "access-point.json"), (rf, "rf-survey.json")):
    with open(fname, "w") as fh:
        json.dump(board.json(), fh, indent=2)
    print(f"wrote {fname}: {len([p for p in board.panels if p['type'] != 'row'])} panels")
