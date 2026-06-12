"""
tests/conftest.py — Pytest fixtures for server and desktop apps.
"""

import os
import tempfile
import pytest

# Monkeypatch Config.get_database_uri to use an in-memory database for testing
from config import Config
Config.get_database_uri = lambda *args, **kwargs: "sqlite:///:memory:"

from app import create_app
from extensions import db as _db


@pytest.fixture
def server_app():
    """Fixture to set up a test app in 'server' mode."""
    # Force development configs and server mode
    os.environ["FLASK_DEBUG"] = "1"
    os.environ["APP_MODE"] = "server"
    
    app = create_app("server")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def server_client(server_app):
    """Fixture to return a test client for server mode."""
    return server_app.test_client()


@pytest.fixture
def desktop_app():
    """Fixture to set up a test app in 'desktop' mode."""
    # Force development configs and desktop mode
    os.environ["FLASK_DEBUG"] = "1"
    os.environ["APP_MODE"] = "desktop"
    
    app = create_app("desktop")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    
    # Configure a temporary upload directory for tests
    temp_dir = tempfile.TemporaryDirectory()
    app.config["UPLOAD_FOLDER"] = temp_dir.name
    
    # Ensure serial token path doesn't overwrite real file
    # Mock token file path in routes.desktop
    from routes import desktop
    temp_token_file = os.path.join(temp_dir.name, "serial.txt")
    desktop.TOKEN_PATH = temp_token_file
    
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()
        
    temp_dir.cleanup()


@pytest.fixture
def desktop_client(desktop_app):
    """Fixture to return a test client for desktop mode."""
    return desktop_app.test_client()
