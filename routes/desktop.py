"""
routes/desktop.py — Desktop kiosk Blueprint.

Routes:
  GET  /                           — Ordering page (checks activation)
  GET  /activate                   — Serial entry form
  POST /activate                   — Submit serial for activation
  GET  /api/products               — Local product list (JSON)
  POST /api/orders                 — Create a new order (JSON)
  GET  /api/print-receipt/<id>     — Printable receipt HTML (browser window.print)
  POST /api/sync                   — Manually trigger sync cycle
  POST /api/pull-products          — Manually trigger product pull
"""

import logging
import os
import socket
import uuid
from datetime import datetime, timezone, timedelta

import requests
from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from extensions import db
from models import Category, Order, OrderItem, Product
from utils import (
    format_price,
    hash_serial,
    validate_activation_token,
)

desktop_bp = Blueprint("desktop", __name__)
logger = logging.getLogger("routes.desktop")

TOKEN_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "serial.txt")


# ─── Activation helpers ───────────────────────────────────────────────────────

def _read_token() -> str:
    """Read the locally stored activation token, or return ''."""
    try:
        if os.path.isfile(TOKEN_PATH):
            with open(TOKEN_PATH, "r") as f:
                return f.read().strip()
    except OSError:
        pass
    return ""


def _write_token(token: str) -> None:
    """Persist the activation token to disk."""
    with open(TOKEN_PATH, "w") as f:
        f.write(token)


def _clear_token() -> None:
    """Remove the locally stored activation token."""
    if os.path.isfile(TOKEN_PATH):
        os.remove(TOKEN_PATH)


_last_remote_check: datetime | None = None
_RECHECK_INTERVAL = timedelta(minutes=5)  # Only hit the server once every 5 min


def _is_activated() -> bool:
    """
    Return True if the kiosk has a valid activation token.

    Rules (in order):
      1. No token on disk → False.
      2. Token is present → local check passes → True by default.
      3. Every 5 minutes, re-validate with the server.
         - Server says 200 valid=true → True (update cache).
         - Server says 401 or 403    → token revoked → clear it → False.
         - Server is unreachable, sleeping, or returns any other status
           → keep the kiosk running (benefit of the doubt).

    This means the kiosk ONLY gets locked out if:
      a) There is no token at all, OR
      b) The server explicitly revokes the serial (401/403).
    Network errors and Render cold-starts NEVER log the user out.
    """
    global _last_remote_check

    token = _read_token()
    if not token or len(token) < 10:
        return False  # No token stored — must activate

    now = datetime.now(timezone.utc)

    # Skip server check if we checked recently — local token is enough
    if _last_remote_check and (now - _last_remote_check) < _RECHECK_INTERVAL:
        return True

    # Attempt remote re-validation (non-blocking; we always default to True on failure)
    server_url = current_app.config.get("SERVER_URL", "http://localhost:5000")
    try:
        resp = requests.post(
            f"{server_url}/api/validate-serial",
            json={"activation_token": token},
            headers={
                "X-API-KEY": current_app.config.get("SYNC_API_KEY", ""),
                "Content-Type": "application/json",
            },
            timeout=4,
        )
        if resp.status_code == 200 and resp.json().get("valid"):
            _last_remote_check = now   # Update cache on success
            return True
        if resp.status_code in (401, 403):
            # Server explicitly revoked this serial key — lock the kiosk
            logger.warning("Serial key revoked by server (status %d). Clearing token.", resp.status_code)
            _clear_token()
            return False
        # Any other response (500, 502, 503, timeout-after-connect, etc.)
        # → treat as temporary server issue, keep running
        logger.debug("Server returned %d during token re-validation — keeping kiosk running.", resp.status_code)

    except requests.RequestException as exc:
        # Server unreachable (offline, Render sleeping, DNS error, timeout)
        # → keep running, DO NOT log the user out
        logger.debug("Server unreachable during token check (%s) — kiosk stays unlocked.", exc)

    return True  # Token exists, server didn't explicitly reject it → stay unlocked


# ─── Routes ───────────────────────────────────────────────────────────────────

@desktop_bp.route("/")
def index():
    """Main ordering page — redirects to /activate if not activated."""
    if not _is_activated():
        return redirect(url_for("desktop.activate"))

    categories = (
        Category.query.order_by(Category.display_order, Category.name).all()
    )
    return render_template("desktop/index.html", categories=categories)


@desktop_bp.route("/activate", methods=["GET", "POST"])
def activate():
    """Serial entry and activation page."""
    error = None
    success = False

    if request.method == "POST":
        raw_serial = request.form.get("serial", "").strip()
        # Use a stable machine identifier so re-activations always match
        device_id = request.form.get("device_id", "").strip() or socket.gethostname()

        if not raw_serial:
            error = "Please enter a serial key."
        else:
            serial_hash = hash_serial(raw_serial)
            server_url = current_app.config.get("SERVER_URL", "http://localhost:5000")
            try:
                resp = requests.post(
                    f"{server_url}/api/validate-serial",
                    json={
                        "serial_hash": serial_hash,
                        "device_id": device_id,
                    },
                    headers={
                        "X-API-KEY": current_app.config.get("SYNC_API_KEY", ""),
                        "Content-Type": "application/json",
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    token = data.get("activation_token", "")
                    if token and validate_activation_token(token):
                        _write_token(token)
                        success = True
                        return redirect(url_for("desktop.index"))
                    else:
                        error = "Received an invalid activation token from server."
                elif resp.status_code == 401:
                    error = "Invalid or already-used serial key."
                elif resp.status_code == 403:
                    error = "Serial key has expired or been revoked."
                else:
                    error = f"Server error ({resp.status_code}). Try again later."
            except requests.RequestException as exc:
                error = f"Cannot reach the server: {exc}. Check your network connection."

    online = False
    server_url = current_app.config.get("SERVER_URL", "http://localhost:5000")
    try:
        requests.get(server_url, timeout=2)
        online = True
    except requests.RequestException:
        pass

    return render_template(
        "desktop/activate.html", error=error, success=success, online=online
    )


@desktop_bp.route("/api/products")
def api_products():
    """Return the local product catalog as JSON for the kiosk UI."""
    categories = (
        Category.query.order_by(Category.display_order, Category.name).all()
    )
    result = []
    for cat in categories:
        products = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description or "",
                "price_cents": p.price_cents,
                "price_display": format_price(p.price_cents),
                "image": p.image,
            }
            for p in cat.products
            if p.is_active
        ]
        if products:
            result.append(
                {
                    "id": cat.id,
                    "name": cat.name,
                    "products": products,
                }
            )
    return jsonify(result)


@desktop_bp.route("/api/orders", methods=["POST"])
def api_create_order():
    """
    Create a new order locally with status='pending'.

    Expected JSON body:
    {
      "local_id": "uuid-string",   // client-generated for idempotency
      "device_id": "kiosk-1",
      "items": [
        {"product_id": 1, "quantity": 2},
        ...
      ]
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON body."}), 400

    items_data = data.get("items", [])
    if not items_data:
        return jsonify({"error": "Order must have at least one item."}), 400

    local_id = data.get("local_id") or str(uuid.uuid4())
    device_id = data.get("device_id", "unknown")

    # Idempotency check — if order already exists, return it
    existing = Order.query.filter_by(local_id=local_id).first()
    if existing:
        return jsonify({"order_id": existing.id, "local_id": existing.local_id}), 200

    # Validate all products exist and are active
    order_items = []
    total_cents = 0
    for item_data in items_data:
        product_id = item_data.get("product_id")
        quantity = item_data.get("quantity", 1)

        if not isinstance(product_id, int) or not isinstance(quantity, int):
            return jsonify({"error": "product_id and quantity must be integers."}), 400
        if quantity < 1 or quantity > 99:
            return jsonify({"error": f"Invalid quantity: {quantity}."}), 400

        product = db.session.get(Product, product_id)
        if not product or not product.is_active:
            return jsonify({"error": f"Product {product_id} not found or inactive."}), 400

        subtotal = product.price_cents * quantity
        total_cents += subtotal
        order_items.append(
            OrderItem(
                product_id=product.id,
                product_name_snapshot=product.name,
                unit_price_cents_snapshot=product.price_cents,
                quantity=quantity,
                subtotal_cents=subtotal,
            )
        )

    try:
        order = Order(
            local_id=local_id,
            status="pending",
            total_cents=total_cents,
            device_id=device_id,
        )
        db.session.add(order)
        db.session.flush()  # Get order.id before adding items

        for item in order_items:
            item.order_id = order.id
            db.session.add(item)

        db.session.commit()
        logger.info("Created order %s (total=%d cents).", local_id, total_cents)
        return jsonify({"order_id": order.id, "local_id": order.local_id}), 201

    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to create order: %s", exc)
        return jsonify({"error": "Failed to save order. Please try again."}), 500


@desktop_bp.route("/api/print-receipt/<int:order_id>")
def print_receipt(order_id: int):
    """Return a printable HTML receipt for a given order (triggered by browser window.print())."""
    order = db.get_or_404(Order, order_id)
    return render_template("desktop/receipt.html", order=order)


@desktop_bp.route("/api/sync", methods=["POST"])
def api_sync():
    """Manually trigger an order sync cycle."""
    from sync import sync_orders

    try:
        count = sync_orders(current_app._get_current_object())
        return jsonify({"synced": count}), 200
    except Exception as exc:
        logger.error("Manual sync failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@desktop_bp.route("/api/pull-products", methods=["POST"])
def api_pull_products():
    """Manually trigger a product pull from the server."""
    from sync import pull_products

    try:
        count = pull_products(current_app._get_current_object())
        return jsonify({"products_updated": count}), 200
    except Exception as exc:
        logger.error("Manual pull-products failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
