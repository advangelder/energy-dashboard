#!/usr/bin/env python3
"""
Enphase CORS Proxy for Energy Dashboard
Runs on the Android tablet via Termux.

Fetches data from the local Enphase IQ Gateway (which blocks browser CORS)
and re-serves it on localhost:8099 with proper CORS headers.

Usage:
    python enphase_proxy.py

Then set the proxy URL in the dashboard Settings to: http://localhost:8099
"""

import http.server
import json
import os
import ssl
import urllib.request
import urllib.error

# ── Configuration ────────────────────────────────────────────────────────────
PORT        = 8099
ENVOY_HOST  = os.environ.get("ENVOY_HOST", "envoy.local")
ENVOY_TOKEN = os.environ.get("ENVOY_TOKEN", "")   # paste your JWT token here
                                                   # or set via environment variable
# ─────────────────────────────────────────────────────────────────────────────


def fetch_enphase():
    """Fetch production data from Enphase gateway, ignoring self-signed cert."""
    url = f"https://{ENVOY_HOST}/api/v1/production"
    req = urllib.request.Request(url)
    if ENVOY_TOKEN:
        req.add_header("Authorization", f"Bearer {ENVOY_TOKEN}")

    # Enphase uses a self-signed certificate — disable verification
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
        return resp.read(), resp.status


class CORSProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Suppress default request logging (keeps Termux output clean)
        pass

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-cache")

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        # Health check endpoint
        if self.path == "/health":
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "envoy": ENVOY_HOST}).encode())
            return

        # Proxy all other requests to Enphase
        try:
            body, status = fetch_enphase()
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(f"[OK] Enphase → {len(body)} bytes")

        except urllib.error.URLError as e:
            msg = json.dumps({"error": str(e.reason)}).encode()
            self.send_response(502)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(msg)
            print(f"[ERR] {e}")

        except Exception as e:
            msg = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(msg)
            print(f"[ERR] {e}")


if __name__ == "__main__":
    print(f"Enphase CORS Proxy starting on http://localhost:{PORT}")
    print(f"  Envoy host : {ENVOY_HOST}")
    print(f"  Token set  : {'yes' if ENVOY_TOKEN else 'NO - set ENVOY_TOKEN or edit this file'}")
    print(f"  Health URL : http://localhost:{PORT}/health")
    print("Press Ctrl+C to stop.\n")

    server = http.server.HTTPServer(("127.0.0.1", PORT), CORSProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nProxy stopped.")
