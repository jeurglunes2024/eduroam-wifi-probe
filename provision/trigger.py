#!/usr/bin/env python3
"""Tiny HTTP endpoint to trigger an eduroam probe run from the dashboard.

A click in Grafana opens /run?token=... here; this launches the probe in the
background (so the browser returns at once) and the dashboard shows the new
data on its next refresh. Read-only otherwise: the only thing it can do is
start a probe run, nothing takes arbitrary input.

Runs as root via systemd. Token is read from /etc/eduroam-probe/trigger.token.
"""
import html
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("TRIGGER_PORT", "8088"))
TOKEN = Path("/etc/eduroam-probe/trigger.token").read_text().strip()
WRAPPER = "/usr/local/sbin/eduroam-probe-run"
ALLOWED = {"scan-only", "nearest-ap", "nearest-only", "all"}
_last = {"t": 0.0}


def launch(mode):
    args = [WRAPPER]
    if mode == "scan-only":
        args.append("--scan-only")
    elif mode == "nearest-ap":
        args.append("--nearest-ap")
    elif mode == "nearest-only":
        args.append("--nearest-only")
    # "all" -> no extra flag (test every radio in range)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)


PAGE = """<!doctype html><meta charset=utf-8>
<title>eduroam probe</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;
color:#e8eef5;background:#10151c}}a{{color:#6ea8ff}}
.card{{background:#1a2230;border:1px solid #2a3547;border-radius:10px;padding:1.2rem 1.4rem}}
h1{{font-size:1.1rem;margin:.2rem 0 1rem}}.ok{{color:#6ee787}}.err{{color:#ff8e8e}}</style>
<div class=card><h1>eduroam probe</h1>{body}</div>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = PAGE.format(body=body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path not in ("/run", "/"):
            return self._send(404, "<p class=err>Not found.</p>")
        if u.path == "/":
            return self._send(200, "<p>Probe trigger is running. Use the dashboard button.</p>")
        if q.get("token", [""])[0] != TOKEN:
            return self._send(403, "<p class=err>Invalid or missing token.</p>")
        mode = q.get("mode", ["nearest-ap"])[0]
        if mode not in ALLOWED:
            return self._send(400, f"<p class=err>Unknown mode: {html.escape(mode)}</p>")
        # Light debounce so a double-click doesn't stack runs (the lock also guards this).
        if time.time() - _last["t"] < 3:
            return self._send(429, "<p>Just triggered a moment ago; try again shortly.</p>")
        _last["t"] = time.time()
        launch(mode)
        back = "the dashboard tab"
        self._send(200, f"<p class=ok>Started a '{html.escape(mode)}' run.</p>"
                        f"<p>It takes up to a minute. Return to {back}; "
                        f"new data appears on the next refresh.</p>")

    def log_message(self, *a):
        pass  # keep the journal quiet


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
