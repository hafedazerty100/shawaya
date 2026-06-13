"""
wsgi.py — WSGI entrypoint for production servers (gunicorn, waitress).

Usage on Render / Railway / Fly.io:
    gunicorn wsgi:app
    OR
    python run_server.py
"""
import os
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("APP_MODE", "server")

from app import create_app  # noqa: E402

app = create_app("server")
