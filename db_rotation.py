"""
db_rotation.py — Automated 10-day database rotation and data replication logic.

Handles:
  - Checking the rotation interval (10 days at 6:00 AM local time).
  - Merging and fully copying all database tables (including binary image data) to the next Neon database.
  - Dumping all database records to CSV files inside the archive folder.
  - Committing and pushing state (db_rotation.json and the archive folder) back to the Git repository.
  - Triggering the Render deploy webhook to redeploy the app under the new active DB.
"""

import os
import json
import logging
import csv
import subprocess
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

# Import DB_URLS and models
from extensions import DB_URLS
from models import AdminUser, Category, Product, Order, OrderItem, SerialKey, SyncLog

logger = logging.getLogger("db_rotation")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ROTATION_FILE = os.path.join(BASE_DIR, "db_rotation.json")
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")

def get_rotation_state():
    """Load or initialize rotation state from db_rotation.json."""
    if not os.path.exists(ROTATION_FILE):
        # Default starting point: index 0, seeded to today (or June 24, 2026)
        initial_state = {
            "active_index": 0,
            "last_rotated": "2026-06-24T06:00:00+01:00"
        }
        try:
            os.makedirs(os.path.dirname(ROTATION_FILE), exist_ok=True)
            with open(ROTATION_FILE, "w", encoding="utf-8") as f:
                json.dump(initial_state, f, indent=2)
        except Exception as e:
            logger.error("Failed to write initial db_rotation.json: %s", e)
        return initial_state
    
    try:
        with open(ROTATION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to read db_rotation.json: %s", e)
        return {"active_index": 0, "last_rotated": "2026-06-24T06:00:00+01:00"}

def save_rotation_state(active_index, last_rotated_str):
    """Save the updated rotation state back to the JSON file."""
    state = {
        "active_index": active_index,
        "last_rotated": last_rotated_str
    }
    try:
        with open(ROTATION_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error("Failed to save db_rotation.json: %s", e)

def get_session(url: str):
    """Open and return a session and engine for copy/migration tasks using NullPool."""
    try:
        connect_args = {}
        if "sqlite" not in url:
            connect_args["connect_timeout"] = 5
        engine = create_engine(url, poolclass=NullPool, connect_args=connect_args)
        Session = sessionmaker(bind=engine)
        session = Session()
        session.execute(text("SELECT 1"))
        return session, engine
    except Exception as exc:
        logger.error("Failed to connect to database %s: %s", url.split("@")[-1], exc)
        return None, None

def copy_database_content(src_url, dest_url):
    """
    Perform a complete transaction-safe copy of all tables from src_url to dest_url.
    Updates existing rows and creates missing ones, copying full data including binaries.
    """
    src_session, src_engine = get_session(src_url)
    dest_session, dest_engine = get_session(dest_url)
    
    if not src_session or not dest_session:
        logger.error("Replication copy failed: source or destination database is unreachable.")
        if src_session: src_session.close()
        if dest_session: dest_session.close()
        return False
        
    try:
        logger.info("Copying tables to next database index...")
        
        # 1. Sync Admin Users
        for src_user in src_session.query(AdminUser).all():
            dest_user = dest_session.query(AdminUser).filter_by(username=src_user.username).first()
            if not dest_user:
                dest_user = AdminUser(
                    username=src_user.username,
                    password_hash=src_user.password_hash,
                    must_change_password=src_user.must_change_password,
                    created_at=src_user.created_at
                )
                dest_session.add(dest_user)
            else:
                dest_user.password_hash = src_user.password_hash
                dest_user.must_change_password = src_user.must_change_password
        dest_session.flush()

        # 2. Sync Categories
        for src_cat in src_session.query(Category).all():
            dest_cat = dest_session.get(Category, src_cat.id)
            if not dest_cat:
                dest_cat = Category(
                    id=src_cat.id,
                    name=src_cat.name,
                    display_order=src_cat.display_order
                )
                dest_session.add(dest_cat)
            else:
                dest_cat.name = src_cat.name
                dest_cat.display_order = src_cat.display_order
        dest_session.flush()

        # 3. Sync Products (image_data is fully copied during migration)
        for src_prod in src_session.query(Product).all():
            dest_prod = dest_session.get(Product, src_prod.id)
            if not dest_prod:
                dest_prod = Product(
                    id=src_prod.id,
                    category_id=src_prod.category_id,
                    name=src_prod.name,
                    description=src_prod.description,
                    price_cents=src_prod.price_cents,
                    image=src_prod.image,
                    image_data=src_prod.image_data,
                    image_mime=src_prod.image_mime,
                    is_active=src_prod.is_active,
                    created_at=src_prod.created_at,
                    updated_at=src_prod.updated_at
                )
                dest_session.add(dest_prod)
            else:
                dest_prod.category_id = src_prod.category_id
                dest_prod.name = src_prod.name
                dest_prod.description = src_prod.description
                dest_prod.price_cents = src_prod.price_cents
                dest_prod.image = src_prod.image
                dest_prod.image_data = src_prod.image_data
                dest_prod.image_mime = src_prod.image_mime
                dest_prod.is_active = src_prod.is_active
                dest_prod.updated_at = src_prod.updated_at
        dest_session.flush()

        # 4. Sync Serial Keys
        for src_key in src_session.query(SerialKey).all():
            dest_key = dest_session.query(SerialKey).filter_by(serial_hash=src_key.serial_hash).first()
            if not dest_key:
                dest_key = SerialKey(
                    serial_hash=src_key.serial_hash,
                    label=src_key.label,
                    device_id=src_key.device_id,
                    is_active=src_key.is_active,
                    activated_at=src_key.activated_at,
                    expires_at=src_key.expires_at,
                    created_at=src_key.created_at
                )
                dest_session.add(dest_key)
            else:
                dest_key.label = src_key.label
                dest_key.device_id = src_key.device_id
                dest_key.is_active = src_key.is_active
                dest_key.activated_at = src_key.activated_at
                dest_key.expires_at = src_key.expires_at
        dest_session.flush()

        # 5. Sync Orders
        for src_order in src_session.query(Order).all():
            dest_order = dest_session.query(Order).filter_by(local_id=src_order.local_id).first()
            if not dest_order:
                dest_order = Order(
                    id=src_order.id,
                    local_id=src_order.local_id,
                    status=src_order.status,
                    total_cents=src_order.total_cents,
                    created_at=src_order.created_at,
                    synced_at=src_order.synced_at,
                    device_id=src_order.device_id
                )
                dest_session.add(dest_order)
                dest_session.flush()
                # Copy order items
                for src_item in src_order.items:
                    dest_item = OrderItem(
                        id=src_item.id,
                        order_id=dest_order.id,
                        product_id=src_item.product_id,
                        product_name_snapshot=src_item.product_name_snapshot,
                        unit_price_cents_snapshot=src_item.unit_price_cents_snapshot,
                        quantity=src_item.quantity,
                        subtotal_cents=src_item.subtotal_cents
                    )
                    dest_session.add(dest_item)
            else:
                dest_order.status = src_order.status
                dest_order.synced_at = src_order.synced_at
                dest_order.total_cents = src_order.total_cents
                # Update/Upsert items as well
                for src_item in src_order.items:
                    dest_item = dest_session.query(OrderItem).filter_by(id=src_item.id).first()
                    if not dest_item:
                        dest_item = OrderItem(
                            id=src_item.id,
                            order_id=dest_order.id,
                            product_id=src_item.product_id,
                            product_name_snapshot=src_item.product_name_snapshot,
                            unit_price_cents_snapshot=src_item.unit_price_cents_snapshot,
                            quantity=src_item.quantity,
                            subtotal_cents=src_item.subtotal_cents
                        )
                        dest_session.add(dest_item)
                    else:
                        dest_item.product_id = src_item.product_id
                        dest_item.product_name_snapshot = src_item.product_name_snapshot
                        dest_item.unit_price_cents_snapshot = src_item.unit_price_cents_snapshot
                        dest_item.quantity = src_item.quantity
                        dest_item.subtotal_cents = src_item.subtotal_cents
        dest_session.flush()

        # 6. Sync SyncLogs
        for src_log in src_session.query(SyncLog).all():
            dest_log = dest_session.query(SyncLog).filter_by(id=src_log.id).first()
            if not dest_log:
                dest_log = SyncLog(
                    id=src_log.id,
                    timestamp=src_log.timestamp,
                    direction=src_log.direction,
                    status=src_log.status,
                    detail=src_log.detail,
                    device_id=src_log.device_id
                )
                dest_session.add(dest_log)
        dest_session.flush()

        # Reset sequences on PostgreSQL
        if "sqlite" not in str(dest_session.bind.url):
            for table in ["products", "categories", "orders", "order_items", "serial_keys", "admin_users", "sync_logs"]:
                try:
                    dest_session.execute(text(
                        f"SELECT setval(seq, COALESCE((SELECT MAX(id) FROM {table}), 1)) "
                        f"FROM (SELECT pg_get_serial_sequence('{table}', 'id') AS seq) s "
                        f"WHERE seq IS NOT NULL"
                    ))
                except Exception as seq_exc:
                    logger.warning("Failed to reset sequence for table %s: %s", table, seq_exc)

        dest_session.commit()
        logger.info("Successfully copied database content to the next instance.")
        return True
    except Exception as e:
        dest_session.rollback()
        logger.exception("Failed to copy database content during rotation: %s", e)
        return False
    finally:
        src_session.close()
        dest_session.close()
        src_engine.dispose()
        dest_engine.dispose()

def archive_database_to_csv(session, backup_dir):
    """Export all tables as CSV files to the archive folder."""
    try:
        os.makedirs(backup_dir, exist_ok=True)
        
        # 1. Archive orders
        orders_file = os.path.join(backup_dir, "orders.csv")
        orders = session.query(Order).all()
        with open(orders_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "local_id", "status", "total_cents", "created_at", "synced_at", "device_id"])
            for o in orders:
                writer.writerow([o.id, o.local_id, o.status, o.total_cents, o.created_at, o.synced_at, o.device_id])
                
        # 2. Archive order_items
        items_file = os.path.join(backup_dir, "order_items.csv")
        items = session.query(OrderItem).all()
        with open(items_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "order_id", "product_id", "product_name_snapshot", "unit_price_cents_snapshot", "quantity", "subtotal_cents"])
            for i in items:
                writer.writerow([i.id, i.order_id, i.product_id, i.product_name_snapshot, i.unit_price_cents_snapshot, i.quantity, i.subtotal_cents])
                
        # 3. Archive products
        products_file = os.path.join(backup_dir, "products.csv")
        products = session.query(Product).all()
        with open(products_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "category_id", "name", "description", "price_cents", "image", "is_active", "created_at", "updated_at"])
            for p in products:
                writer.writerow([p.id, p.category_id, p.name, p.description, p.price_cents, p.image, p.is_active, p.created_at, p.updated_at])
                
        # 4. Archive categories
        categories_file = os.path.join(backup_dir, "categories.csv")
        categories = session.query(Category).all()
        with open(categories_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "name", "display_order"])
            for c in categories:
                writer.writerow([c.id, c.name, c.display_order])

        # 5. Archive serial_keys
        serials_file = os.path.join(backup_dir, "serial_keys.csv")
        serials = session.query(SerialKey).all()
        with open(serials_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "serial_hash", "label", "device_id", "is_active", "activated_at", "expires_at", "created_at"])
            for s in serials:
                writer.writerow([s.id, s.serial_hash, s.label, s.device_id, s.is_active, s.activated_at, s.expires_at, s.created_at])

        # 6. Archive sync_logs
        logs_file = os.path.join(backup_dir, "sync_logs.csv")
        logs = session.query(SyncLog).all()
        with open(logs_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "timestamp", "direction", "status", "detail", "device_id"])
            for l in logs:
                writer.writerow([l.id, l.timestamp, l.direction, l.status, l.detail, l.device_id])

        logger.info("Database archived to CSV files in %s", backup_dir)
        return True
    except Exception as e:
        logger.exception("Failed to archive database to CSV: %s", e)
        return False

def commit_and_push_rotation_state(backup_dir):
    """Commit updated db_rotation.json and the backup folder, and push to GitHub."""
    try:
        # Configure git identity locally
        subprocess.run(["git", "config", "user.name", "Shawaya POS Server"], cwd=BASE_DIR, capture_output=True)
        subprocess.run(["git", "config", "user.email", "server@shawaya.local"], cwd=BASE_DIR, capture_output=True)
        
        # Add files
        subprocess.run(["git", "add", "db_rotation.json"], cwd=BASE_DIR, capture_output=True)
        if backup_dir and os.path.exists(backup_dir):
            rel_backup_dir = os.path.relpath(backup_dir, BASE_DIR)
            subprocess.run(["git", "add", rel_backup_dir], cwd=BASE_DIR, capture_output=True)
            
        res = subprocess.run(["git", "commit", "-m", "chore: rotate active database and archive CSV backups"], cwd=BASE_DIR, capture_output=True, text=True)
        
        # Push to remote
        github_token = os.environ.get("GITHUB_TOKEN")
        if github_token:
            remote_url = f"https://{github_token}@github.com/hafedazerty100/shawaya.git"
            push_res = subprocess.run(["git", "push", remote_url, "main"], cwd=BASE_DIR, capture_output=True, text=True)
            logger.info("Git push with token result: %s", push_res.stdout)
        else:
            push_res = subprocess.run(["git", "push", "origin", "main"], cwd=BASE_DIR, capture_output=True, text=True)
            logger.info("Git push standard result: %s", push_res.stdout)
            
        return True
    except Exception as exc:
        logger.exception("Failed to commit and push database rotation state: %s", exc)
        return False

def trigger_render_deploy():
    """Trigger the Render service deployment webhook."""
    import requests
    url = "https://api.render.com/deploy/srv-d8thne3eo5us73bgiutg?key=8cUu7s535k8"
    try:
        resp = requests.post(url, timeout=10)
        resp.raise_for_status()
        logger.info("Successfully triggered Render redeployment: %s", resp.text)
        return True
    except Exception as exc:
        logger.error("Failed to trigger Render redeployment: %s", exc)
        return False

def rotate_database_if_needed(app):
    """
    Check if it is time to rotate.
    Fires every 10 days at 6:00 AM local time.
    """
    if len(DB_URLS) < 2:
        return
        
    state = get_rotation_state()
    active_idx = state.get("active_index", 0)
    last_rotated_str = state.get("last_rotated", "2026-06-24T06:00:00+01:00")
    
    # Parse last_rotated with timezone
    last_rotated = datetime.fromisoformat(last_rotated_str)
    tz = last_rotated.tzinfo
    
    # Next rotation date is last_rotated + 10 days
    target_date = (last_rotated + timedelta(days=10)).date()
    target_dt = datetime.combine(target_date, datetime.min.time().replace(hour=6)).replace(tzinfo=tz)
    
    # Get current time in the same timezone
    now = datetime.now(timezone.utc).astimezone(tz)
    
    if now < target_dt:
        return
        
    logger.info("Database rotation target date reached! Initiating switch from index %d...", active_idx)
    
    # Next index in loop
    new_idx = (active_idx + 1) % len(DB_URLS)
    src_url = DB_URLS[active_idx]
    dest_url = DB_URLS[new_idx]
    
    # 1. First run the database replication to ensure all databases are up to date
    try:
        from db_sync import replicate_databases
        replicate_databases()
    except Exception as e:
        logger.error("Replication failed before rotation: %s", e)
        
    # 2. Fully copy all tables to the next database (including binary images)
    copied = copy_database_content(src_url, dest_url)
    if not copied:
        logger.error("Database migration copy failed. Aborting rotation switch.")
        return
        
    # 3. Create a CSV dump of the source database in the archive folder
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(ARCHIVE_DIR, f"backup_{timestamp_str}")
    
    src_session, src_engine = get_session(src_url)
    if src_session:
        archive_database_to_csv(src_session, backup_dir)
        src_session.close()
        src_engine.dispose()
    else:
        logger.warning("Could not establish session on source DB for CSV archiving.")
        
    # 4. Save updated rotation state
    # Set target_dt as the new base date to prevent double rotation checks today
    new_last_rotated_str = target_dt.isoformat()
    save_rotation_state(new_idx, new_last_rotated_str)
    
    # 5. Commit and push the files to Git repository
    commit_and_push_rotation_state(backup_dir)
    
    # 6. Trigger Render redeploy so the server starts with the new active database configuration
    trigger_render_deploy()
    
    logger.info("Database rotation completed. Switch to index %d initiated.", new_idx)
