"""
sync.py — Background sync logic for the desktop kiosk.

Responsibilities:
  1. sync_orders(app)   — Push pending orders to the server.
  2. pull_products(app) — Pull the latest product catalog from the server.
  3. start_sync_thread(app) — Daemon thread that runs both functions in a loop.

Design principles:
  - Every function runs inside app.app_context().
  - A single failed cycle NEVER kills the thread.
  - Exponential backoff on repeated failures (capped at 5 minutes).
  - Idempotency: re-running after a partial failure never duplicates data.
"""

import io
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import requests
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger("sync")


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _get_headers(app) -> dict:
    """Build auth headers required by the server sync API."""
    token_path = os.path.join(os.path.dirname(__file__), "serial.txt")
    activation_token = ""
    if os.path.isfile(token_path):
        with open(token_path, "r") as f:
            activation_token = f.read().strip()
    return {
        "X-API-KEY": app.config.get("SYNC_API_KEY", ""),
        "X-Activation-Token": activation_token,
        "Content-Type": "application/json",
    }


def _log_sync(db, direction: str, status: str, detail: str, device_id: str = ""):
    """Write a SyncLog record. Rolls back on failure (best-effort)."""
    from models import SyncLog

    log = SyncLog(
        direction=direction,
        status=status,
        detail=detail,
        device_id=device_id,
    )
    try:
        db.session.add(log)
        db.session.commit()
    except Exception as exc:
        logger.error("Failed to write SyncLog: %s", exc)
        db.session.rollback()


# ─── sync_orders ──────────────────────────────────────────────────────────────

def sync_orders(app) -> int:
    """
    Push all locally pending orders to the server.

    Returns the number of successfully synced orders.
    """
    from extensions import db
    from models import Order, SyncLog
    from datetime import timezone

    with app.app_context():
        pending = Order.query.filter_by(status="pending").all()
        if not pending:
            return 0

        server_url = app.config.get("SERVER_URL", "http://localhost:5000")
        endpoint = f"{server_url}/api/sync/orders"
        headers = _get_headers(app)

        # Serialise orders for the payload
        payload = []
        for order in pending:
            payload.append(
                {
                    "local_id": order.local_id,
                    "total_cents": order.total_cents,
                    "created_at": order.created_at.isoformat(),
                    "device_id": order.device_id or "",
                    "items": [
                        {
                            "product_id": item.product_id,
                            "product_name_snapshot": item.product_name_snapshot,
                            "unit_price_cents_snapshot": item.unit_price_cents_snapshot,
                            "quantity": item.quantity,
                            "subtotal_cents": item.subtotal_cents,
                        }
                        for item in order.items
                    ],
                }
            )

        device_id = pending[0].device_id if pending else ""

        try:
            resp = requests.post(
                endpoint,
                json={"orders": payload},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            results = resp.json().get("results", {})

            synced_count = 0
            now = datetime.now(timezone.utc)
            for order in pending:
                result = results.get(order.local_id, {})
                if result.get("status") == "ok":
                    order.status = "synced"
                    order.synced_at = now
                    synced_count += 1
                else:
                    order.status = "failed"
                    logger.warning(
                        "Order %s rejected by server: %s",
                        order.local_id,
                        result.get("error", "unknown"),
                    )
            try:
                db.session.commit()
            except Exception as exc:
                logger.error("DB commit after sync failed: %s", exc)
                db.session.rollback()

            _log_sync(
                db,
                "push",
                "success",
                f"Synced {synced_count}/{len(pending)} orders.",
                device_id,
            )
            logger.info("sync_orders: %d/%d orders synced.", synced_count, len(pending))
            return synced_count

        except requests.RequestException as exc:
            _log_sync(db, "push", "error", f"Network error: {exc}", device_id)
            logger.error("sync_orders network error: %s", exc)
            return 0


# ─── pull_products ────────────────────────────────────────────────────────────

def pull_products(app) -> int:
    """
    Pull the latest product catalog (products + categories) from the server.

    Upserts using the server-assigned product ID as the stable key.
    Downloads new/changed product images and validates them with Pillow.

    Returns the number of products updated/created.
    """
    from extensions import db
    from models import Category, Product

    with app.app_context():
        server_url = app.config.get("SERVER_URL", "http://localhost:5000")
        endpoint = f"{server_url}/api/products"
        headers = _get_headers(app)
        upload_folder = app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_folder, exist_ok=True)

        try:
            resp = requests.get(endpoint, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            _log_sync(db, "pull", "error", f"Network error: {exc}", "desktop")
            logger.error("pull_products network error: %s", exc)
            return 0

        categories = data.get("categories", [])
        products = data.get("products", [])

        try:
            # ── Upsert categories ────────────────────────────────────────────
            server_cat_ids = set()
            for cat_data in categories:
                cat = Category.query.get(cat_data["id"])
                if cat is None:
                    cat = Category(id=cat_data["id"])
                    db.session.add(cat)
                cat.name = cat_data["name"]
                cat.display_order = cat_data.get("display_order", 0)
                server_cat_ids.add(cat_data["id"])

            # Delete categories no longer on server
            for local_cat in Category.query.all():
                if local_cat.id not in server_cat_ids:
                    db.session.delete(local_cat)
                    logger.info("Removed deleted category id=%s from local DB.", local_cat.id)

            # ── Upsert products ──────────────────────────────────────────────
            server_product_ids = set()
            count = 0
            for prod_data in products:
                prod = Product.query.get(prod_data["id"])
                if prod is None:
                    prod = Product(id=prod_data["id"])
                    db.session.add(prod)

                prod.category_id = prod_data["category_id"]
                prod.name = prod_data["name"]
                prod.description = prod_data.get("description", "")
                prod.price_cents = prod_data["price_cents"]
                prod.is_active = prod_data.get("is_active", True)

                # Download image only if it has changed or is new
                remote_image = prod_data.get("image")
                if remote_image and prod.image != remote_image:
                    image_url = f"{server_url}/static/uploads/products/{remote_image}"
                    try:
                        img_resp = requests.get(image_url, timeout=15)
                        img_resp.raise_for_status()
                        # Validate with Pillow before saving
                        img = Image.open(io.BytesIO(img_resp.content))
                        img.verify()
                        img = Image.open(io.BytesIO(img_resp.content))
                        dest = os.path.join(upload_folder, remote_image)
                        img.save(dest)
                        prod.image = remote_image
                        logger.info("Downloaded product image: %s", remote_image)
                    except (UnidentifiedImageError, Exception) as img_exc:
                        logger.warning(
                            "Could not download/validate image %s: %s",
                            remote_image,
                            img_exc,
                        )

                server_product_ids.add(prod_data["id"])
                count += 1

            # ── Delete products removed on server ────────────────────────────
            for local_prod in Product.query.all():
                if local_prod.id not in server_product_ids:
                    logger.info(
                        "Removing deleted product '%s' (id=%s) from local DB.",
                        local_prod.name,
                        local_prod.id,
                    )
                    db.session.delete(local_prod)

            db.session.commit()
            _log_sync(db, "pull", "success", f"Pulled {count} products.", "desktop")
            logger.info("pull_products: %d products pulled.", count)
            return count

        except Exception as exc:
            db.session.rollback()
            _log_sync(db, "pull", "error", str(exc), "desktop")
            logger.error("pull_products DB error: %s", exc)
            return 0


# ─── pull_orders_from_server ──────────────────────────────────────────────────

def pull_orders_from_server(app) -> int:
    """
    Pull the latest orders from the server that are newer than our most recent order.
    Returns the number of new orders saved locally.
    """
    from extensions import db
    from models import Order, OrderItem
    from sqlalchemy import func
    
    with app.app_context():
        # Find the latest created_at date we have locally
        latest_order = db.session.query(func.max(Order.created_at)).scalar()
        after_date = latest_order.isoformat() if latest_order else ""
        
        server_url = app.config.get("SERVER_URL", "http://localhost:5000")
        endpoint = f"{server_url}/api/sync/pull_orders"
        if after_date:
            endpoint += f"?after_date={after_date}"
            
        headers = _get_headers(app)
        
        try:
            resp = requests.get(endpoint, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            _log_sync(db, "pull_orders", "error", f"Network error: {exc}", "desktop")
            logger.error("pull_orders_from_server network error: %s", exc)
            return 0
            
        orders_data = data.get("orders", [])
        if not orders_data:
            return 0
            
        count = 0
        try:
            for order_data in orders_data:
                local_id = order_data["local_id"]
                # Skip if we already have it
                if Order.query.filter_by(local_id=local_id).first():
                    continue
                    
                created_at_raw = order_data.get("created_at")
                synced_at_raw = order_data.get("synced_at")
                
                try:
                    created_at = datetime.fromisoformat(created_at_raw) if created_at_raw else datetime.now(timezone.utc)
                except (ValueError, TypeError):
                    created_at = datetime.now(timezone.utc)
                    
                try:
                    synced_at = datetime.fromisoformat(synced_at_raw) if synced_at_raw else None
                except (ValueError, TypeError):
                    synced_at = None

                new_order = Order(
                    local_id=local_id,
                    status=order_data.get("status", "synced"),
                    total_cents=order_data.get("total_cents", 0),
                    device_id=order_data.get("device_id", "unknown"),
                    created_at=created_at,
                    synced_at=synced_at
                )
                db.session.add(new_order)
                db.session.flush() # Get ID
                
                for item_data in order_data.get("items", []):
                    new_item = OrderItem(
                        order_id=new_order.id,
                        product_id=item_data.get("product_id"),
                        product_name_snapshot=item_data.get("product_name_snapshot", ""),
                        unit_price_cents_snapshot=item_data.get("unit_price_cents_snapshot", 0),
                        quantity=item_data.get("quantity", 1),
                        subtotal_cents=item_data.get("subtotal_cents", 0)
                    )
                    db.session.add(new_item)
                
                count += 1
                
            db.session.commit()
            if count > 0:
                _log_sync(db, "pull_orders", "success", f"Pulled {count} new orders.", "desktop")
                logger.info("pull_orders_from_server: %d new orders pulled.", count)
            return count
            
        except Exception as exc:
            db.session.rollback()
            _log_sync(db, "pull_orders", "error", str(exc), "desktop")
            logger.error("pull_orders_from_server DB error: %s", exc)
            return 0


# ─── Background sync thread ───────────────────────────────────────────────────

def start_sync_thread(app):
    """
    Start a daemon background thread that repeatedly syncs orders and products.

    Uses exponential backoff on consecutive failures (max 5 minutes).
    One failed cycle does NOT kill the thread.
    """
    sync_interval = app.config.get("SYNC_INTERVAL", 30)

    def _sync_loop():
        consecutive_failures = 0
        max_backoff = 300  # 5 minutes in seconds

        while True:
            try:
                synced = sync_orders(app)
                pulled = pull_products(app)
                pulled_orders = pull_orders_from_server(app)
                logger.debug(
                    "Sync cycle: %d orders pushed, %d products pulled, %d orders pulled.",
                    synced,
                    pulled,
                    pulled_orders
                )
                consecutive_failures = 0
                sleep_time = sync_interval
            except Exception as exc:
                consecutive_failures += 1
                # Exponential backoff: 30s, 60s, 120s, 240s, 300s, 300s, ...
                sleep_time = min(sync_interval * (2 ** consecutive_failures), max_backoff)
                logger.error(
                    "Sync cycle error (#%d consecutive): %s. "
                    "Sleeping %ds before retry.",
                    consecutive_failures,
                    exc,
                    sleep_time,
                )

            time.sleep(sleep_time)

    thread = threading.Thread(target=_sync_loop, daemon=True, name="SyncThread")
    thread.start()
    logger.info(
        "Background sync thread started (interval=%ds).", sync_interval
    )
    return thread
