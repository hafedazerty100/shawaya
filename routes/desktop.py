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

Auth flow (simple):
  1. User enters raw serial → desktop hashes it (SHA-256) → sends hash to server.
  2. Server checks hash in Neon DB → 200 OK if valid.
  3. Desktop stores the hash in serial.txt.
  4. On each page load: if hash in file → kiosk is unlocked (checked every 5 min).
  5. Only locks out if server explicitly says 401/403 (revoked/not found).
"""

import logging
import os
import socket
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
from utils import format_price, hash_serial

desktop_bp = Blueprint("desktop", __name__)
logger = logging.getLogger("routes.desktop")

# Local file that stores the SHA-256 hash of the activated serial
SERIAL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "serial.txt")


# ─── Activation helpers ───────────────────────────────────────────────────────

def _read_serial_hash() -> str:
    """Read the stored serial hash from disk, or return ''."""
    try:
        if os.path.isfile(SERIAL_PATH):
            return open(SERIAL_PATH).read().strip()
    except OSError:
        pass
    return ""


def _save_serial_hash(serial_hash: str) -> None:
    """Persist the serial hash to disk."""
    with open(SERIAL_PATH, "w") as f:
        f.write(serial_hash)


def _clear_serial_hash() -> None:
    """Delete the stored serial hash (kiosk needs to re-activate)."""
    if os.path.isfile(SERIAL_PATH):
        os.remove(SERIAL_PATH)


_last_remote_check: datetime | None = None
_RECHECK_INTERVAL = timedelta(minutes=5)


def _is_activated() -> bool:
    """
    Return True if the kiosk has a valid serial hash stored locally.

    Rules:
      1. No serial hash on disk → show activation form.
      2. Hash exists → unlocked by default.
      3. Every 5 min, re-check with the server:
         - 200 OK → stay unlocked, update cache timestamp.
         - 401/403 → server revoked/deleted the serial → clear hash → lock.
         - Network error / server sleeping → stay unlocked (benefit of doubt).
    """
    global _last_remote_check

    serial_hash = _read_serial_hash()
    if not serial_hash or len(serial_hash) != 64:
        return False  # No valid serial stored — must activate

    now = datetime.now(timezone.utc)

    # Within 5-min cache window — no need to hit the server
    if _last_remote_check and (now - _last_remote_check) < _RECHECK_INTERVAL:
        return True

    # Periodic re-validation against Neon DB
    server_url = current_app.config.get("SERVER_URL", "http://localhost:5000")
    try:
        resp = requests.post(
            f"{server_url}/api/validate-serial",
            json={
                "serial_hash": serial_hash,
                "device_id": socket.gethostname(),
            },
            headers={
                "X-API-KEY": current_app.config.get("SYNC_API_KEY", ""),
                "Content-Type": "application/json",
            },
            timeout=4,
        )
        if resp.status_code == 200 and resp.json().get("valid"):
            _last_remote_check = now
            return True
        if resp.status_code in (401, 403):
            logger.warning(
                "Serial rejected by server (%d) — clearing local hash.",
                resp.status_code,
            )
            _clear_serial_hash()
            return False
        # 500, 502, 503, etc. → treat as temporary → stay unlocked
        logger.debug("Server returned %d during serial check — keeping kiosk running.", resp.status_code)

    except requests.RequestException as exc:
        # Offline / Render sleeping → stay unlocked
        logger.debug("Server unreachable during serial check — kiosk stays unlocked. (%s)", exc)

    return True  # Serial on disk, server didn't explicitly reject it


# ─── Routes ───────────────────────────────────────────────────────────────────

@desktop_bp.route("/")
def index():
    """Main ordering page — redirects to /activate if not activated."""
    if not _is_activated():
        return redirect(url_for("desktop.activate"))
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    return render_template("desktop/index.html", categories=categories)


@desktop_bp.route("/activate", methods=["GET", "POST"])
def activate():
    """Serial key entry form."""
    error = None

    if request.method == "POST":
        raw_serial = request.form.get("serial", "").strip()
        if not raw_serial:
            error = "أدخل الرمز التسلسلي."
        else:
            serial_hash = hash_serial(raw_serial)
            server_url = current_app.config.get("SERVER_URL", "http://localhost:5000")
            try:
                resp = requests.post(
                    f"{server_url}/api/validate-serial",
                    json={
                        "serial_hash": serial_hash,
                        "device_id": socket.gethostname(),
                    },
                    headers={
                        "X-API-KEY": current_app.config.get("SYNC_API_KEY", ""),
                        "Content-Type": "application/json",
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    _save_serial_hash(serial_hash)
                    return redirect(url_for("desktop.index"))
                # Use the Arabic error message from the server if available
                server_error = resp.json().get("error", "")
                if resp.status_code == 401:
                    error = server_error or "الرمز التسلسلي غير صحيح."
                elif resp.status_code == 403:
                    error = server_error or "الرمز التسلسلي منتهي الصلاحية أو تم إلغاؤه."
                else:
                    error = f"خطأ في الخادم ({resp.status_code}). حاول مجدداً."
            except requests.RequestException:
                error = "لا يمكن الوصول إلى الخادم. تحقق من اتصالك بالإنترنت."

    # Check server connectivity for UI feedback
    online = False
    server_url = current_app.config.get("SERVER_URL", "http://localhost:5000")
    try:
        requests.get(server_url, timeout=2)
        online = True
    except requests.RequestException:
        pass

    return render_template("desktop/activate.html", error=error, online=online)


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
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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
            status="draft",
            total_cents=total_cents,
            device_id=device_id,
        )
        db.session.add(order)
        db.session.flush()  # Get order.id before adding items

        for item in order_items:
            item.order_id = order.id
            db.session.add(item)

        db.session.commit()
        logger.info("Created draft order %s (total=%d cents).", local_id, total_cents)
        return jsonify({"order_id": order.id, "local_id": order.local_id}), 201

    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to create order: %s", exc)
        return jsonify({"error": "Failed to save order. Please try again."}), 500


@desktop_bp.route("/api/orders/<int:order_id>/confirm", methods=["POST"])
def confirm_order(order_id: int):
    """Update order status from 'draft' to 'pending' after successful printing."""
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"error": "Order not found."}), 404
    if order.status == "draft":
        order.status = "pending"
        db.session.commit()
        logger.info("Order %d confirmed and status set to 'pending'.", order_id)
        return jsonify({"success": True}), 200
    return jsonify({"success": True, "message": "Already confirmed"}), 200


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


def _get_sync_headers():
    token_path = SERIAL_PATH
    activation_token = ""
    try:
        if os.path.isfile(token_path):
            with open(token_path, "r") as f:
                activation_token = f.read().strip()
    except Exception:
        pass
    return {
        "X-API-KEY": current_app.config.get("SYNC_API_KEY", ""),
        "X-Activation-Token": activation_token,
        "Content-Type": "application/json",
    }


def _get_remote_revenue(start_dt, end_dt):
    """Query the remote Neon PostgreSQL database directly for revenue totals when online."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return None
        
    if db_url.startswith("postgres://"):
        db_url = "postgresql+pg8000://" + db_url[len("postgres://"):]
    elif db_url.startswith("postgresql://"):
        db_url = "postgresql+pg8000://" + db_url[len("postgresql://"):]
        
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(db_url, connect_args={"timeout": 5})
        with engine.connect() as conn:
            query = text(
                "SELECT COALESCE(SUM(total_cents), 0) FROM orders "
                "WHERE status != 'draft' AND created_at >= :start AND created_at <= :end"
            )
            res = conn.execute(query, {"start": start_dt, "end": end_dt}).scalar()
            return int(res or 0)
    except Exception as exc:
        logger.warning("Failed to query remote database directly for revenue: %s", exc)
        return None


@desktop_bp.route("/api/revenue")
def api_revenue():
    """Return total revenue for a specific date or date range."""
    date_str = request.args.get("date", "").strip()
    start_str = request.args.get("start_date", "").strip()
    end_str = request.args.get("end_date", "").strip()
    
    try:
        if start_str and end_str:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        elif date_str:
            start_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            end_date = start_date
        else:
            # Default to today
            start_date = datetime.now(timezone.utc).date()
            end_date = start_date
            
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400
        
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)
    
    # Try fetching from PostgreSQL database directly first (if online)
    server_revenue_cents = 0
    server_success = False
    
    remote_revenue = _get_remote_revenue(start_dt, end_dt)
    if remote_revenue is not None:
        server_revenue_cents = remote_revenue
        server_success = True
    else:
        # Fallback to server API endpoint
        server_url = current_app.config.get("SERVER_URL", "")
        if server_url:
            endpoint = f"{server_url}/api/sync/revenue"
            headers = _get_sync_headers()
            params = {}
            params["start_date"] = start_date.strftime("%Y-%m-%d")
            params["end_date"] = end_date.strftime("%Y-%m-%d")
            try:
                resp = requests.get(endpoint, headers=headers, params=params, timeout=5)
                if resp.status_code == 200:
                    server_revenue_cents = resp.json().get("total_cents", 0)
                    server_success = True
                else:
                    logger.warning("Server returned %d for sync revenue: %s", resp.status_code, resp.text)
            except Exception as exc:
                logger.warning("Failed to fetch revenue from server: %s", exc)
            
    if server_success:
        # Include any local orders that are NOT yet synced (i.e. status is draft, pending, or failed)
        local_orders = Order.query.filter(
            Order.status != "synced",
            Order.created_at >= start_dt,
            Order.created_at <= end_dt
        ).all()
        total_cents = server_revenue_cents + sum(o.total_cents for o in local_orders)
    else:
        # Offline fallback: calculate purely from local DB (excluding draft)
        local_orders = Order.query.filter(
            Order.status != "draft",
            Order.created_at >= start_dt,
            Order.created_at <= end_dt
        ).all()
        total_cents = sum(o.total_cents for o in local_orders)
        
    return jsonify({
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
        "total_cents": total_cents,
        "total_display": format_price(total_cents)
    }), 200

