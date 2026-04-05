#!/usr/bin/env python3
"""
Energy Dashboard — Local CORS Proxy for Termux
================================================
Runs on the Android tablet and proxies two local devices that block browser
CORS requests:

  GET /        → Enphase IQ Gateway  (https://envoy.local/api/v1/production)
  GET /enphase → same as above
  GET /p1      → HomeWizard P1 Meter (http://<P1_HOST>/api/v1/data)
  GET /health  → JSON health check

Usage:
    python enphase_proxy.py

Or override hosts via environment:
    ENVOY_HOST=192.168.2.x P1_HOST=192.168.2.2 ENVOY_TOKEN=eyJ... python enphase_proxy.py

In the dashboard Settings set the Proxy URL to:
    http://localhost:8099
"""

import http.server
import json
import os
import ssl
import urllib.request
import urllib.error

# ── Configuration ─────────────────────────────────────────────────────────────
PORT        = 8099
ENVOY_HOST  = os.environ.get("ENVOY_HOST",  "envoy.local")
P1_HOST     = os.environ.get("P1_HOST",     "192.168.2.2")
ENVOY_TOKEN = os.environ.get("ENVOY_TOKEN", "")  # JWT token from entrez.enphaseenergy.com
# ──────────────────────────────────────────────────────────────────────────────


def fetch_url(url, token=None, verify_ssl=True, timeout=5):
    """Fetch a URL, optionally with a Bearer token and/or SSL verification disabled."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    if not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx = None

    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return resp.read(), resp.status


class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default output

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-cache")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        # ── Health check ──────────────────────────────────────────────────────
        if path == "/health":
            body = json.dumps({
                "status":  "ok",
                "envoy":   ENVOY_HOST,
                "p1":      P1_HOST,
                "token":   bool(ENVOY_TOKEN)
            }).encode()
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── P1 meter ──────────────────────────────────────────────────────────
        if path == "/p1":
            self._proxy(
                url=f"http://{P1_HOST}/api/v1/data",
                label="P1",
                verify_ssl=True
            )
            return

        # ── Enphase gateway (default / /enphase) ─────────────────────────────
        self._proxy(
            url=f"https://{ENVOY_HOST}/api/v1/production",
            label="Enphase",
            token=ENVOY_TOKEN,
            verify_ssl=False   # Enphase uses a self-signed certificate
        )

    def _proxy(self, url, label, token=None, verify_ssl=True):
        try:
            body, status = fetch_url(url, token=token, verify_ssl=verify_ssl)
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(f"[OK]  {label} → {len(body)} bytes")

        except urllib.error.URLError as e:
            msg = json.dumps({"error": str(e.reason)}).encode()
            self.send_response(502)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(msg)
            print(f"[ERR] {label} → {e}")

        except Exception as e:
            msg = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(msg)
            print(f"[ERR] {label} → {e}")


if __name__ == "__main__":
    print(f"Energy Dashboard Proxy  —  http://localhost:{PORT}")
    print(f"  Enphase : https://{ENVOY_HOST}/api/v1/production")
    print(f"  P1      : http://{P1_HOST}/api/v1/data")
    print(f"  Token   : {'set ✓' if ENVOY_TOKEN else 'NOT SET — Enphase will fail without it'}")
    print(f"  Health  : http://localhost:{PORT}/health")
    print("Press Ctrl+C to stop.\n")

    server = http.server.HTTPServer(("127.0.0.1", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nProxy stopped.")
