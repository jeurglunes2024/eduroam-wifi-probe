#!/usr/bin/env python3
"""eduroam probe agent.

Scans for eduroam and, by default, connects to every eduroam radio in range
(strongest first, above a signal floor). For each one it pins the BSSID,
radios with the BSSID pinned, times every stage of the connection, measures
the network behind it, and writes everything to InfluxDB for Grafana.

Standard library only. Run as root.

    sudo ./probe.py doctor                 # check tools and settings
    sudo ./probe.py scan                   # show visible eduroam BSSIDs
    sudo ./probe.py run --location "Library 2F"
    sudo ./probe.py run --location "Library 2F" --repeat 5 --interval 60
    sudo ./probe.py run --scan-only        # RF data only, no credentials needed

Settings come from /etc/eduroam-probe/probe.env and influx.env.
"""

import argparse
import csv
import getpass
import ipaddress
import json
import os
import queue
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

VERSION = "0.5.0"
ETC = Path("/etc/eduroam-probe")
DATA = Path("/var/lib/eduroam-probe")
RUN = Path("/run/eduroam-probe")
POLICY_TABLE = "100"
POLICY_PRIO = "1000"
MAIN_METRIC = "900"
SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"


# --------------------------------------------------------------------- helpers

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sh(cmd, timeout=30, check=False):
    """Run a command, return (returncode, combined output)."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {p.stdout.strip()}")
    return p.returncode, p.stdout


def load_env(*paths):
    cfg = {}
    for path in paths:
        try:
            for line in Path(path).read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                cfg[k.strip()] = v
        except FileNotFoundError:
            pass
    return cfg


def load_ap_names(path=None):
    """Read ap_names.csv: id,name,building. id is a BSSID or an AP group (first 5 octets)."""
    path = Path(path or ETC / "ap_names.csv")
    names = {}
    try:
        with open(path, newline="") as fh:
            for row in csv.reader(fh):
                if not row or row[0].strip().startswith("#"):
                    continue
                key = row[0].strip().lower()
                names[key] = {"ap_name": (row[1].strip() if len(row) > 1 else "") or None,
                              "building": (row[2].strip() if len(row) > 2 else "") or None}
    except FileNotFoundError:
        pass
    return names


def name_scan(scan, names):
    """Attach a readable AP name and building to each BSSID (default name: its AP group)."""
    for b in scan:
        hit = names.get(b["bssid"].lower()) or names.get(b["ap_group"]) or {}
        b["ap_name"] = hit.get("ap_name") or b["ap_group"]
        b["building"] = hit.get("building")
    return scan


def apply_rogue_flags(scan, ssid, cfg):
    """Flag impersonation risks on networks advertising our SSID.

    - rogue_enc: an <ssid> network that is not WPA-Enterprise (open/WEP/WPA1/PSK)
      is almost certainly not the real eduroam.
    - unknown_vendor: its MAC OUI is not in KNOWN_OUIS (only when that list is set).
    Both are appended to the BSS's existing security_flags.
    """
    ouis = [o.strip().lower() for o in (cfg.get("KNOWN_OUIS") or "").split(",") if o.strip()]
    for b in scan:
        if b["ssid"] != ssid:
            continue
        extra = []
        if b["security"] not in ("wpa2-enterprise", "wpa3-enterprise"):
            extra.append("rogue_enc")
        if ouis and b["bssid"][:8].lower() not in ouis:
            extra.append("unknown_vendor")
        if extra:
            b["security_flags"] = ",".join(f for f in [b["security_flags"], *extra] if f)
    return scan


def freq_to_band_channel(freq):
    if freq == 2484:
        return "2.4", 14
    if 2400 <= freq < 2500:
        return "2.4", (freq - 2407) // 5
    if 5000 <= freq < 5925:
        return "5", (freq - 5000) // 5
    if 5925 <= freq < 7125:
        return "6", (freq - 5950) // 5
    return "?", 0


def ap_group(bssid):
    """Radios on one physical AP usually share the first five octets."""
    return bssid.lower()[:14]


def md4(data):
    """MD4 (RFC 1320). hashlib often lacks it on OpenSSL 3, so it lives here."""
    mask = 0xFFFFFFFF

    def rol(v, s):
        v &= mask
        return ((v << s) | (v >> (32 - s))) & mask

    msg = bytearray(data)
    bit_len = (8 * len(data)) & 0xFFFFFFFFFFFFFFFF
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += struct.pack("<Q", bit_len)
    rounds = [
        (lambda x, y, z: (x & y) | (~x & z), 0,
         range(16), (3, 7, 11, 19)),
        (lambda x, y, z: (x & y) | (x & z) | (y & z), 0x5A827999,
         (0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15), (3, 5, 9, 13)),
        (lambda x, y, z: x ^ y ^ z, 0x6ED9EBA1,
         (0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15), (3, 9, 11, 15)),
    ]
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476]
    for off in range(0, len(msg), 64):
        X = struct.unpack("<16I", bytes(msg[off:off + 64]))
        A, B, C, D = h
        for fn, const, order, shifts in rounds:
            for i, k in enumerate(order):
                s = shifts[i % 4]
                if i % 4 == 0:
                    A = rol(A + fn(B, C, D) + X[k] + const, s)
                elif i % 4 == 1:
                    D = rol(D + fn(A, B, C) + X[k] + const, s)
                elif i % 4 == 2:
                    C = rol(C + fn(D, A, B) + X[k] + const, s)
                else:
                    B = rol(B + fn(C, D, A) + X[k] + const, s)
        h = [(h[0] + A) & mask, (h[1] + B) & mask, (h[2] + C) & mask, (h[3] + D) & mask]
    return struct.pack("<4I", *h)


def nt_hash(password):
    return md4(password.encode("utf-16-le")).hex()


def uses_mschapv2(cfg):
    return "MSCHAPV2" in cfg.get("EAP_PHASE2", "auth=MSCHAPV2").upper()


# --------------------------------------------------------------------- parsers

def new_bss(bssid):
    # _raw_* fields are gathered during parsing; classify_security() reduces
    # them to the stored security fields and is popped off before writing.
    return {"bssid": bssid, "ssid": "", "freq": 0, "signal": None,
            "station_count": None, "channel_util_pct": None, "wifi_gen": "legacy",
            "_privacy": False, "_has_rsn": False, "_has_wpa1": False, "_wps": False,
            "_rsn_auth": "", "_rsn_pairwise": "", "_rsn_group": "", "_rsn_caps": "",
            "_wpa_pairwise": ""}


def classify_security(b):
    """Reduce the raw RSN/WPA/WPS fields to an encryption label, PMF state and flags."""
    auth, pair = b["_rsn_auth"], b["_rsn_pairwise"]
    flags = []

    # Encryption family
    if b["_has_rsn"]:
        sae = "SAE" in auth
        dot1x = "802.1X" in auth or "8021X" in auth.replace(".", "").replace("/", "")
        psk = re.search(r"\bPSK\b", auth) is not None
        suiteb_or_sha = "SHA256" in auth.replace("-", "").replace(" ", "") or "SHA-256" in auth or "SuiteB" in auth
        if dot1x:
            enc = "wpa3-enterprise" if suiteb_or_sha and "MFP-required" in b["_rsn_caps"] else "wpa2-enterprise"
        elif sae and psk:
            enc = "wpa2/3-personal"
        elif sae:
            enc = "wpa3-personal"
        elif psk:
            enc = "wpa2-personal"
        else:
            enc = "wpa2"
    elif b["_has_wpa1"]:
        enc = "wpa1"
    elif b["_privacy"]:
        enc = "wep"
    else:
        enc = "open"

    # PMF / 802.11w
    if "MFP-required" in b["_rsn_caps"]:
        pmf = "required"
    elif "MFP-capable" in b["_rsn_caps"]:
        pmf = "capable"
    else:
        pmf = "off"

    # Flags (the actionable findings)
    if enc == "open":
        flags.append("open")
    if enc == "wep":
        flags.append("wep")
    if enc == "wpa1" or b["_has_wpa1"]:
        flags.append("wpa1")
    if "TKIP" in pair or "TKIP" in b["_wpa_pairwise"] or "TKIP" in b["_rsn_group"]:
        flags.append("tkip")
    if b["_has_rsn"] and pmf == "off":
        flags.append("no_pmf")
    elif pmf == "capable":
        flags.append("pmf_optional")
    if b["_wps"]:
        flags.append("wps_on")

    b["security"] = enc
    b["pmf"] = pmf
    b["wps"] = b["_wps"]
    b["auth_suites"] = auth.strip() or None
    b["security_flags"] = ",".join(flags)
    for k in list(b):
        if k.startswith("_"):
            del b[k]
    return b


def parse_scan(text):
    """Parse `iw dev X scan` into a list of dicts (including security fields)."""
    out, cur, section, crypto = [], None, None, None
    for raw in text.splitlines():
        m = re.match(r"^BSS ([0-9a-f:]{17})", raw)
        if m:
            cur = new_bss(m.group(1))
            out.append(cur)
            section, crypto = None, None
            continue
        if cur is None:
            continue
        line = raw.strip()
        top_level = raw.startswith("\t") and not raw.startswith("\t\t")
        if top_level:
            section = line.rstrip(":")
            if line.startswith("RSN:"):
                crypto, cur["_has_rsn"] = "rsn", True
            elif line.startswith("WPA:"):
                crypto, cur["_has_wpa1"] = "wpa", True
            elif line.startswith("WPS:"):
                crypto, cur["_wps"] = None, True
            else:
                crypto = None
        if line.startswith("freq:"):
            cur["freq"] = int(float(line.split()[1]))
        elif line.startswith("signal:"):
            cur["signal"] = float(line.split()[1])
        elif line.startswith("SSID:"):
            cur["ssid"] = line[5:].strip()
        elif line.startswith("capability:"):
            cur["_privacy"] = "Privacy" in line
        elif line.startswith("last seen:"):
            m2 = re.search(r"(\d+) ms", line)
            if m2:
                cur["last_seen_ms"] = int(m2.group(1))
        elif section == "BSS Load" and "station count:" in line:
            cur["station_count"] = int(line.split(":")[1])
        elif section == "BSS Load" and "channel utilisation:" in line:
            m2 = re.search(r"(\d+)/255", line)
            if m2:
                cur["channel_util_pct"] = round(int(m2.group(1)) * 100 / 255, 1)
        elif crypto == "rsn" and "Authentication suites:" in line:
            cur["_rsn_auth"] = line.split(":", 1)[1].strip()
        elif crypto == "rsn" and "Pairwise ciphers:" in line:
            cur["_rsn_pairwise"] = line.split(":", 1)[1].strip()
        elif crypto == "rsn" and "Group cipher:" in line:
            cur["_rsn_group"] = line.split(":", 1)[1].strip()
        elif crypto == "rsn" and "Capabilities:" in line:
            cur["_rsn_caps"] = line.split(":", 1)[1].strip()
        elif crypto == "wpa" and "Pairwise ciphers:" in line:
            cur["_wpa_pairwise"] = line.split(":", 1)[1].strip()
        elif line.startswith("HE capabilities"):
            cur["wifi_gen"] = "ax"
        elif line.startswith("VHT capabilities") and cur["wifi_gen"] != "ax":
            cur["wifi_gen"] = "ac"
        elif line.startswith("HT capabilities") and cur["wifi_gen"] == "legacy":
            cur["wifi_gen"] = "n"
    for b in out:
        b["band"], b["channel"] = freq_to_band_channel(b["freq"])
        b["ap_group"] = ap_group(b["bssid"])
        classify_security(b)
    return out


def parse_link(text):
    """Parse `iw dev X link` / `station dump` output into numeric fields."""
    f = {}
    pats = {
        "signal_dbm": r"signal:\s*(-?\d+)",
        "tx_bitrate_mbps": r"tx bitrate:\s*([\d.]+)",
        "rx_bitrate_mbps": r"rx bitrate:\s*([\d.]+)",
        "tx_retries": r"tx retries:\s*(\d+)",
        "tx_failed": r"tx failed:\s*(\d+)",
        "beacon_loss": r"beacon loss:\s*(\d+)",
        "tx_packets": r"tx packets:\s*(\d+)",
        "rx_packets": r"rx packets:\s*(\d+)",
    }
    for k, p in pats.items():
        m = re.search(p, text)
        if m:
            f[k] = float(m.group(1))
    return f


def parse_ping(text):
    f = {}
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
    if m:
        sent, recv = int(m.group(1)), int(m.group(2))
        f["sent"], f["received"] = sent, recv
        f["loss_pct"] = round(100.0 * (sent - recv) / sent, 1) if sent else 100.0
    m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms", text)
    if m:
        f["rtt_min_ms"], f["rtt_avg_ms"], f["rtt_max_ms"], f["jitter_ms"] = map(float, m.groups())
    return f


def parse_dig(text):
    f = {"ok": False}
    m = re.search(r"status: (\w+)", text)
    if m:
        f["status"] = m.group(1)
        f["ok"] = m.group(1) == "NOERROR"
    m = re.search(r"Query time: (\d+) msec", text)
    if m:
        f["query_ms"] = int(m.group(1))
    f["answers"] = re.findall(r"\sIN\s+A\s+(\d+\.\d+\.\d+\.\d+)", text)
    return f


WPA_TS = re.compile(r"^(\d+\.\d+): (.*)$")


def classify_wpa(lines, ssid):
    """Turn timestamped wpa_supplicant output into stage times and an outcome."""
    t = {}
    info = {"eap_cert_subject": None, "eap_method": None, "reason": None, "status": "timeout"}

    def first(key, ts):
        t.setdefault(key, ts)

    for line in lines:
        m = WPA_TS.match(line)
        if not m:
            continue
        ts, msg = float(m.group(1)), m.group(2)
        if "Trying to associate with" in msg:
            first("assoc_start", ts)
        elif re.search(r"Associated with [0-9a-f:]{17}", msg):
            first("assoc_done", ts)
        elif "CTRL-EVENT-EAP-STARTED" in msg:
            first("eap_start", ts)
        elif "CTRL-EVENT-EAP-PROPOSED-METHOD" in msg:
            m2 = re.search(r"method=(\d+)", msg)
            info["eap_method"] = {"25": "PEAP", "21": "TTLS", "13": "TLS"}.get(
                m2.group(1), m2.group(1)) if m2 else None
        elif "CTRL-EVENT-EAP-PEER-CERT" in msg and "depth=0" in msg:
            m2 = re.search(r"subject='([^']*)'", msg)
            if m2:
                info["eap_cert_subject"] = m2.group(1)
        elif "CTRL-EVENT-EAP-SUCCESS" in msg:
            first("eap_done", ts)
        elif "Key negotiation completed" in msg:
            first("keys_done", ts)
        elif "CTRL-EVENT-CONNECTED" in msg:
            first("connected", ts)
            info["status"] = "connected"
        elif "CTRL-EVENT-EAP-TLS-CERT-ERROR" in msg:
            info["status"], info["reason"] = "fail", "tls_cert_error: " + msg.split("CERT-ERROR", 1)[1].strip()
        elif "CTRL-EVENT-EAP-FAILURE" in msg:
            info["status"], info["reason"] = "fail", info["reason"] or "eap_failure"
        elif "CTRL-EVENT-ASSOC-REJECT" in msg:
            m2 = re.search(r"status_code=(\d+)", msg)
            info["status"], info["reason"] = "fail", f"assoc_reject status={m2.group(1) if m2 else '?'}"
        elif "CTRL-EVENT-AUTH-REJECT" in msg:
            m2 = re.search(r"status_code=(\d+)", msg)
            info["status"], info["reason"] = "fail", f"auth_reject status={m2.group(1) if m2 else '?'}"
        elif "CTRL-EVENT-SSID-TEMP-DISABLED" in msg:
            m2 = re.search(r"reason=(\S+)", msg)
            info["status"] = "fail"
            info["reason"] = info["reason"] or f"temp_disabled {m2.group(1) if m2 else ''}".strip()
        elif "CTRL-EVENT-DISCONNECTED" in msg and "connected" not in t:
            m2 = re.search(r"reason=(\d+)", msg)
            info["deauth_reason"] = int(m2.group(1)) if m2 else None
            info["reason"] = info["reason"] or f"disconnected reason={info['deauth_reason']}"
        elif "CTRL-EVENT-NETWORK-NOT-FOUND" in msg:
            info["not_found"] = info.get("not_found", 0) + 1

    def ms(a, b):
        return round((t[b] - t[a]) * 1000, 1) if a in t and b in t else None

    timings = {
        "assoc_ms": ms("assoc_start", "assoc_done"),
        "eap_ms": ms("eap_start", "eap_done"),
        "handshake_ms": ms("eap_done", "keys_done"),
        "l2_total_ms": ms("assoc_start", "connected"),
    }
    # Which stage did it stop in?
    if info["status"] == "connected":
        stage = None
    elif "assoc_start" not in t:
        stage = "scan"
    elif "assoc_done" not in t:
        stage = "association"
    elif "eap_done" not in t:
        stage = "authentication"
    else:
        stage = "handshake"
    if (info.get("reason") or "").startswith(("assoc_reject", "auth_reject")):
        stage = "association"
    info["fail_stage"] = stage
    if info["status"] != "connected" and not info["reason"]:
        info["reason"] = "ap_not_found" if stage == "scan" else "timeout"
    return timings, info


# --------------------------------------------------------------------- radio work

class Radio:
    def __init__(self, cfg):
        self.cfg = cfg
        self.iface = cfg.get("TEST_IFACE", "wlan0")
        self.ssid = cfg.get("SSID", "eduroam")
        self.wpa = None
        self.lease = None
        RUN.mkdir(mode=0o700, parents=True, exist_ok=True)

    # ---- scanning
    def scan(self, attempts=4):
        sh(["ip", "link", "set", self.iface, "up"])
        for i in range(attempts):
            rc, out = sh(["iw", "dev", self.iface, "scan"], timeout=20)
            if rc == 0 and "BSS " in out:
                return parse_scan(out)
            time.sleep(2 + i)
        return []

    # ---- 802.1X connection
    def _wpa_conf(self, bssid, freq):
        c = self.cfg

        def q(v):
            if '"' in v or "\n" in v:
                raise ValueError("EAP values may not contain double quotes or newlines")
            return f'"{v}"'

        if c.get("EAP_SERVER_CERT_HASH"):
            ca = "hash://server/sha256/" + c["EAP_SERVER_CERT_HASH"].lower()
        else:
            ca = c.get("EAP_CA_CERT") or SYSTEM_CA
        lines = [
            f"country={c.get('COUNTRY', 'US')}",
            "ap_scan=1",
            "network={",
            f"  ssid={q(self.ssid)}",
            f"  bssid={bssid}",
            f"  freq_list={freq}",
            f"  scan_freq={freq}",
            "  key_mgmt=WPA-EAP WPA-EAP-SHA256",
            "  ieee80211w=1",
            f"  eap={c.get('EAP_METHOD', 'PEAP').upper()}",
            f"  identity={q(c['EAP_IDENTITY'])}",
            self._password_line(q),
            f"  ca_cert={q(ca)}",
            f"  phase2={q(c.get('EAP_PHASE2', 'auth=MSCHAPV2'))}",
        ]
        if c.get("EAP_ANON_IDENTITY"):
            lines.append(f"  anonymous_identity={q(c['EAP_ANON_IDENTITY'])}")
        if c.get("EAP_DOMAIN_MATCH"):
            lines.append(f"  domain_suffix_match={q(c['EAP_DOMAIN_MATCH'])}")
        lines.append("}")
        path = RUN / "wpa.conf"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def _password_line(self, q):
        """Never write the plaintext password to disk if it can be avoided.

        EAP_PASSWORD_HASH (an NT hash) or a password typed at the prompt is
        written as hash:... when the inner method is MSCHAPv2. The config file
        lives in /run (RAM), is root-only, and is deleted after each test.
        """
        c = self.cfg
        if c.get("EAP_PASSWORD_HASH"):
            h = c["EAP_PASSWORD_HASH"].lower().removeprefix("hash:")
            if not re.fullmatch(r"[0-9a-f]{32}", h):
                raise ValueError("EAP_PASSWORD_HASH must be 32 hex characters")
            if not uses_mschapv2(c):
                raise ValueError("EAP_PASSWORD_HASH only works with MSCHAPv2 inner auth")
            return f"  password=hash:{h}"
        pw = c.get("_PW_MEM") or c.get("EAP_PASSWORD")
        if uses_mschapv2(c):
            return f"  password=hash:{nt_hash(pw)}"
        return f"  password={q(pw)}"

    def connect(self, bssid, freq, timeout=30):
        self.teardown()
        conf = self._wpa_conf(bssid, freq)
        cmd = ["stdbuf", "-oL", "wpa_supplicant", "-t"] + (["-d"] if os.environ.get("PROBE_WPA_DEBUG") else []) + ["-i", self.iface,
               "-D", "nl80211", "-c", str(conf)]
        self.wpa = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
        lines, q = [], queue.Queue()
        threading.Thread(target=lambda: [q.put(l.rstrip()) for l in self.wpa.stdout],
                         daemon=True).start()
        deadline = time.monotonic() + timeout
        not_found = 0
        while time.monotonic() < deadline:
            try:
                line = q.get(timeout=0.5)
            except queue.Empty:
                if self.wpa.poll() is not None:
                    break
                continue
            lines.append(line)
            if "CTRL-EVENT-CONNECTED" in line:
                break
            if any(k in line for k in ("CTRL-EVENT-EAP-FAILURE", "CTRL-EVENT-ASSOC-REJECT",
                                        "CTRL-EVENT-AUTH-REJECT", "CTRL-EVENT-SSID-TEMP-DISABLED",
                                        "CTRL-EVENT-EAP-TLS-CERT-ERROR")):
                time.sleep(0.3)
                while not q.empty():
                    lines.append(q.get())
                break
            if "CTRL-EVENT-NETWORK-NOT-FOUND" in line:
                not_found += 1
                if not_found >= 3:
                    break
        timings, info = classify_wpa(lines, self.ssid)
        return timings, info, lines

    # ---- DHCP (udhcpc) and routing
    @staticmethod
    def _udhcpc_cmd():
        if shutil.which("udhcpc"):
            return ["udhcpc"]
        if shutil.which("busybox"):
            rc, out = sh(["busybox", "--list"])
            if "udhcpc" in out.split():
                return ["busybox", "udhcpc"]
        return None

    def dhcp(self, timeout=20):
        base = Radio._udhcpc_cmd()
        if not base:
            return None, {"reason": "no_dhcp_client"}
        lease_file = RUN / "lease.env"
        script = Path("/opt/eduroam-probe/udhcpc.sh")  # /run is mounted noexec
        script.write_text("#!/bin/sh\n"
                          f'[ "$1" = bound ] || [ "$1" = renew ] || exit 0\n'
                          f"env | grep -E '^(ip|mask|subnet|router|dns|domain|lease|serverid|broadcast|mtu)=' > {lease_file}\n")
        script.chmod(0o700)
        lease_file.unlink(missing_ok=True)
        t0 = time.monotonic()
        rc, out = sh(base + ["-i", self.iface, "-f", "-q", "-n", "-t", "5", "-T", "2",
                             "-A", "1", "-s", str(script)], timeout=timeout)
        dhcp_ms = round((time.monotonic() - t0) * 1000, 1)
        if not lease_file.exists():
            tail = out.strip().splitlines()[-1] if out.strip() else "no output"
            return dhcp_ms, {"reason": f"no_lease (udhcpc rc={rc}: {tail})"}
        lease = {}
        for line in lease_file.read_text().splitlines():
            k, _, v = line.partition("=")
            lease[k] = v.strip()
        if "ip" not in lease:
            return dhcp_ms, {"reason": "no_lease"}
        self._apply_lease(lease)
        self.lease = lease
        return dhcp_ms, lease

    def _apply_lease(self, lease):
        ip = lease["ip"]
        prefix = lease.get("mask") or str(ipaddress.IPv4Network(f"0.0.0.0/{lease.get('subnet', '255.255.255.0')}").prefixlen)
        net = ipaddress.IPv4Interface(f"{ip}/{prefix}").network
        gw = (lease.get("router") or "").split()[0] if lease.get("router") else None
        sh(["ip", "addr", "add", f"{ip}/{prefix}", "dev", self.iface])
        sh(["ip", "route", "add", str(net), "dev", self.iface, "table", POLICY_TABLE])
        sh(["ip", "rule", "add", "from", ip, "table", POLICY_TABLE, "priority", POLICY_PRIO])
        if gw:
            sh(["ip", "route", "add", "default", "via", gw, "dev", self.iface, "table", POLICY_TABLE])
            sh(["ip", "route", "add", "default", "via", gw, "dev", self.iface, "metric", MAIN_METRIC])

    def teardown(self):
        if self.wpa and self.wpa.poll() is None:
            self.wpa.send_signal(signal.SIGTERM)
            try:
                self.wpa.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.wpa.kill()
        self.wpa = None
        # Also clear any supplicant left behind by a crashed run.
        sh(["pkill", "-f", f"wpa_supplicant .*-i {self.iface} .*{RUN}/wpa.conf"])
        (RUN / "wpa.conf").unlink(missing_ok=True)
        while sh(["ip", "rule", "del", "priority", POLICY_PRIO])[0] == 0:
            pass
        sh(["ip", "route", "flush", "table", POLICY_TABLE])
        sh(["ip", "route", "del", "default", "dev", self.iface, "metric", MAIN_METRIC])
        sh(["ip", "addr", "flush", "dev", self.iface])
        self.lease = None


# --------------------------------------------------------------------- measurements

def measure_network(radio, cfg):
    lease = radio.lease or {}
    ip = lease.get("ip")
    gw = (lease.get("router") or "").split()[0] if lease.get("router") else None
    dns_servers = (lease.get("dns") or "").split()
    m = {"dhcp_server": lease.get("serverid"), "lease_s": int(lease["lease"]) if lease.get("lease", "").isdigit() else None,
         "client_ip": ip, "gateway": gw, "dns_servers": " ".join(dns_servers)}

    if gw:
        g = parse_ping(sh(["ping", "-I", radio.iface, "-c", "10", "-i", "0.2", "-W", "1", "-q", gw], timeout=20)[1])
        m.update({f"gw_{k}": v for k, v in g.items() if k not in ("sent", "received")})
    target = cfg.get("PING_TARGET", "8.8.8.8")
    p = parse_ping(sh(["ping", "-I", radio.iface, "-c", "10", "-i", "0.2", "-W", "1", "-q", target], timeout=20)[1])
    m.update({f"inet_{k}": v for k, v in p.items() if k not in ("sent", "received")})

    host = cfg.get("DNS_TEST_HOST", "www.google.com")
    resolved = None
    for i, server in enumerate(dns_servers[:2]):
        d = parse_dig(sh(["dig", f"@{server}", "-b", ip, "+time=2", "+tries=1", host], timeout=10)[1])
        m[f"dns{i+1}_ok"] = d["ok"]
        if "query_ms" in d:
            m[f"dns{i+1}_ms"] = d["query_ms"]
        if d["answers"] and not resolved:
            resolved = d["answers"][0]
    m["dns_ok"] = resolved is not None

    url = cfg.get("HTTP_TEST_URL", "https://www.google.com/generate_204")
    if resolved:
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        rc, out = sh(["curl", "--interface", radio.iface, "--resolve", f"{u.hostname}:{port}:{resolved}",
                      "-o", "/dev/null", "-s", "--max-time", "10",
                      "-w", "%{http_code} %{time_connect} %{time_appconnect} %{time_total}", url], timeout=15)
        parts = out.split()
        if len(parts) == 4:
            m["http_code"] = int(parts[0])
            m["http_ok"] = 200 <= int(parts[0]) < 400
            m["http_connect_ms"] = round(float(parts[1]) * 1000, 1)
            m["http_tls_ms"] = round(float(parts[2]) * 1000, 1)
            m["http_total_ms"] = round(float(parts[3]) * 1000, 1)
        else:
            m["http_ok"] = False

    time.sleep(1)  # give router advertisements a moment
    rc, out = sh(["ip", "-6", "-o", "addr", "show", "dev", radio.iface, "scope", "global"])
    m["ipv6_global"] = bool(out.strip())

    server = cfg.get("IPERF_SERVER")
    if server and ip:
        for direction, extra in (("up", []), ("down", ["-R"])):
            rc, out = sh(["iperf3", "-c", server, "-B", ip, "-t", "5", "-J"] + extra, timeout=25)
            try:
                bps = json.loads(out)["end"]["sum_received"]["bits_per_second"]
                m[f"iperf_{direction}_mbps"] = round(bps / 1e6, 2)
            except (ValueError, KeyError, TypeError):
                pass
    return m


# --------------------------------------------------------------------- InfluxDB

def _esc_tag(v):
    return str(v).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _fmt_field(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def line(measurement, tags, fields, ts_ms):
    tags = ",".join(f"{_esc_tag(k)}={_esc_tag(v)}" for k, v in sorted(tags.items()) if v not in (None, ""))
    fields = ",".join(f"{_esc_tag(k)}={_fmt_field(v)}" for k, v in fields.items() if v is not None)
    if not fields:
        return None
    return f"{measurement}{',' + tags if tags else ''} {fields} {ts_ms}"


class Influx:
    def __init__(self, cfg):
        self.url = cfg.get("INFLUX_URL", "http://localhost:8086")
        self.org = cfg.get("INFLUX_ORG")
        self.bucket = cfg.get("INFLUX_BUCKET")
        self.token = cfg.get("INFLUX_TOKEN")
        self.pending = DATA / "pending"
        self.pending.mkdir(parents=True, exist_ok=True)

    def _post(self, body):
        url = f"{self.url}/api/v2/write?org={self.org}&bucket={self.bucket}&precision=ms"
        req = urllib.request.Request(url, data=body.encode(), method="POST",
                                     headers={"Authorization": f"Token {self.token}",
                                              "Content-Type": "text/plain; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status in (200, 204)

    def write(self, lines):
        body = "\n".join(l for l in lines if l)
        if not body:
            return True
        if not (self.org and self.bucket and self.token):
            log("InfluxDB settings missing; saving data locally only")
            self._queue(body)
            return False
        try:
            ok = self._post(body)
        except (urllib.error.URLError, OSError) as e:
            log(f"InfluxDB write failed ({e}); queued for retry")
            ok = False
        if not ok:
            self._queue(body)
        return ok

    def _queue(self, body):
        (self.pending / f"{time.time_ns()}.lp").write_text(body)

    def flush_pending(self):
        for f in sorted(self.pending.glob("*.lp")):
            try:
                if self._post(f.read_text()):
                    f.unlink()
            except (urllib.error.URLError, OSError):
                return


# --------------------------------------------------------------------- commands

def cmd_scan(cfg, args):
    radio = Radio(cfg)
    scan = name_scan(radio.scan(), load_ap_names())
    apply_rogue_flags(scan, radio.ssid, cfg)
    bss = [b for b in scan if b["ssid"] == radio.ssid]
    bss.sort(key=lambda b: b["signal"] or -999, reverse=True)
    if not bss:
        print(f"No {radio.ssid} BSSIDs found.")
        return 1
    print(f"{'signal':>8}  {'band':>4}  {'ch':>3}  {'util%':>5}  {'sta':>3}  {'security':<15}  "
          f"{'pmf':<8}  {'bssid':<17}  flags / name")
    for b in bss:
        note = b["security_flags"] or ("" if b["ap_name"] == b["ap_group"] else b["ap_name"])
        print(f"{b['signal']:>8.1f}  {b['band']:>4}  {b['channel']:>3}  "
              f"{'' if b['channel_util_pct'] is None else b['channel_util_pct']:>5}  "
              f"{'' if b['station_count'] is None else b['station_count']:>3}  {b['security']:<15}  "
              f"{b['pmf']:<8}  {b['bssid']:<17}  {note}")
    return 0


def password_mode(cfg):
    if cfg.get("EAP_PASSWORD_HASH"):
        return "hash"
    if cfg.get("_PW_MEM"):
        return "prompt"
    if cfg.get("EAP_PASSWORD"):
        return "plaintext"
    return None


def creds_ready(cfg):
    missing = [] if cfg.get("EAP_IDENTITY") else ["EAP_IDENTITY"]
    if not password_mode(cfg):
        missing.append("password (run from a terminal to be prompted, or set EAP_PASSWORD_HASH)")
    ca = cfg.get("EAP_CA_CERT")
    if ca and not Path(ca).exists():
        missing.append(f"EAP_CA_CERT file {ca}")
    return missing


def run_cycle(cfg, args, influx):
    probe_id = cfg.get("PROBE_ID", socket.gethostname())
    location = args.location or cfg.get("LOCATION", "unspecified")
    radio = Radio(cfg)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    now_ms = int(time.time() * 1000)
    result = {"probe": probe_id, "location": location, "time": stamp, "version": VERSION, "tests": []}
    lines = []

    log("Scanning")
    scan = name_scan(radio.scan(), load_ap_names())
    apply_rogue_flags(scan, radio.ssid, cfg)
    result["scan"] = scan
    edu = [b for b in scan if b["ssid"] == radio.ssid]
    for b in scan:
        flags = b["security_flags"].split(",") if b["security_flags"] else []
        lines.append(line("wifi_scan",
                          {"probe": probe_id, "location": location, "ssid": b["ssid"] or "<hidden>",
                           "bssid": b["bssid"], "ap_group": b["ap_group"], "ap_name": b["ap_name"],
                           "building": b["building"], "band": b["band"], "channel": b["channel"],
                           "security": b["security"], "pmf": b["pmf"]},
                          {"signal_dbm": b["signal"], "freq_mhz": b["freq"],
                           "station_count": b["station_count"], "channel_util_pct": b["channel_util_pct"],
                           "wifi_gen": b["wifi_gen"], "wps": b["wps"],
                           "security_flags": b["security_flags"] or None,
                           "flag_count": len(flags),
                           "insecure": 1 if flags else 0,
                           "auth_suites": b["auth_suites"]}, now_ms))
    # Co-channel neighbors: how many other BSSIDs (any SSID) share each channel.
    by_chan = {}
    for b in scan:
        by_chan[(b["band"], b["channel"])] = by_chan.get((b["band"], b["channel"]), 0) + 1
    log(f"Found {len(scan)} BSSIDs, {len(edu)} on {radio.ssid}")

    if not edu:
        lines.append(line("wifi_test", {"probe": probe_id, "location": location, "result": "fail",
                                        "fail_stage": "scan"},
                          {"success": False, "fail_reason": "no_eduroam_visible"}, now_ms))
        _finish(result, lines, influx, stamp)
        return result

    nearest = max(edu, key=lambda b: b["signal"] or -999)
    if args.bssid:
        targets = [b for b in edu if b["bssid"] == args.bssid.lower()]
        log(f"Testing {args.bssid} only")
    elif args.nearest_only:
        targets = [nearest]
        log(f"Testing nearest radio {nearest['bssid']} ({nearest['signal']} dBm)")
    elif args.nearest_ap:
        targets = sorted([b for b in edu if b["ap_group"] == nearest["ap_group"]],
                         key=lambda b: b["freq"])
        log(f"Testing nearest AP {nearest['ap_group']}: {len(targets)} radio(s)")
    else:
        targets = sorted([b for b in edu if (b["signal"] or -999) >= args.min_signal],
                         key=lambda b: -(b["signal"] or -999))
        skipped = len(edu) - len(targets)
        aps = len({b["ap_group"] for b in targets})
        log(f"Testing all {len(targets)} eduroam radio(s) on {aps} AP(s) at or above {args.min_signal} dBm"
            + (f"; skipping {skipped} weaker" if skipped else ""))

    if args.scan_only:
        log("Scan-only mode; skipping connection tests")
        _finish(result, lines, influx, stamp)
        return result

    missing = creds_ready(cfg)
    if missing:
        log(f"Connection tests skipped, missing settings: {', '.join(missing)}")
        _finish(result, lines, influx, stamp)
        return result

    try:
        for b in targets:
            t_ms = int(time.time() * 1000)
            tags = {"probe": probe_id, "location": location, "bssid": b["bssid"], "ap_group": b["ap_group"],
                    "ap_name": b["ap_name"], "building": b["building"], "band": b["band"], "channel": b["channel"]}
            fields = {"scan_signal_dbm": b["signal"], "channel_util_pct": b["channel_util_pct"],
                      "station_count": b["station_count"],
                      "cochannel_bssids": by_chan.get((b["band"], b["channel"]), 1) - 1}
            label = "" if b["ap_name"] == b["ap_group"] else f" [{b['ap_name']}]"
            log(f"Connecting to {b['bssid']}{label} ({b['band']} GHz ch {b['channel']}, {b['signal']} dBm)")
            timings, info, wpa_lines = radio.connect(b["bssid"], b["freq"])
            (DATA / "logs").mkdir(parents=True, exist_ok=True)
            (DATA / "logs" / f"{stamp}-{b['bssid'].replace(':', '')}.log").write_text("\n".join(wpa_lines) + "\n")
            fields.update(timings)
            fields["eap_method"] = info.get("eap_method")
            fields["eap_cert_subject"] = info.get("eap_cert_subject")
            fields["deauth_reason"] = info.get("deauth_reason")
            test = {"bssid": b["bssid"], "band": b["band"], **timings, **info}

            if info["status"] != "connected":
                log(f"  FAILED at {info['fail_stage']}: {info['reason']}")
                tags.update(result="fail", fail_stage=info["fail_stage"])
                fields.update(success=False, fail_reason=info["reason"])
            else:
                log(f"  connected: assoc {timings['assoc_ms']} ms, EAP {timings['eap_ms']} ms, "
                    f"handshake {timings['handshake_ms']} ms")
                fields.update(parse_link(sh(["iw", "dev", radio.iface, "link"])[1]))
                dump = parse_link(sh(["iw", "dev", radio.iface, "station", "dump"])[1])
                fields.update({k: v for k, v in dump.items() if k not in fields})
                dhcp_ms, lease = radio.dhcp()
                fields["dhcp_ms"] = dhcp_ms
                if "ip" not in lease:
                    log(f"  FAILED at dhcp: {lease.get('reason')}")
                    tags.update(result="fail", fail_stage="dhcp")
                    fields.update(success=False, fail_reason=lease.get("reason"))
                else:
                    log(f"  DHCP {dhcp_ms} ms -> {lease['ip']}; measuring network")
                    net = measure_network(radio, cfg)
                    test.update(net)
                    fields.update({k: v for k, v in net.items() if not isinstance(v, str) or k in
                                   ("dhcp_server", "client_ip", "gateway", "dns_servers")})
                    if not net.get("dns_ok"):
                        tags.update(result="fail", fail_stage="dns")
                        fields.update(success=False, fail_reason="dns_failed")
                    elif net.get("http_ok") is False:
                        tags.update(result="fail", fail_stage="http")
                        fields.update(success=False, fail_reason=f"http_{net.get('http_code', 'error')}")
                    else:
                        tags.update(result="ok")
                        fields["success"] = True
                    log(f"  {'OK' if fields.get('success') else 'FAILED at ' + tags['fail_stage']}: "
                        f"gateway {net.get('gw_rtt_avg_ms', '-')} ms ({net.get('gw_loss_pct', '-')}% loss), "
                        f"DNS {net.get('dns1_ms', '-')} ms, HTTPS {net.get('http_total_ms', '-')} ms")
                    # Link stats again after traffic, to catch retries and failures under load.
                    after = parse_link(sh(["iw", "dev", radio.iface, "station", "dump"])[1])
                    for k in ("tx_retries", "tx_failed", "beacon_loss"):
                        if k in after:
                            fields[k] = after[k]
            test["result"] = tags["result"]
            result["tests"].append(test)
            lines.append(line("wifi_test", tags, fields, t_ms))
            radio.teardown()
            if info.get("reason") == "eap_failure":
                log("  Credentials rejected; stopping to avoid locking the account")
                result["auth_rejected"] = True
                break
    finally:
        radio.teardown()
    n_ok = sum(1 for t in result["tests"] if t.get("result") == "ok")
    log(f"Summary: {len(result['tests'])} radio(s) tested, {n_ok} ok, {len(result['tests']) - n_ok} failed")
    _finish(result, lines, influx, stamp)
    return result


def _finish(result, lines, influx, stamp):
    (DATA / "results").mkdir(parents=True, exist_ok=True)
    (DATA / "results" / f"{stamp}.json").write_text(json.dumps(result, indent=2, default=str))
    ok = influx.write(lines)
    log(f"Saved results/{stamp}.json; InfluxDB write {'ok' if ok else 'queued'} ({len([l for l in lines if l])} points)")


def cmd_run(cfg, args):
    log(f"eduroam-probe v{VERSION}")
    if not args.scan_only:
        tty = sys.stdin.isatty()
        if not cfg.get("EAP_IDENTITY"):
            if tty:
                ident = input("eduroam username (e.g. you@your-institution.edu), blank to skip connection tests: ").strip()
                if ident:
                    cfg["EAP_IDENTITY"] = ident
            else:
                log("EAP_IDENTITY is not set in probe.env; connection tests will be skipped")
        if cfg.get("EAP_IDENTITY") and not password_mode(cfg):
            if tty:
                pw = getpass.getpass(f"eduroam password for {cfg['EAP_IDENTITY']}: ")
                if pw:
                    cfg["_PW_MEM"] = pw
            else:
                log("No password available and no terminal to prompt on; connection tests will be skipped")
        if cfg.get("EAP_IDENTITY"):
            log(f"Connection tests as {cfg['EAP_IDENTITY']} (password: {password_mode(cfg) or 'none'})")
    if password_mode(cfg) == "plaintext":
        log("Warning: EAP_PASSWORD is stored in plaintext; consider EAP_PASSWORD_HASH or the prompt")
    influx = Influx(cfg)
    influx.flush_pending()
    for i in range(args.repeat):
        if i:
            log(f"Waiting {args.interval}s before next cycle")
            time.sleep(args.interval)
        log(f"=== Cycle {i + 1}/{args.repeat} ===")
        if run_cycle(cfg, args, influx).get("auth_rejected"):
            log("Stopping: the server rejected the credentials. Check the username/password before retrying.")
            break
    return 0


def cmd_hash_password(cfg, args):
    pw = getpass.getpass("eduroam password: ")
    if not pw or pw != getpass.getpass("again: "):
        print("Passwords empty or did not match.")
        return 1
    print("\nAdd this line to /etc/eduroam-probe/probe.env (and remove any EAP_PASSWORD line):\n")
    print(f"EAP_PASSWORD_HASH={nt_hash(pw)}\n")
    print("Works with MSCHAPv2 inner auth only (PEAP/MSCHAPv2 or TTLS/MSCHAPv2).")
    print("It hides your real password, but anyone holding it can still log in to eduroam as you.")
    return 0


def cmd_doctor(cfg, args):
    ok = True

    def chk(cond, good, bad):
        nonlocal ok
        print(("  PASS " if cond else "  FAIL ") + (good if cond else bad))
        ok &= bool(cond)

    print("Tools")
    for tool in ("iw", "wpa_supplicant", "stdbuf", "ip", "ping", "dig", "curl"):
        chk(shutil.which(tool), tool, f"{tool} missing")
    chk(Radio._udhcpc_cmd(), "DHCP client (udhcpc)", "no udhcpc - install the 'udhcpc' package (needs internet once)")
    print("  info iperf3 " + ("present" if shutil.which("iperf3") else "missing (throughput tests skipped)"))

    print("Settings")
    iface = cfg.get("TEST_IFACE", "wlan0")
    chk(Path(f"/sys/class/net/{iface}").exists(), f"test interface {iface}", f"{iface} not found")
    missing = creds_ready(cfg)
    mode = password_mode(cfg)
    if not mode and cfg.get("EAP_IDENTITY"):
        print("  info no stored password; `run` will prompt for it (nothing written to disk)")
        missing = [m for m in missing if not m.startswith("password")]
    elif mode:
        print(f"  info password source: {mode}" + ("  <- stored in plaintext, consider EAP_PASSWORD_HASH"
                                                   if mode == "plaintext" else ""))
    if mode == "hash" and not uses_mschapv2(cfg):
        chk(False, "", "EAP_PASSWORD_HASH needs MSCHAPv2 inner auth (EAP_PHASE2=auth=MSCHAPV2)")
    chk(not missing, "eduroam credentials set", "missing: " + ", ".join(missing))
    chk(cfg.get("EAP_DOMAIN_MATCH"), f"RADIUS server check: {cfg.get('EAP_DOMAIN_MATCH')}",
        "EAP_DOMAIN_MATCH not set - the RADIUS server certificate name is not being checked")
    print(f"  info EAP method {cfg.get('EAP_METHOD', 'PEAP')} / {cfg.get('EAP_PHASE2', 'auth=MSCHAPV2')}, "
          f"CA {cfg.get('EAP_CA_CERT') or SYSTEM_CA + ' (system bundle)'}")

    print("InfluxDB")
    influx = Influx(cfg)
    try:
        with urllib.request.urlopen(f"{influx.url}/health", timeout=5) as r:
            chk(r.status == 200, f"reachable at {influx.url}", "health check failed")
    except (urllib.error.URLError, OSError) as e:
        chk(False, "", f"not reachable at {influx.url}: {e}")
    chk(influx.token and influx.org and influx.bucket, "token/org/bucket set", "influx.env incomplete")
    n = len(list(influx.pending.glob("*.lp")))
    print(f"  info {n} queued write(s) waiting")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="eduroam Wi-Fi probe agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check tools and settings")
    sub.add_parser("scan", help="list visible eduroam BSSIDs")
    sub.add_parser("hash-password", help="turn your password into an NT hash for probe.env")
    r = sub.add_parser("run", help="run test cycle(s) and write to InfluxDB")
    r.add_argument("--location", help="building/room tag, e.g. 'Library 2F'")
    r.add_argument("--repeat", type=int, default=1, help="number of cycles (default 1)")
    r.add_argument("--interval", type=int, default=60, help="seconds between cycles")
    r.add_argument("--min-signal", type=int, default=-80,
                   help="skip radios weaker than this many dBm (default -80)")
    r.add_argument("--nearest-ap", action="store_true", help="test only the nearest AP's radios")
    r.add_argument("--nearest-only", action="store_true", help="test only the strongest BSSID")
    r.add_argument("--bssid", help="test this BSSID only")
    r.add_argument("--scan-only", action="store_true", help="record RF scan data, skip connection tests")
    args = ap.parse_args()

    if args.cmd == "hash-password":
        return cmd_hash_password({}, args)
    if os.geteuid() != 0:
        sys.exit("run as root (sudo)")
    cfg = load_env(ETC / "probe.env", ETC / "influx.env")
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        return {"doctor": cmd_doctor, "scan": cmd_scan, "run": cmd_run}[args.cmd](cfg, args)
    except KeyboardInterrupt:
        Radio(cfg).teardown()
        return 130


if __name__ == "__main__":
    sys.exit(main())
