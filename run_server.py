"""
run_server.py — Entrypoint for the server (admin dashboard) mode.

Starts the Flask app on PORT env var (cloud) or 5000 (local).
In production, uses waitress. Supports Render, Fly.io, Railway, etc.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("APP_MODE", "server")

from app import create_app

app = create_app("server")

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0").strip() == "1"
    # Cloud platforms set PORT env var; local default is 5000
    port = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", "5000")))
    host = "0.0.0.0"

    if debug:
        print(f"[DEV] Running Flask dev server on http://localhost:{port}")
        app.run(host=host, port=port, debug=True)
    else:
        try:
            from waitress import serve
            print(f"[PROD] Serving with waitress on {host}:{port}")
            serve(app, host=host, port=port, threads=4)
        except ImportError:
            print(
                "[WARNING] waitress not installed — falling back to Flask dev server.",
                file=sys.stderr,
            )
            app.run(host=host, port=port, debug=False)
