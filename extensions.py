"""
extensions.py — Flask extension instances (unbound from app) with multi-DB failover.

Includes custom SQLAlchemy FailoverSession to rotate between Neon instances.

SECURITY: DB credentials are NEVER hardcoded here. They are loaded from:
  1. db_urls.json (local file, in .gitignore)
  2. DATABASE_URL environment variable (set by Render/Fly/Railway)
"""

import json
import logging
import os

from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_sqlalchemy.session import Session
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy.exc import OperationalError, InterfaceError, DatabaseError

logger = logging.getLogger("db_failover")

# ── Load DB URLs from gitignored file (NEVER hardcode credentials here) ─────
base_dir = os.path.abspath(os.path.dirname(__file__))
urls_file = os.path.join(base_dir, "db_urls.json")

DB_URLS: list[str] = []

# 1. Load from db_urls.json (persisted via admin UI, gitignored)
if os.path.exists(urls_file):
    try:
        with open(urls_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, list):
                DB_URLS = list(loaded)
    except Exception as _e:
        logger.warning("Could not load db_urls.json: %s", _e)

# 1.5 Load from DB_URLS_LIST environment variable (persists across Render restarts)
_env_urls_list = os.environ.get("DB_URLS_LIST", "").strip()
if _env_urls_list:
    try:
        loaded = json.loads(_env_urls_list)
        if isinstance(loaded, list):
            for url in loaded:
                if url not in DB_URLS:
                    DB_URLS.append(url)
    except Exception:
        # Fallback to comma-separated list
        for url in _env_urls_list.split(","):
            url = url.strip()
            if url and url not in DB_URLS:
                DB_URLS.append(url)

# 2. Always include DATABASE_URL env var if set (Render/Fly primary DB)
_env_db_url = os.environ.get("DATABASE_URL", "").strip()
if _env_db_url:
    if _env_db_url.startswith("postgres://"):
        _env_db_url = "postgresql://" + _env_db_url[len("postgres://"):]
    if _env_db_url not in DB_URLS:
        DB_URLS.insert(0, _env_db_url)  # Env var is the primary, always first

_active_db_index = 0

def get_active_db_index() -> int:
    return _active_db_index

def rebuild_db_engines(target_app) -> None:
    import sqlalchemy as sa
    if not target_app:
        return
    with target_app.app_context():
        # Rebuild the engines for target_app based on the updated config
        if hasattr(db, '_app_engines') and target_app in db._app_engines:
            engines = db._app_engines[target_app]
            for engine in list(engines.values()):
                try:
                    engine.dispose()
                except Exception:
                    pass
            engines.clear()
        else:
            if not hasattr(db, '_app_engines'):
                db._app_engines = {}
            db._app_engines.setdefault(target_app, {})
            engines = db._app_engines[target_app]

        basic_uri = target_app.config.get("SQLALCHEMY_DATABASE_URI")
        basic_engine_options = db._engine_options.copy()
        basic_engine_options.update(target_app.config.get("SQLALCHEMY_ENGINE_OPTIONS", {}))
        echo = target_app.config.get("SQLALCHEMY_ECHO", False)
        config_binds = target_app.config.get("SQLALCHEMY_BINDS", {})
        
        engine_options = {}
        for key, value in config_binds.items():
            engine_options[key] = db._engine_options.copy()
            if isinstance(value, (str, sa.engine.URL)):
                engine_options[key]["url"] = value
            else:
                engine_options[key].update(value)
                
        if basic_uri is not None:
            basic_engine_options["url"] = basic_uri
        if "url" in basic_engine_options:
            engine_options.setdefault(None, {}).update(basic_engine_options)
            
        for key, options in engine_options.items():
            db._make_metadata(key)
            options.setdefault("echo", echo)
            options.setdefault("echo_pool", echo)
            db._apply_driver_defaults(options, target_app)
            engines[key] = db._make_engine(key, options, target_app)

def switch_to_next_db(app=None) -> str:
    global _active_db_index
    from flask import current_app
    import sys
    
    target_app = app or (current_app._get_current_object() if current_app else None)
    is_testing = ("pytest" in sys.modules) or os.environ.get("TESTING") == "1" or (target_app and target_app.config.get("TESTING"))
    if is_testing or (target_app and target_app.config.get("MODE") == "desktop"):
        # SQLite local database or test suite does not have fallback options
        return target_app.config.get("SQLALCHEMY_DATABASE_URI", "") if target_app else "sqlite:///:memory:"
        
    _active_db_index = (_active_db_index + 1) % len(DB_URLS)
    new_url = DB_URLS[_active_db_index]
    
    if target_app:
        target_app.config["SQLALCHEMY_DATABASE_URI"] = new_url
        logger.info("Switched SQLALCHEMY_DATABASE_URI to DB index %d: %s", _active_db_index, new_url)
        rebuild_db_engines(target_app)
        
    return new_url

class FailoverSession(Session):
    def execute(self, statement, *args, **kwargs):
        from flask import current_app
        import sys
        
        is_testing = ("pytest" in sys.modules) or os.environ.get("TESTING") == "1" or (current_app and current_app.config.get("TESTING"))
        disable_failover = is_testing or (current_app and current_app.config.get("MODE") == "desktop")
        
        retries = 1 if disable_failover else len(DB_URLS)
        for attempt in range(retries):
            try:
                return super().execute(statement, *args, **kwargs)
            except (OperationalError, InterfaceError) as exc:
                if attempt < retries - 1:
                    logger.warning("DB failover triggered during execute on index %d: %s", _active_db_index, exc)
                    switch_to_next_db()
                    try:
                        self.rollback()
                    except Exception:
                        pass
                else:
                    raise exc

    def commit(self):
        from flask import current_app
        import sys
        
        is_testing = ("pytest" in sys.modules) or os.environ.get("TESTING") == "1" or (current_app and current_app.config.get("TESTING"))
        disable_failover = is_testing or (current_app and current_app.config.get("MODE") == "desktop")
        
        if disable_failover:
            super().commit()
            return
            
        try:
            super().commit()
        except (OperationalError, InterfaceError) as exc:
            logger.warning("DB failover triggered during commit on index %d: %s", _active_db_index, exc)
            switch_to_next_db()
            try:
                self.rollback()
            except Exception:
                pass
            raise exc

db = SQLAlchemy(session_options={"class_": FailoverSession})
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)
