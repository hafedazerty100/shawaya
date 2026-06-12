"""
run_desktop.py — Entrypoint for the desktop kiosk mode.

Starts the Flask app on port 5001 and launches the background sync thread.
"""

import os
import sys

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("APP_MODE", "desktop")

from app import create_app
from sync import start_sync_thread

app = create_app("desktop")

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0").strip() == "1"
    port = int(os.environ.get("DESKTOP_PORT", "5001"))

    # Start background sync daemon thread
    start_sync_thread(app)
    print(f"[KIOSK] Background sync thread started.")

    if debug:
        print(f"[DEV] Running Flask dev server on http://localhost:{port}")
        # use_reloader=False prevents the sync thread from starting twice
        app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
    else:
        try:
            from waitress import serve
            print(f"[PROD] Serving kiosk with waitress on http://0.0.0.0:{port}")
            serve(app, host="0.0.0.0", port=port, threads=4)
        except ImportError:
            print(
                "[WARNING] waitress not installed — falling back to Flask dev server.",
                file=sys.stderr,
            )
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
