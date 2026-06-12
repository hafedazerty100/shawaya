"""
start.py — ONE COMMAND to run everything.

    python start.py

What this does:
  1. Installs any missing dependencies automatically
  2. Starts the Admin Server       → http://localhost:5000
  3. Opens a public ngrok tunnel   → https://buffer-handbag-evident.ngrok-free.dev
  4. Starts the Kiosk              → http://localhost:5001
  5. Kiosk syncs orders to server automatically (works offline too)
  6. Press Ctrl+C to stop everything cleanly
"""

import os
import sys
import subprocess
import time
import signal
import threading

# Force UTF-8 output on Windows CMD
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── 0. Auto-install missing deps ──────────────────────────────────────────────

def _ensure_deps():
    try:
        import flask, pyngrok, waitress, dotenv
    except ImportError:
        print("[SETUP] Installing dependencies...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"]
        )
        print("[SETUP] Dependencies installed.\n")

_ensure_deps()

# ── 1. Load env ────────────────────────────────────────────────────────────────

from dotenv import load_dotenv
load_dotenv()

NGROK_AUTH_TOKEN = "3EXMRTHVAblAHedIbaBO441JpLy_2YLVuDRKacV9kGYbtnik1"
NGROK_DOMAIN     = "buffer-handbag-evident.ngrok-free.dev"
PUBLIC_URL       = f"https://{NGROK_DOMAIN}"
SERVER_PORT      = int(os.environ.get("SERVER_PORT", "5000"))
KIOSK_PORT       = int(os.environ.get("DESKTOP_PORT", "5001"))

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── 2. Start ngrok tunnel ─────────────────────────────────────────────────────

def _start_ngrok():
    from pyngrok import ngrok, conf
    conf.get_default().auth_token = NGROK_AUTH_TOKEN
    try:
        tunnel = ngrok.connect(addr=SERVER_PORT, proto="http", hostname=NGROK_DOMAIN)
        return tunnel
    except Exception as exc:
        msg = str(exc).encode("ascii", errors="replace").decode("ascii")
        print(f"\n[NGROK] !! Could not open tunnel: {msg}")
        print("[NGROK]    The server still runs locally -- kiosk will queue orders offline.\n")
        return None

print("[1/4] Connecting to ngrok...")
tunnel = _start_ngrok()

if tunnel:
    print(f"[1/4] OK  ngrok tunnel active -> {PUBLIC_URL}")
else:
    print(f"[1/4] XX  ngrok offline -- running in local mode only")

# ── 3. Start Admin Server ─────────────────────────────────────────────────────

print("[2/4] Starting admin server...")
server_env = os.environ.copy()
server_env["APP_MODE"] = "server"
server_env["FLASK_DEBUG"] = "0"
server_env["PYTHONIOENCODING"] = "utf-8"

server_proc = subprocess.Popen(
    [sys.executable, os.path.join(ROOT, "run_server.py")],
    env=server_env,
    cwd=ROOT,
)
time.sleep(2)  # Let server boot before kiosk tries to validate
if server_proc.poll() is not None:
    print("[ERROR] Admin server failed to start. Check logs above.")
    sys.exit(1)
print(f"[2/4] OK  Admin server running -> http://localhost:{SERVER_PORT}")

# ── 4. Start Kiosk ────────────────────────────────────────────────────────────

print("[3/4] Starting kiosk...")
kiosk_env = os.environ.copy()
kiosk_env["APP_MODE"] = "desktop"
kiosk_env["FLASK_DEBUG"] = "0"
kiosk_env["PYTHONIOENCODING"] = "utf-8"
# Kiosk syncs to the PUBLIC ngrok URL so it works from any machine
kiosk_env["SERVER_URL"] = PUBLIC_URL if tunnel else f"http://localhost:{SERVER_PORT}"

kiosk_proc = subprocess.Popen(
    [sys.executable, os.path.join(ROOT, "run_desktop.py")],
    env=kiosk_env,
    cwd=ROOT,
)
time.sleep(2)
if kiosk_proc.poll() is not None:
    print("[ERROR] Kiosk failed to start. Check logs above.")
    server_proc.terminate()
    sys.exit(1)
print(f"[3/4] OK  Kiosk running         -> http://localhost:{KIOSK_PORT}")

# ── 5. Print summary ──────────────────────────────────────────────────────────

print()
print("=" * 62)
print("  COFFEE SHOP POS -- ALL SYSTEMS RUNNING")
print("=" * 62)
print()
if tunnel:
    print(f"  [WEB]  OWNER DASHBOARD (anywhere, any device):")
    print(f"         {PUBLIC_URL}/admin/login")
    print()
print(f"  [PC]   LOCAL ADMIN (this machine):")
print(f"         http://localhost:{SERVER_PORT}/admin/login")
print(f"  [SHOP] KIOSK (this machine):")
print(f"         http://localhost:{KIOSK_PORT}")
print()
print(f"  [SYNC] kiosk -> server every 10s")
print(f"         works offline, auto-retries when net returns")
print()
print(f"  [STOP] Press Ctrl+C to stop everything")
print("=" * 62)
print()

# Launch Edge with kiosk printing enabled
try:
    print("[4/4] Launching Microsoft Edge in kiosk printing mode...")
    subprocess.Popen("start msedge --kiosk-printing http://localhost:5001", shell=True)
except Exception as e:
    print(f"[WARN] Failed to launch Edge: {e}")



def _monitor(proc, name):
    """Restart a process if it crashes."""
    while True:
        proc.wait()
        if _stopping:
            break
        print(f"\n[WARN] {name} crashed — restarting in 3 s...")
        time.sleep(3)
        proc = subprocess.Popen(proc.args, env=proc.__dict__.get("env"), cwd=ROOT)

_stopping = False

monitor_server = threading.Thread(target=_monitor, args=(server_proc, "Admin server"), daemon=True)
monitor_kiosk  = threading.Thread(target=_monitor, args=(kiosk_proc,  "Kiosk"),        daemon=True)
monitor_server.start()
monitor_kiosk.start()

def _shutdown(sig=None, frame=None):
    global _stopping
    _stopping = True
    print("\n[STOP] Shutting down...")
    server_proc.terminate()
    kiosk_proc.terminate()
    try:
        from pyngrok import ngrok
        ngrok.kill()
    except Exception:
        pass
    server_proc.wait()
    kiosk_proc.wait()
    print("[STOP] All processes stopped. Goodbye!")
    sys.exit(0)

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# Keep main thread alive
while True:
    time.sleep(1)
