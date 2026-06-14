"""
routes/api.py — Sync API Blueprint (server-side only).

Endpoints:
  POST /api/sync/orders      — Accept a batch of orders from a kiosk
  GET  /api/products         — Return full product catalog for desktop pull
  POST /api/validate-serial  — Validate a serial key against the Neon DB

All endpoints require X-API-KEY header and are rate-limited.
"""

import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from extensions import db, limiter
from models import Category, Order, OrderItem, Product, SerialKey, SyncLog
from utils import api_key_required, format_price

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
    Simple serial validation: check if the serial hash exists in the DB.

    Accepts: { "serial_hash": "<sha256>", "device_id": "<optional>" }

    Returns:
      200 { "valid": true }   — Serial found, not expired, not revoked
      401 { "error": "..." }  — Serial not found
      403 { "error": "..." }  — Expired or revoked by admin
    """
    data = request.get_json(silent=True) or {}
    serial_hash = data.get("serial_hash", "").strip()
    device_id = data.get("device_id", request.remote_addr)

    if not serial_hash or len(serial_hash) != 64:
        return jsonify({"error": "Missing or invalid serial_hash."}), 400

    key = SerialKey.query.filter_by(serial_hash=serial_hash).first()

    if key is None:
        logger.warning("Unknown serial attempt from IP %s", request.remote_addr)
        return jsonify({"error": "الرمز التسلسلي غير صحيح."}), 401

    # Check expiry
    if key.expires_at:
        now = datetime.now(timezone.utc)
        expires = key.expires_at if key.expires_at.tzinfo else key.expires_at.replace(tzinfo=timezone.utc)
        if expires < now:
            return jsonify({"error": "الرمز التسلسلي منتهي الصلاحية."}), 403

    # Revoked: was activated before but admin set is_active=False
    if not key.is_active and key.activated_at is not None:
        return jsonify({"error": "تم إلغاء الرمز التسلسلي من قِبَل المشرف."}), 403

    # First use or re-activation: mark as active
    try:
        key.is_active = True
        key.activated_at = datetime.now(timezone.utc)
        if device_id:
            key.device_id = device_id
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to activate serial: %s", exc)
        return jsonify({"error": "خطأ في الخادم."}), 500

    _log("push", "success", f"Serial validated for device '{device_id}'", device_id)
    return jsonify({"valid": True}), 200


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


# ─── Order sync (pull to desktop) ────────────────────────────────────────────

@api_bp.route("/sync/pull_orders", methods=["GET"])
@limiter.limit("30 per minute")
@api_key_required
def pull_orders():
    """
    Return orders created after a specific date (for two-way sync).
    
    Query params:
      after_date: ISO-8601 string or empty (if empty, returns last 30 days or similar, 
                  but to prevent huge payloads, we rely on the client sending this).
    """
    after_date_str = request.args.get("after_date", "").strip()
    
    query = Order.query
    
    if after_date_str:
        try:
            after_date = datetime.fromisoformat(after_date_str)
            # Ensure it is UTC aware if naive
            if after_date.tzinfo is None:
                after_date = after_date.replace(tzinfo=timezone.utc)
            query = query.filter(Order.created_at > after_date)
        except ValueError:
            pass
            
    # Order by created_at ascending so client gets oldest new orders first
    orders = query.order_by(Order.created_at.asc()).all()
    
    payload = []
    for order in orders:
        payload.append({
            "local_id": order.local_id,
            "status": order.status,
            "total_cents": order.total_cents,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "synced_at": order.synced_at.isoformat() if order.synced_at else None,
            "device_id": order.device_id,
            "items": [
                {
                    "product_id": item.product_id,
                    "product_name_snapshot": item.product_name_snapshot,
                    "unit_price_cents_snapshot": item.unit_price_cents_snapshot,
                    "quantity": item.quantity,
                    "subtotal_cents": item.subtotal_cents,
                }
                for item in order.items
            ]
        })
        
    return jsonify({"orders": payload}), 200


# ─── Order deletions sync ────────────────────────────────────────────────────

@api_bp.route("/sync/active_order_ids", methods=["GET"])
@limiter.limit("30 per minute")
@api_key_required
def active_order_ids():
    """
    Return a list of all active order local_ids on the server.
    The desktop will use this to delete local orders that were deleted on the server.
    """
    # Fetch only local_id to keep payload small
    orders = db.session.query(Order.local_id).all()
    local_ids = [o[0] for o in orders if o[0]]
    
    return jsonify({"local_ids": local_ids}), 200
