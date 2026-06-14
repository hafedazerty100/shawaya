"""
app.py — Application factory.

Usage:
    from app import create_app
    app = create_app('server')   # or 'desktop'
"""

import logging
import logging.handlers
import os
import sys

from flask import Flask, jsonify, render_template

from config import DevelopmentConfig, ProductionConfig
from extensions import csrf, db, limiter, login_manager, migrate


def _configure_logging(app: Flask) -> None:
    """Set up rotating file + console logging."""
    log_dir = os.path.join(app.root_path, "logs")
    os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler (10 MB, keep 5 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.DEBUG if app.debug else logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def _seed_admin(app: Flask) -> None:
    """Seed the default admin user in server mode if none exists."""
    from models import AdminUser
    from werkzeug.security import generate_password_hash

    with app.app_context():
        try:
            if AdminUser.query.count() == 0:
                username = app.config.get("ADMIN_DEFAULT_USERNAME", "admin")
                password = app.config.get("ADMIN_DEFAULT_PASSWORD", "changeme123")
                admin = AdminUser(
                    username=username,
                    password_hash=generate_password_hash(password),
                    # If they explicitly set a custom password in Env Vars, don't force them to change it
                    must_change_password=(password == "changeme123"),
                )
                db.session.add(admin)
                db.session.commit()
                logging.getLogger("app").info(
                    "Seeded default admin user '%s'. "
                    "Change the password on first login!",
                    username,
                )
        except Exception as exc:
            db.session.rollback()
            logging.getLogger("app").error("Failed to seed admin: %s", exc)


def _register_error_handlers(app: Flask) -> None:
    """Register JSON error handlers for /api/* and HTML handlers for everything else."""

    def wants_json() -> bool:
        from flask import request
        return request.path.startswith("/api/")

    @app.errorhandler(400)
    def bad_request(e):
        if wants_json():
            return jsonify({"error": "Bad request", "detail": str(e)}), 400
        return render_template("errors/400.html"), 400

    @app.errorhandler(401)
    def unauthorized(e):
        if wants_json():
            return jsonify({"error": "Unauthorized"}), 401
        return render_template("errors/401.html"), 401

    @app.errorhandler(403)
    def forbidden(e):
        if wants_json():
            return jsonify({"error": "Forbidden"}), 403
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        if wants_json():
            return jsonify({"error": "Not found"}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        # Never leak stack traces to clients — log them instead
        logging.getLogger("app").exception("Internal server error")
        if wants_json():
            return jsonify({"error": "Internal server error"}), 500
        return render_template("errors/500.html"), 500


def create_app(mode: str | None = None) -> Flask:
    """
    Application factory.

    Args:
        mode: 'server' or 'desktop'. Falls back to APP_MODE env var.
    """
    app = Flask(__name__)

    # ── Determine run mode ────────────────────────────────────────────────────
    if mode is None:
        mode = os.environ.get("APP_MODE", "server").lower()
    app.config["MODE"] = mode

    # ── Load config ───────────────────────────────────────────────────────────
    debug = os.environ.get("FLASK_DEBUG", "0").strip() == "1"
    cfg = DevelopmentConfig() if debug else ProductionConfig()
    app.config.from_object(cfg)

    # Set database URI based on mode
    app.config["SQLALCHEMY_DATABASE_URI"] = cfg.get_database_uri(mode)

    # ── Configure logging ─────────────────────────────────────────────────────
    _configure_logging(app)
    logger = logging.getLogger("app")
    logger.info("Starting Coffee Shop POS in '%s' mode (debug=%s).", mode, debug)

    # ── Bind extensions ───────────────────────────────────────────────────────
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "admin.login"
    login_manager.login_message = "Please log in to access this page."
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id: str):
        from models import AdminUser
        return db.session.get(AdminUser, int(user_id))

    # ── Register blueprints ───────────────────────────────────────────────────
    if mode == "desktop":
        from routes.desktop import desktop_bp
        app.register_blueprint(desktop_bp)
        # Exempt JSON API endpoints from CSRF (machine-to-machine, no browser session)
        csrf.exempt(desktop_bp)
    else:
        # Server mode: admin dashboard + sync API
        from routes.admin import admin_bp
        from routes.api import api_bp
        app.register_blueprint(admin_bp)
        app.register_blueprint(api_bp)
        # Exempt the sync API blueprint from CSRF (API-key authenticated, not browser)
        csrf.exempt(api_bp)
        
        # Redirect the main URL (/) to the Admin Login so it doesn't show a 404
        from flask import redirect, url_for
        @app.route("/")
        def index():
            return redirect(url_for("admin.login"))

    # ── Register error handlers ───────────────────────────────────────────────
    _register_error_handlers(app)

    # ── Jinja2 globals ────────────────────────────────────────────────────────
    from utils import format_price
    app.jinja_env.globals["format_price"] = format_price
    app.jinja_env.globals["getattr"] = getattr
    app.jinja_env.globals["hasattr"] = hasattr

    # ── Create tables + seed admin (server mode only) ─────────────────────────
    with app.app_context():
        db.create_all()
        if mode == "server":
            _seed_admin(app)
            
            # Safe migration for the new image_data columns
            try:
                from sqlalchemy import text
                engine_name = db.engine.name.lower()
                col_type = "BYTEA" if "postgres" in engine_name or "neon" in engine_name else "BLOB"
                db.session.execute(text(f"ALTER TABLE products ADD COLUMN image_data {col_type}"))
                db.session.commit()
            except Exception:
                db.session.rollback()

            try:
                from sqlalchemy import text
                db.session.execute(text("ALTER TABLE products ADD COLUMN image_mime VARCHAR(50)"))
                db.session.commit()
            except Exception:
                db.session.rollback()

    # ── Custom Image Route ────────────────────────────────────────────────────
    @app.route('/static/uploads/products/<path:filename>')
    def serve_product_image(filename):
        from models import Product
        from flask import send_file, send_from_directory, abort
        import io
        import os
        
        product = Product.query.filter_by(image=filename).first()
        if product and product.image_data:
            return send_file(
                io.BytesIO(product.image_data),
                mimetype=product.image_mime or "image/jpeg"
            )
            
        # Fallback to local file system if not in DB
        try:
            return send_from_directory(os.path.join(app.root_path, 'static', 'uploads', 'products'), filename)
        except Exception:
            abort(404)

    return app
