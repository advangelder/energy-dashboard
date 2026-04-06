#!/usr/bin/env python3
"""
Energy Dashboard — Local CORS Proxy for Termux
================================================
Handles:
  - Auto token refresh (tokens expire after 12h for self-installers)
  - Inverter filtering (only sums YOUR inverters, not the neighbours')
  - Watt-hour accumulation persisted to disk (survives proxy restarts)

Endpoints:
  GET /        → Enphase filtered production data (your inverters only)
  GET /enphase → same
  GET /p1      → HomeWizard P1 Meter
  GET /health  → JSON health/status check

Usage:
    python enphase_proxy.py

Required env vars (or edit the CONFIG section below):
    ENPHASE_USER    your Enphase/Enlighten account e-mail
    ENPHASE_PASS    your Enphase/Enlighten password
    ENVOY_SERIAL    your IQ Gateway serial number (from Enphase app)
    ENVOY_HOST      IP address of your gateway (e.g. 192.168.2.x)
    P1_HOST         IP of your HomeWizard P1 meter (default: 192.168.2.2)

Example:
    ENPHASE_USER=you@example.com ENPHASE_PASS=secret ENVOY_SERIAL=123456789012 \\
    ENVOY_HOST=192.168.2.5 python enphase_proxy.py
"""

import base64
import http.server
import json
import os
import ssl
import time
import urllib.parse
import urllib.request
import urllib.error

# ── CONFIG ────────────────────────────────────────────────────────────────────
PORT          = 8099
ENVOY_HOST    = os.environ.get("ENVOY_HOST",    "192.168.2.5")   # use IP, not envoy.local
P1_HOST       = os.environ.get("P1_HOST",       "192.168.2.2")
ENPHASE_USER  = os.environ.get("ENPHASE_USER",  "")   # your Enlighten e-mail
ENPHASE_PASS  = os.environ.get("ENPHASE_PASS",  "")   # your Enlighten password
ENVOY_SERIAL  = os.environ.get("ENVOY_SERIAL",  "")   # IQ Gateway serial number

# Your 4 inverter serial numbers — neighbours' inverters are excluded
MY_SERIALS = {
    "482314043339",
    "122312149182",
    "122312147071",
    "122312147074",
}

# Where to cache token + daily watt-hour accumulator
TOKEN_FILE = os.path.expanduser("~/.energy_token.json")
STATE_FILE = os.path.expanduser("~/.energy_proxy_state.json")
# ──────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
#  TOKEN MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _decode_jwt_exp(token: str) -> int:
    """Return the 'exp' Unix timestamp from a JWT, or 0 on failure."""
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)   # re-add padding
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data.get('exp', 0))
    except Exception:
        return 0


def _fetch_fresh_token() -> str:
    """
    Obtain a new JWT from Enphase using username/password.
    Flow from the Enphase IQ Gateway tech brief (Jan 2023):
      1. POST enlighten.enphaseenergy.com/login/login.json  → session_id
      2. POST entrez.enphaseenergy.com/tokens               → JWT
    """
    if not ENPHASE_USER or not ENPHASE_PASS or not ENVOY_SERIAL:
        raise RuntimeError(
            "ENPHASE_USER, ENPHASE_PASS and ENVOY_SERIAL must be set for auto token refresh."
        )

    print("[TOKEN] Fetching new token from Enphase…")

    # Step 1 — login to get session_id
    login_body = urllib.parse.urlencode({
        'user[email]':    ENPHASE_USER,
        'user[password]': ENPHASE_PASS,
    }).encode()
    req = urllib.request.Request(
        'https://enlighten.enphaseenergy.com/login/login.json?',
        data=login_body,
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        session_id = json.loads(resp.read())['session_id']

    # Step 2 — exchange session_id + serial for a JWT
    token_body = json.dumps({
        'session_id': session_id,
        'serial_num': ENVOY_SERIAL,
        'username':   ENPHASE_USER,
    }).encode()
    req = urllib.request.Request(
        'https://entrez.enphaseenergy.com/tokens',
        data=token_body,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        token = resp.read().decode().strip()

    exp = _decode_jwt_exp(token)
    print(f"[TOKEN] New token obtained, expires {time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))}")
    return token


class TokenManager:
    """Keeps a valid Enphase JWT, auto-refreshing before it expires."""

    REFRESH_BEFORE_SEC = 5 * 60   # refresh when < 5 min remain

    def __init__(self):
        self._token = ""
        self._exp   = 0
        self._load_cached()

    def _load_cached(self):
        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)
            self._token = data.get('token', '')
            self._exp   = int(data.get('exp', 0))
            if self._token and self._exp > time.time() + self.REFRESH_BEFORE_SEC:
                print(f"[TOKEN] Loaded cached token, expires {time.strftime('%Y-%m-%d %H:%M', time.localtime(self._exp))}")
            else:
                self._token = ""   # force refresh
        except Exception:
            pass

    def _save_cached(self):
        try:
            with open(TOKEN_FILE, 'w') as f:
                json.dump({'token': self._token, 'exp': self._exp}, f)
        except Exception:
            pass

    def get(self) -> str:
        """Return a valid token, refreshing if needed."""
        if not self._token or time.time() > self._exp - self.REFRESH_BEFORE_SEC:
            try:
                self._token = _fetch_fresh_token()
                self._exp   = _decode_jwt_exp(self._token)
                self._save_cached()
            except Exception as e:
                print(f"[TOKEN] Refresh failed: {e}")
                # Return stale token rather than empty string — gateway may still accept it briefly
        return self._token


token_mgr = TokenManager()


# ══════════════════════════════════════════════════════════════════════════════
#  INVERTER STATE — watt-hour accumulation, persisted to disk
# ══════════════════════════════════════════════════════════════════════════════

class InverterState:
    """
    Accumulates watt-hours for YOUR inverters only.
    Resets at midnight. Survives proxy restarts via STATE_FILE.
    """

    def __init__(self):
        self.wh_today   = 0.0
        self.last_watts = 0.0
        self.last_ts    = 0.0
        self.day        = ""
        self._load()

    def _today_str(self):
        return time.strftime('%Y-%m-%d')

    def _load(self):
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
            if d.get('day') == self._today_str():
                self.wh_today   = float(d.get('wh_today',   0))
                self.last_watts = float(d.get('last_watts', 0))
                self.last_ts    = float(d.get('last_ts',    0))
                self.day        = d['day']
                print(f"[STATE] Restored {self.wh_today:.3f} Wh for today")
        except Exception:
            pass

    def _save(self):
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump({
                    'day':         self._today_str(),
                    'wh_today':    round(self.wh_today, 4),
                    'last_watts':  self.last_watts,
                    'last_ts':     self.last_ts,
                }, f)
        except Exception:
            pass

    def update(self, watts: float) -> float:
        """Add the energy produced since last call. Returns wh_today."""
        today = self._today_str()
        if today != self.day:
            self.wh_today = 0.0
            self.day      = today

        now = time.time()
        if self.last_ts > 0 and self.last_watts >= 0:
            elapsed_h = (now - self.last_ts) / 3600.0
            # Only integrate when plausible gap (< 10 min between updates)
            if elapsed_h < 600 / 3600:
                self.wh_today += self.last_watts * elapsed_h

        self.last_watts = watts
        self.last_ts    = now
        self._save()
        return self.wh_today


inv_state = InverterState()


# ══════════════════════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def fetch_enphase_data() -> dict:
    """
    Fetch /api/v1/production/inverters, filter to MY_SERIALS,
    sum watts, and return a dict compatible with the dashboard's expectations.
    """
    token = token_mgr.get()
    url   = f"https://{ENVOY_HOST}/api/v1/production/inverters"
    req   = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")

    with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=6) as resp:
        inverters = json.loads(resp.read())

    # Filter to only your inverters
    mine = [inv for inv in inverters if str(inv.get('serialNumber', '')) in MY_SERIALS]

    watts_now = sum(inv.get('lastReportWatts', 0) for inv in mine)
    wh_today  = inv_state.update(watts_now)

    return {
        'wattsNow':         watts_now,
        'wattHoursToday':   round(wh_today, 1),
        'wattHoursLifetime': 0,       # not available per-inverter from this endpoint
        'inverterCount':    len(mine),
        'inverters':        mine,
    }


def fetch_p1_data() -> tuple:
    url = f"http://{P1_HOST}/api/v1/data"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read(), resp.status


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP SERVER
# ══════════════════════════════════════════════════════════════════════════════

class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass   # suppress default Apache-style logging

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-cache")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/')

        if path == '/health':
            self._json(200, {
                "status":      "ok",
                "envoy":       ENVOY_HOST,
                "p1":          P1_HOST,
                "credentials": bool(ENPHASE_USER and ENPHASE_PASS and ENVOY_SERIAL),
                "my_serials":  list(MY_SERIALS),
                "wh_today":    round(inv_state.wh_today, 1),
            })
            return

        if path == '/p1':
            self._proxy_raw(fetch_p1_data, "P1")
            return

        # Default (/ or /enphase) → Enphase filtered data
        self._proxy_json(fetch_enphase_data, "Enphase")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_cors()
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_json(self, fetch_fn, label):
        """Call fetch_fn() → dict, serialise and return with CORS headers."""
        try:
            data = fetch_fn()
            self._json(200, data)
            print(f"[OK]  {label}: {data.get('wattsNow', '?')} W, {data.get('wattHoursToday', '?')} Wh today")
        except urllib.error.URLError as e:
            self._json(502, {"error": str(e.reason)})
            print(f"[ERR] {label}: {e}")
        except Exception as e:
            self._json(500, {"error": str(e)})
            print(f"[ERR] {label}: {e}")

    def _proxy_raw(self, fetch_fn, label):
        """Call fetch_fn() → (bytes, status), return raw bytes with CORS headers."""
        try:
            body, status = fetch_fn()
            self.send_response(status)
            self.send_cors()
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(f"[OK]  {label}: {len(body)} bytes")
        except urllib.error.URLError as e:
            self._json(502, {"error": str(e.reason)})
            print(f"[ERR] {label}: {e}")
        except Exception as e:
            self._json(500, {"error": str(e)})
            print(f"[ERR] {label}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"Energy Dashboard Proxy  —  http://localhost:{PORT}")
    print(f"  Enphase gateway : https://{ENVOY_HOST}/api/v1/production/inverters")
    print(f"  P1 meter        : http://{P1_HOST}/api/v1/data")
    print(f"  My inverters    : {sorted(MY_SERIALS)}")
    print(f"  Auto-refresh    : {'enabled' if ENPHASE_USER else 'DISABLED (set ENPHASE_USER/PASS/SERIAL)'}")
    print(f"  Today so far    : {inv_state.wh_today:.1f} Wh")
    print(f"  Health URL      : http://localhost:{PORT}/health")
    print("Press Ctrl+C to stop.\n")

    server = http.server.HTTPServer(("127.0.0.1", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nProxy stopped.")
