"""
run_public.py — Start the admin server AND expose it publicly via ngrok.

Usage:
    python run_public.py

Required .env variables:
    NGROK_AUTH_TOKEN   — your ngrok auth token (from https://dashboard.ngrok.com/authtokens)
    NGROK_DOMAIN       — your free static domain  (from https://dashboard.ngrok.com/domains)
                         e.g.  your-shop-name.ngrok-free.app
                         Leave blank to get a random URL each run (not recommended).

The script will:
    1. Start the Flask admin server (waitress, port 5000)
    2. Open an ngrok HTTPS tunnel to that port
    3. Print the public URL for the owner and kiosk config
    4. Keep running until Ctrl+C
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("APP_MODE", "server")


# ── 1. Validate ngrok config ───────────────────────────────────────────────────

NGROK_AUTH_TOKEN = os.environ.get("NGROK_AUTH_TOKEN", "").strip()
NGROK_DOMAIN     = os.environ.get("NGROK_DOMAIN", "").strip()
SERVER_PORT      = int(os.environ.get("SERVER_PORT", "5000"))

if not NGROK_AUTH_TOKEN:
    print("=" * 60)
    print("  ERROR: NGROK_AUTH_TOKEN is not set in your .env file.")
    print()
    print("  Steps to fix:")
    print("  1. Sign up free at https://ngrok.com")
    print("  2. Copy your auth token from:")
    print("     https://dashboard.ngrok.com/authtokens")
    print("  3. Add to .env:")
    print("     NGROK_AUTH_TOKEN=your-token-here")
    print("=" * 60)
    sys.exit(1)


# ── 2. Start Flask server in a background thread ───────────────────────────────

from app import create_app

app = create_app("server")


def _run_flask():
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=SERVER_PORT, threads=4)
    except ImportError:
        app.run(host="0.0.0.0", port=SERVER_PORT, debug=False)


flask_thread = threading.Thread(target=_run_flask, daemon=True)
flask_thread.start()
print(f"[SERVER] Admin server started on http://localhost:{SERVER_PORT}")

# Give Flask a moment to boot
time.sleep(2)


# ── 3. Start ngrok tunnel ──────────────────────────────────────────────────────

try:
    from pyngrok import ngrok, conf
except ImportError:
    print("ERROR: pyngrok not installed. Run:  pip install pyngrok")
    sys.exit(1)

# Set auth token
conf.get_default().auth_token = NGROK_AUTH_TOKEN

# Open tunnel — use static domain if provided
tunnel_options = {"addr": SERVER_PORT, "proto": "http"}
if NGROK_DOMAIN:
    tunnel_options["hostname"] = NGROK_DOMAIN

try:
    tunnel = ngrok.connect(**tunnel_options)
    public_url = tunnel.public_url
    # ngrok always returns https for http tunnels on named domains
    if public_url.startswith("http://"):
        public_url = "https://" + public_url[7:]
except Exception as exc:
    print(f"ERROR: Could not start ngrok tunnel: {exc}")
    print("Check your NGROK_AUTH_TOKEN and internet connection.")
    sys.exit(1)


# ── 4. Print instructions ──────────────────────────────────────────────────────

print()
print("=" * 60)
print("  ☕  COFFEE SHOP POS — ONLINE")
print("=" * 60)
print()
print(f"  🌐 Public URL (owner dashboard):")
print(f"     {public_url}/admin/login")
print()
print(f"  🖥️  Local URL (same machine):")
print(f"     http://localhost:{SERVER_PORT}/admin/login")
print()
print("  📋 Kiosk .env — set these on every kiosk machine:")
print(f"     SERVER_URL={public_url}")
print(f"     SYNC_API_KEY={os.environ.get('SYNC_API_KEY', '<your-sync-api-key>')}")
print()
print("  Press Ctrl+C to stop.")
print("=" * 60)
print()


# ── 5. Keep running until Ctrl+C ──────────────────────────────────────────────

try:
    ngrok.run()  # blocks until interrupted
except KeyboardInterrupt:
    print("\n[STOPPED] Shutting down ngrok tunnel...")
    ngrok.kill()
    print("[STOPPED] Done.")
