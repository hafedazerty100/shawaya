import os

# Force production server settings so it never crashes on missing environment variables
os.environ["APP_MODE"] = "server"
os.environ["FLASK_DEBUG"] = "0"
os.environ.setdefault("SECRET_KEY", "pella-cloud-secret-key-super-secure")
os.environ.setdefault("SYNC_API_KEY", "pella-cloud-sync-key-super-secure")

from app import create_app

# This 'app' variable is what Pella will hook into
app = create_app("server")
