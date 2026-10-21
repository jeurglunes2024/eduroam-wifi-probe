#!/usr/bin/env python3
"""Run every Flux query in the dashboard JSON files against the local InfluxDB.

Reports which panels' queries error out, and how many rows each returns.
Usage: sudo python3 check_dashboards.py overview.json access-point.json rf-survey.json
"""
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

env = {}
for line in Path("/etc/eduroam-probe/influx.env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

SUBS = [
    ("v.timeRangeStart", "-7d"), ("v.timeRangeStop", "now()"), ("v.windowPeriod", "5m"),
    (re.compile(r"\$\{(\w+):regex\}"), ".*"), (re.compile(r"\$\{(\w+)\}"), ""),
]


def prepare(q):
    for pat, rep in SUBS:
        q = pat.sub(rep, q) if hasattr(pat, "sub") else q.replace(pat, rep)
    return q


def run(q):
    req = urllib.request.Request(
        f"{env['INFLUX_URL']}/api/v2/query?org={env['INFLUX_ORG']}", data=q.encode(), method="POST",
        headers={"Authorization": f"Token {env['INFLUX_TOKEN']}", "Content-Type": "application/vnd.flux",
                 "Accept": "application/csv"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
        rows = [l for l in body.splitlines() if l and not l.startswith(("#", ",result")) and l.strip(",")]
        return True, f"{max(len(rows) - 1, 0)} rows" if rows else "0 rows"
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("message", "")
        except ValueError:
            msg = str(e)
        return False, msg


bad = 0
for f in sys.argv[1:] or ["overview.json", "access-point.json", "rf-survey.json"]:
    d = json.load(open(f))
    print(f"\n{d['title']}")
    items = [(f"variable ${v['name']}", v["query"]) for v in d["templating"]["list"]]
    items += [(p["title"], t["query"]) for p in d["panels"] if p["type"] != "row" for t in p.get("targets", [])]
    for title, q in items:
        ok, info = run(prepare(q))
        bad += not ok
        print(f"  {'OK  ' if ok else 'FAIL'} {title}: {info}")
print(f"\n{bad} failing quer{'y' if bad == 1 else 'ies'}")
sys.exit(1 if bad else 0)
