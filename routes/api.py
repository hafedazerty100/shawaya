"""
routes/api.py — Sync API Blueprint (server-side only).

Endpoints:
  POST /api/sync/orders      — Accept a batch of orders from a kiosk
  GET  /api/products         — Return full product catalog for desktop pull
  POST /api/validate-serial  — Validate/activate a serial key; return signed token

All endpoints:
  - Require X-API-KEY header matching SYNC_API_KEY env var
  - Are rate-limited to 30 requests/minute per IP
  - Log auth failures to SyncLog
"""

import hmac
import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from extensions import db, limiter
from models import Category, Order, OrderItem, Product, SerialKey, SyncLog
from utils import (
    api_key_required,
    format_price,
    generate_activation_token,
    hash_serial,
    validate_activation_token,
    extract_token_device_id,
)

api_bp = Blueprint("api", __name__, url_prefix="/api")
logger = logging.getLogger("routes.api")


# ─── Helper ───────────────────────────────────────────────────────────────────

def _log(direction: str, status: str, detail: str, device_id: str = "") -> None:
    log = SyncLog(
        direction=direction, status=status, detail=detail, device_id=device_id
    )
    try:
        db.session.add(log)
        db.session.commit()
    except Exception:
        db.session.rollback()


# ─── Serial validation ────────────────────────────────────────────────────────

@api_bp.route("/validate-serial", methods=["POST"])
@limiter.limit("30 per minute")
@api_key_required
def validate_serial():
    """
    Validates a serial key (or re-validates an existing activation token).

    Accepts either:
      A. { "serial_hash": "<sha256>", "device_id": "<id>" }  → First activation
      B. { "activation_token": "<token>" }                   → Re-validation

    Returns:
      200 { "valid": true, "activation_token": "..." }
      401 { "error": "..." }   Invalid or inactive serial
      403 { "error": "..." }   Expired or revoked
    """
    data = request.get_json(silent=True) or {}
    device_id = data.get("device_id", request.remote_addr)

    # Case B: token re-validation
    activation_token = data.get("activation_token")
    if activation_token:
        if validate_activation_token(activation_token):
            return jsonify({"valid": True, "activation_token": activation_token}), 200
        _log("push", "error", f"Invalid re-validation token from {request.remote_addr}", device_id)
        return jsonify({"error": "Invalid activation token."}), 401

    # Case A: first activation
    serial_hash = data.get("serial_hash", "").strip()
    if not serial_hash or len(serial_hash) != 64:
        return jsonify({"error": "Missing or invalid serial_hash."}), 400

    key = SerialKey.query.filter_by(serial_hash=serial_hash).first()

    if key is None:
        _log("push", "error", f"Unknown serial from {request.remote_addr}", device_id)
        logger.warning("Unknown serial attempt from IP %s", request.remote_addr)
        return jsonify({"error": "Invalid serial key."}), 401

    if key.expires_at:
        expires_at_naive = key.expires_at.replace(tzinfo=None)
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        if expires_at_naive < now_naive:
            _log("push", "error", f"Expired serial from {request.remote_addr}", str(key.id))
            return jsonify({"error": "Serial key has expired."}), 403

    if key.is_active and key.device_id and key.device_id != device_id:
        # Already activated on a different device
        _log(
            "push", "error",
            f"Serial already active on device '{key.device_id}', attempted by '{device_id}'",
            device_id,
        )
        return jsonify({"error": "Serial key already activated on another device."}), 401

    # Activate / re-activate
    try:
        key.is_active = True
        key.activated_at = datetime.now(timezone.utc)
        key.device_id = device_id
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to activate serial: %s", exc)
        return jsonify({"error": "Server error during activation."}), 500

    token = generate_activation_token(serial_hash, device_id)
    _log("push", "success", f"Serial activated for device '{device_id}'", device_id)
    logger.info("Serial activated: device=%s", device_id)
    return jsonify({"valid": True, "activation_token": token}), 200


# ─── Product catalog pull ─────────────────────────────────────────────────────

@api_bp.route("/products", methods=["GET"])
@limiter.limit("30 per minute")
@api_key_required
def products():
    """Return the full product catalog for desktop pull."""
    categories = Category.query.order_by(Category.display_order, Category.name).all()
    cat_list = [
        {"id": c.id, "name": c.name, "display_order": c.display_order}
        for c in categories
    ]
    prod_list = []
    for cat in categories:
        for p in cat.products:
            if p.is_active:
                prod_list.append(
                    {
                        "id": p.id,
                        "category_id": p.category_id,
                        "name": p.name,
                        "description": p.description or "",
                        "price_cents": p.price_cents,
                        "image": p.image,
                        "is_active": p.is_active,
                    }
                )
    return jsonify({"categories": cat_list, "products": prod_list}), 200


# ─── Order sync (push from desktop) ──────────────────────────────────────────

@api_bp.route("/sync/orders", methods=["POST"])
@limiter.limit("30 per minute")
@api_key_required
def sync_orders():
    """
    Accept a batch of orders from a kiosk.

    Expected JSON: { "orders": [ { ... }, ... ] }

    Returns per-order results keyed by local_id:
    { "results": { "<local_id>": { "status": "ok" | "error", "error": "..." } } }
    """
    data = request.get_json(silent=True) or {}
    orders_data = data.get("orders", [])

    if not isinstance(orders_data, list):
        return jsonify({"error": "Expected 'orders' to be a list."}), 400

    results = {}

    for order_data in orders_data:
        local_id = order_data.get("local_id", "")
        device_id = order_data.get("device_id", "unknown")

        # Validate required fields
        if not local_id:
            continue  # Skip malformed entries

        try:
            total_cents = int(order_data.get("total_cents", 0))
            items_data = order_data.get("items", [])
            if not items_data:
                results[local_id] = {"status": "error", "error": "No items in order."}
                continue

            # Idempotency: skip if already received
            existing = Order.query.filter_by(local_id=local_id).first()
            if existing:
                results[local_id] = {"status": "ok"}
                continue

            # Parse created_at
            created_at_raw = order_data.get("created_at")
            try:
                created_at = datetime.fromisoformat(created_at_raw) if created_at_raw else datetime.now(timezone.utc)
            except (ValueError, TypeError):
                created_at = datetime.now(timezone.utc)

            order = Order(
                local_id=local_id,
                status="synced",
                total_cents=total_cents,
                created_at=created_at,
                synced_at=datetime.now(timezone.utc),
                device_id=device_id,
            )
            db.session.add(order)
            db.session.flush()

            for item_data in items_data:
                quantity = int(item_data.get("quantity", 1))
                unit_price = int(item_data.get("unit_price_cents_snapshot", 0))
                item = OrderItem(
                    order_id=order.id,
                    product_id=item_data.get("product_id"),
                    product_name_snapshot=str(item_data.get("product_name_snapshot", "")),
                    unit_price_cents_snapshot=unit_price,
                    quantity=quantity,
                    subtotal_cents=int(item_data.get("subtotal_cents", unit_price * quantity)),
                )
                db.session.add(item)

            db.session.commit()
            results[local_id] = {"status": "ok"}
            logger.info("Received order %s from device %s.", local_id, device_id)

        except Exception as exc:
            db.session.rollback()
            logger.error("Failed to process order %s: %s", local_id, exc)
            results[local_id] = {"status": "error", "error": str(exc)}

    _log(
        "push",
        "success" if any(r["status"] == "ok" for r in results.values()) else "error",
        f"Processed {len(results)} orders.",
        data.get("orders", [{}])[0].get("device_id", "") if orders_data else "",
    )

    return jsonify({"results": results}), 200
