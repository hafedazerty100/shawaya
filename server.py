import os
import sys

# Force production server settings
os.environ["APP_MODE"] = "server"
os.environ["FLASK_DEBUG"] = "0"
os.environ.setdefault("SECRET_KEY", "pella-cloud-secret-key-super-secure")
os.environ.setdefault("SYNC_API_KEY", "pella-cloud-sync-key-super-secure")

from app import create_app

app = create_app("server")

if __name__ == "__main__":
    # Pella and similar panels usually provide a PORT env var or expect 8080/5000
    port = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", "8080")))
    print(f"Starting server on 0.0.0.0:{port}...", flush=True)
    
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=4)
    except ImportError:
        app.run(host="0.0.0.0", port=port, debug=False)
