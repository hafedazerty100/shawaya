"""
config.py — Application configuration classes.

Loads all settings from environment variables (via python-dotenv).
Never hardcode secrets here; add them to .env (gitignored).
"""

import os
from dotenv import load_dotenv

# Load .env file before anything else reads os.environ
load_dotenv()

# ─── Developer-mode defaults ─────────────────────────────────────────────────
# These are ONLY safe for local development. Production must override via .env.
_DEV_SECRET_KEY = "dev-insecure-secret-key-change-me"
_DEV_SYNC_API_KEY = "dev-insecure-sync-api-key"


class Config:
    """Base configuration — shared by all modes."""

    # ── Core ──────────────────────────────────────────────────────────────────
    SECRET_KEY: str = os.environ.get("SECRET_KEY", _DEV_SECRET_KEY)
    SYNC_API_KEY: str = os.environ.get("SYNC_API_KEY", _DEV_SYNC_API_KEY)

    # Warn loudly if running with default secrets
    _using_default_secret = SECRET_KEY == _DEV_SECRET_KEY
    _using_default_sync_key = SYNC_API_KEY == _DEV_SYNC_API_KEY

    # ── SQLAlchemy ────────────────────────────────────────────────────────────
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Application mode ──────────────────────────────────────────────────────
    # APP_MODE must be "server" or "desktop"
    MODE: str = os.environ.get("APP_MODE", "server").lower()

    @staticmethod
    def get_database_uri(mode: str) -> str:
        """Return the database URI for the given mode.

        Priority:
          1. DATABASE_URL env var (set by Render/Fly/Railway PostgreSQL)
          2. SQLite file (local development)
        """
        # Cloud platforms provide DATABASE_URL (PostgreSQL)
        db_url = os.environ.get("DATABASE_URL", "")
        if db_url and mode == "server":
            # Render uses postgres:// but SQLAlchemy needs postgresql://
            if db_url.startswith("postgres://"):
                db_url = "postgresql://" + db_url[len("postgres://"):]
            return db_url
        base = os.path.abspath(os.path.dirname(__file__))
        if mode == "desktop":
            return f"sqlite:///{os.path.join(base, 'local_data.db')}"
        return f"sqlite:///{os.path.join(base, 'server_data.db')}"

    # ── File uploads ──────────────────────────────────────────────────────────
    UPLOAD_FOLDER: str = os.path.join(
        os.path.abspath(os.path.dirname(__file__)), "static", "uploads", "products"
    )
    MAX_CONTENT_LENGTH: int = 5 * 1024 * 1024  # 5 MB

    # ── Sync (desktop mode) ───────────────────────────────────────────────────
    SERVER_URL: str = os.environ.get("SERVER_URL", "http://localhost:5000")
    SYNC_INTERVAL: int = int(os.environ.get("SYNC_INTERVAL", "30"))

    # ── Admin seeding ─────────────────────────────────────────────────────────
    ADMIN_DEFAULT_USERNAME: str = os.environ.get("ADMIN_DEFAULT_USERNAME", "admin")
    ADMIN_DEFAULT_PASSWORD: str = os.environ.get(
        "ADMIN_DEFAULT_PASSWORD", "changeme123"
    )

    # ── Session / Cookie security ─────────────────────────────────────────────
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Secure flag overridden per subclass
    SESSION_COOKIE_SECURE = False

    # ── Rate limiting ─────────────────────────────────────────────────────────
    RATELIMIT_STORAGE_URL = "memory://"


class DevelopmentConfig(Config):
    """Local development — permissive settings."""

    DEBUG = True
    TESTING = False
    SESSION_COOKIE_SECURE = False  # HTTP is fine in dev
    WTF_CSRF_ENABLED = True


class ProductionConfig(Config):
    """Production — strict settings."""

    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = True  # Require HTTPS
    WTF_CSRF_ENABLED = True

    def __init__(self):
        # Warn loudly if critical secrets are still at default values
        missing = []
        if Config.SECRET_KEY == "dev-insecure-secret-key-change-me":
            missing.append("SECRET_KEY")
        if Config.SYNC_API_KEY == "dev-insecure-sync-api-key":
            missing.append("SYNC_API_KEY")
        if missing:
            import logging
            logging.getLogger("config").warning(
                "[WARNING] The following env vars are using insecure defaults: %s. "
                "Set them in your environment or .env file before going live.",
                ", ".join(missing),
            )


def get_config() -> Config:
    """Return the appropriate config object based on FLASK_DEBUG env var."""
    debug = os.environ.get("FLASK_DEBUG", "0").strip() == "1"
    if debug:
        cfg = DevelopmentConfig()
        if Config._using_default_secret:
            print(
                "[WARNING] Running with default SECRET_KEY — safe for dev only!",
                file=sys.stderr,
            )
        if Config._using_default_sync_key:
            print(
                "[WARNING] Running with default SYNC_API_KEY — safe for dev only!",
                file=sys.stderr,
            )
        return cfg
    return ProductionConfig()
