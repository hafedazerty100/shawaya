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
            resp = requests.get(endpoint, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            _log_sync(db, "pull", "error", f"Network error: {exc}", "desktop")
            logger.error("pull_products network error: %s", exc)
            raise exc

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

                # Download image only if it has changed or is new, or if DB bytes are missing (self-healing)
                remote_image = prod_data.get("image")
                if remote_image and (prod.image != remote_image or not getattr(prod, "image_data", None)):
                    image_url = f"{server_url}/static/uploads/products/{remote_image}"
                    try:
                        img_resp = requests.get(image_url, timeout=5)
                        img_resp.raise_for_status()
                        # Save raw bytes directly to avoid RGBA->JPEG conversion errors
                        dest = os.path.join(upload_folder, remote_image)
                        with open(dest, "wb") as f:
                            f.write(img_resp.content)
                        prod.image = remote_image
                        prod.image_data = img_resp.content
                        prod.image_mime = img_resp.headers.get("Content-Type", "image/jpeg")
                        logger.info("Downloaded product image and saved to DB: %s", remote_image)
                    except Exception as img_exc:
                        logger.warning(
                            "Could not download/validate image %s: %s",
                            remote_image,
                            img_exc,
                        )
                        # Avoid retrying permanently missing images (4xx errors)
                        if isinstance(img_exc, requests.exceptions.HTTPError) and img_exc.response is not None and img_exc.response.status_code < 500:
                            prod.image = remote_image
                            prod.image_data = b"FAILED"
                            prod.image_mime = "image/jpeg"

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
            resp = requests.get(endpoint, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            _log_sync(db, "pull_orders", "error", f"Network error: {exc}", "desktop")
            logger.error("pull_orders_from_server network error: %s", exc)
            raise exc
            
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


# ─── sync_deleted_orders ──────────────────────────────────────────────────────

def sync_deleted_orders(app) -> int:
    """
    Fetch all active order IDs from the server.
    Delete any local order (that isn't pending) if it was deleted on the server.
    """
    from extensions import db
    from models import Order
    
    with app.app_context():
        server_url = app.config.get("SERVER_URL", "http://localhost:5000")
        endpoint = f"{server_url}/api/sync/active_order_ids"
        headers = _get_headers(app)
        
        try:
            resp = requests.get(endpoint, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.error("sync_deleted_orders network error: %s", exc)
            raise exc
            
        server_ids = set(data.get("local_ids", []))
        if not server_ids:
            return 0 # Safety check: if server says 0 active orders, maybe it's an error. 
                     # Or maybe all are deleted. But to be safe, we just skip.
        
        count = 0
        try:
            # Get all non-pending local orders
            local_synced_orders = Order.query.filter(Order.status != "pending").all()
            for order in local_synced_orders:
                if order.local_id and order.local_id not in server_ids:
                    logger.info("Order %s was deleted on server. Deleting locally.", order.local_id)
                    db.session.delete(order)
                    count += 1
            
            if count > 0:
                db.session.commit()
                _log_sync(db, "sync_deletions", "success", f"Deleted {count} removed orders.", "desktop")
                
            return count
            
        except Exception as exc:
            db.session.rollback()
            logger.error("sync_deleted_orders DB error: %s", exc)
            return 0


def check_and_generate_daily_archives(app):
    """
    Finds all dates in local history that have orders, checks if an archive CSV
    exists in the 'archive' folder, and generates it if missing.
    Excludes the current ongoing day to only archive completed days.
    """
    from extensions import db
    from models import Order
    import os
    import csv
    from datetime import datetime

    with app.app_context():
        try:
            # We only run this in desktop mode
            if app.config.get("MODE") != "desktop":
                return
                
            # Ensure 'archive' directory exists
            base_dir = app.root_path
            archive_dir = os.path.join(base_dir, "archive")
            os.makedirs(archive_dir, exist_ok=True)
            
            # Get system local timezone
            local_tz = datetime.now().astimezone().tzinfo
            current_local_date = datetime.now(local_tz).date()
            
            # Fetch all non-draft orders
            orders = Order.query.filter(Order.status != "draft").all()
            if not orders:
                logger.debug("No orders found to archive.")
                return
                
            # Group orders by their local date
            orders_by_date = {}
            from datetime import timezone
            for o in orders:
                o_dt = o.created_at
                if o_dt.tzinfo is None:
                    o_dt = o_dt.replace(tzinfo=timezone.utc)
                o_local_dt = o_dt.astimezone(local_tz)
                o_date = o_local_dt.date()
                
                # Exclude the current day as it is still in progress
                if o_date >= current_local_date:
                    continue
                    
                orders_by_date.setdefault(o_date, []).append(o)
                
            # For each past date with orders, check if CSV exists. If not, generate it.
            for o_date, day_orders in orders_by_date.items():
                date_str = o_date.strftime("%Y-%m-%d")
                csv_filename = os.path.join(archive_dir, f"{date_str}.csv")
                
                if os.path.exists(csv_filename):
                    continue
                    
                # Generate the daily revenue CSV
                logger.info("Generating daily revenue archive CSV for %s", date_str)
                with open(csv_filename, "w", newline="", encoding="utf-8-sig") as csvfile:
                    writer = csv.writer(csvfile)
                    # Header
                    writer.writerow([
                        "Order ID",
                        "Local ID",
                        "Time (Local)",
                        "Device ID",
                        "Status",
                        "Product Name",
                        "Quantity",
                        "Unit Price (DA)",
                        "Subtotal (DA)",
                        "Order Total (DA)"
                    ])
                    
                    total_revenue_cents = 0
                    total_orders_count = len(day_orders)
                    
                    # Sort orders by time
                    day_orders.sort(key=lambda x: x.created_at)
                    
                    for order in day_orders:
                        order_time_str = order.created_at.astimezone(local_tz).strftime("%H:%M:%S")
                        order_total_da = f"{order.total_cents / 100:.2f}"
                        total_revenue_cents += order.total_cents
                        
                        # Write each line item
                        for idx, item in enumerate(order.items):
                            unit_price_da = f"{item.unit_price_cents_snapshot / 100:.2f}"
                            subtotal_da = f"{item.subtotal_cents / 100:.2f}"
                            
                            writer.writerow([
                                order.id,
                                order.local_id,
                                order_time_str,
                                order.device_id or "",
                                order.status,
                                item.product_name_snapshot,
                                item.quantity,
                                unit_price_da,
                                subtotal_da,
                                # Write order total only on the first item line of this order
                                order_total_da if idx == 0 else ""
                            ])
                            
                    # Write Summary Footer
                    writer.writerow([])
                    writer.writerow(["", "", "", "", "", "", "", "", "Total Revenue", f"{total_revenue_cents / 100:.2f} DA"])
                    writer.writerow(["", "", "", "", "", "", "", "", "Total Orders", total_orders_count])
                    
            logger.info("Completed check for daily revenue archives.")
        except Exception as exc:
            logger.exception("Failed checking or writing daily revenue archives: %s", exc)


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
        max_backoff = 60  # 60 seconds max backoff
        last_update_check = 0
        last_product_pull = 0
        last_order_pull = 0
        last_deletion_sync = 0
        last_archive_check = 0

        while True:
            now = time.time()
            # Periodically check for remote updates (every 1 hour - 3600 seconds)
            if now - last_update_check > 3600:
                last_update_check = now
                try:
                    from utils import check_and_apply_updates
                    check_and_apply_updates()
                except Exception as u_err:
                    logger.error("Auto-updater failed during sync loop: %s", u_err)

            try:
                # 1. PUSH pending orders (runs every sync_interval, typically 30 seconds)
                # It only contacts the server if there are actually pending orders locally.
                synced = sync_orders(app)

                # 2. PULL products (every 30 minutes - 1800 seconds)
                pulled = 0
                if now - last_product_pull > 1800 or last_product_pull == 0:
                    pulled = pull_products(app)
                    last_product_pull = now

                # 3. PULL new orders from server (every 5 minutes - 300 seconds)
                pulled_orders = 0
                if now - last_order_pull > 300 or last_order_pull == 0:
                    pulled_orders = pull_orders_from_server(app)
                    last_order_pull = now

                # 4. Sync deleted orders (every 60 minutes - 3600 seconds)
                deleted_orders = 0
                if now - last_deletion_sync > 3600 or last_deletion_sync == 0:
                    deleted_orders = sync_deleted_orders(app)
                    last_deletion_sync = now

                # 5. Daily revenue archives (check every 1 hour - 3600 seconds)
                if now - last_archive_check > 3600 or last_archive_check == 0:
                    check_and_generate_daily_archives(app)
                    last_archive_check = now

                logger.debug(
                    "Sync cycle: %d orders pushed, %d products pulled, %d orders pulled, %d deleted locally.",
                    synced,
                    pulled,
                    pulled_orders,
                    deleted_orders
                )
                consecutive_failures = 0
                sleep_time = sync_interval
            except requests.RequestException as exc:
                consecutive_failures += 1
                sleep_time = min(sync_interval * (2 ** consecutive_failures), max_backoff)
                logger.warning(
                    "Sync cycle network error (#%d consecutive): %s. "
                    "Sleeping %ds before retry.",
                    consecutive_failures,
                    exc,
                    sleep_time,
                )
            except Exception as exc:
                consecutive_failures += 1
                sleep_time = min(sync_interval * (2 ** consecutive_failures), max_backoff)
                logger.error(
                    "Sync cycle unexpected error (#%d consecutive): %s. "
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
