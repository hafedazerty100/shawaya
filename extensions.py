"""
extensions.py — Flask extension instances (unbound from app) with multi-DB failover.

Includes custom SQLAlchemy FailoverSession to rotate between Neon instances.
"""

import logging
from flask_sqlalchemy import SQLAlchemy
from flask_sqlalchemy.session import Session
from sqlalchemy.exc import OperationalError, InterfaceError, DatabaseError
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

logger = logging.getLogger("db_failover")

# Multi-DB fallback accounts provided by user
DB_URLS = [
    "postgresql://neondb_owner:npg_DWMBL10dhXkj@ep-lingering-resonance-abun03tf-pooler.eu-west-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
    "postgresql://neondb_owner:npg_VbFmLnR0ThP5@ep-super-sound-ab5v2qrt-pooler.eu-west-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
    "postgresql://neondb_owner:npg_ATn9EDIkdB8X@ep-shiny-sky-abtyuysv-pooler.eu-west-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
]

# Prepend the active database URL from environment variables as the primary database
import os
env_db_url = os.environ.get("DATABASE_URL", "")
if env_db_url:
    if env_db_url.startswith("postgres://"):
        env_db_url = "postgresql://" + env_db_url[len("postgres://"):]
    if env_db_url not in DB_URLS:
        DB_URLS.insert(0, env_db_url)

_active_db_index = 0

def get_active_db_index() -> int:
    return _active_db_index

def switch_to_next_db(app=None) -> str:
    global _active_db_index
    from flask import current_app
    
    target_app = app or (current_app._get_current_object() if current_app else None)
    if target_app and target_app.config.get("MODE") == "desktop":
        # SQLite local database does not have fallback options
        return target_app.config.get("SQLALCHEMY_DATABASE_URI", "")
        
    _active_db_index = (_active_db_index + 1) % len(DB_URLS)
    new_url = DB_URLS[_active_db_index]
    
    if target_app:
        target_app.config["SQLALCHEMY_DATABASE_URI"] = new_url
        logger.info("Switched SQLALCHEMY_DATABASE_URI to DB index %d: %s", _active_db_index, new_url)
        
        with target_app.app_context():
            try:
                if hasattr(db, '_app_engines'):
                    db._app_engines.clear()
            except Exception:
                pass
            try:
                if hasattr(db, '_engines'):
                    db._engines.clear()
            except Exception:
                pass
            try:
                db.engines.clear()
            except Exception:
                pass
        
    return new_url

class FailoverSession(Session):
    def execute(self, statement, params=None, bind=None, **kwargs):
        from flask import current_app
        is_desktop = current_app and current_app.config.get("MODE") == "desktop"
        
        retries = 1 if is_desktop else len(DB_URLS)
        for attempt in range(retries):
            try:
                return super().execute(statement, params, bind, **kwargs)
            except (OperationalError, InterfaceError, DatabaseError) as exc:
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
        is_desktop = current_app and current_app.config.get("MODE") == "desktop"
        
        retries = 1 if is_desktop else len(DB_URLS)
        for attempt in range(retries):
            try:
                super().commit()
                return
            except (OperationalError, InterfaceError, DatabaseError) as exc:
                if attempt < retries - 1:
                    logger.warning("DB failover triggered during commit on index %d: %s", _active_db_index, exc)
                    switch_to_next_db()
                    try:
                        self.rollback()
                    except Exception:
                        pass
                else:
                    raise exc

db = SQLAlchemy(session_options={"class_": FailoverSession})
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)
