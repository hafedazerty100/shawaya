"""
migrate_to_supabase.py — Migration script to transfer all schema, data, and image blobs from Neon/Local DB to Supabase.
"""

import os
import sys
import logging
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker

# Configure logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("migration")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from app import create_app
from extensions import db
from models import AdminUser, Category, Product, Order, OrderItem, SerialKey, SyncLog

SUPABASE_URL = "postgresql://postgres.neouimhepbutsatyuulx:A.p.p.le.99.3.4%40gmail.com@aws-1-eu-west-1.pooler.supabase.com:5432/postgres"

# Candidate source databases to pull data from
SOURCE_URLS = [
    "postgresql://neondb_owner:npg_HASe2VZoGuX9@ep-long-snow-abkfkbs5-pooler.eu-west-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
    "postgresql://neondb_owner:npg_PdHvBWD93zFQ@ep-late-sound-abx9vm19-pooler.eu-west-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
    "postgresql://neondb_owner:npg_NBJR9nlpW5PD@ep-aged-darkness-aia8vh1d-pooler.c-4.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
    f"sqlite:///{os.path.join(BASE_DIR, 'local_data.db')}",
    f"sqlite:///{os.path.join(BASE_DIR, 'server_data.db')}"
]

def find_best_source():
    """Find the reachable source database with the highest amount of data."""
    best_url = None
    max_score = -1

    for url in SOURCE_URLS:
        try:
            if "sqlite" in url and not os.path.exists(url.replace("sqlite:///", "")):
                continue
            engine = create_engine(url, connect_args={"connect_timeout": 5} if "sqlite" not in url else {})
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            
            Session = sessionmaker(bind=engine)
            session = Session()
            prod_count = session.query(Product).count()
            order_count = session.query(Order).count()
            score = prod_count * 10 + order_count
            logger.info("Found valid source %s (Products: %d, Orders: %d)", url.split("@")[-1], prod_count, order_count)
            if score > max_score:
                max_score = score
                best_url = url
            session.close()
            engine.dispose()
        except Exception as e:
            logger.warning("Source candidate %s is unreachable or invalid: %s", url.split("@")[-1], e)

    return best_url

def run_migration():
    logger.info("=== Starting Shawaya POS Database Migration to Supabase ===")
    
    # 1. Find source database
    src_url = find_best_source()
    if not src_url:
        logger.error("No valid source database could be reached!")
        return False
        
    logger.info("Selected source database: %s", src_url.split("@")[-1])
    
    # 2. Setup engines and sessions
    src_engine = create_engine(src_url)
    dest_engine = create_engine(SUPABASE_URL, connect_args={"connect_timeout": 10})
    
    # Test dest connection
    try:
        with dest_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Successfully connected to Supabase target database.")
    except Exception as e:
        logger.error("Failed to connect to Supabase target database: %s", e)
        return False
        
    # Create tables on Supabase using App Context
    app = create_app("server")
    with app.app_context():
        logger.info("Creating all tables on Supabase schema...")
        db.metadata.create_all(bind=dest_engine)
        
        # Ensure image_data BYTEA and image_mime columns exist
        with dest_engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS image_data BYTEA"))
                conn.commit()
            except Exception as e:
                logger.info("Notice image_data column check: %s", e)
            try:
                conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS image_mime VARCHAR(50)"))
                conn.commit()
            except Exception as e:
                logger.info("Notice image_mime column check: %s", e)
            try:
                conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS quantity INTEGER DEFAULT 99 NOT NULL"))
                conn.commit()
            except Exception as e:
                logger.info("Notice quantity column check: %s", e)
            try:
                conn.execute(text("ALTER TABLE sync_logs ALTER COLUMN direction TYPE VARCHAR(50)"))
                conn.commit()
            except Exception as e:
                logger.info("Notice sync_logs direction column check: %s", e)

    SrcSession = sessionmaker(bind=src_engine)
    DestSession = sessionmaker(bind=dest_engine)
    
    src = SrcSession()
    dest = DestSession()
    
    try:
        # ── 1. Migrate Admin Users ──────────────────────────────────────────
        logger.info("Migrating Admin Users...")
        for user in src.query(AdminUser).all():
            existing = dest.query(AdminUser).filter_by(username=user.username).first()
            if existing:
                existing.password_hash = user.password_hash
                existing.must_change_password = user.must_change_password
            else:
                new_user = AdminUser(
                    id=user.id,
                    username=user.username,
                    password_hash=user.password_hash,
                    must_change_password=user.must_change_password,
                    created_at=user.created_at
                )
                dest.add(new_user)
        dest.commit()
        
        # Adjust sequence for admin_users
        with dest_engine.connect() as conn:
            conn.execute(text("SELECT setval('admin_users_id_seq', (SELECT COALESCE(MAX(id), 1) FROM admin_users))"))
            conn.commit()

        # ── 2. Migrate Categories ───────────────────────────────────────────
        logger.info("Migrating Categories...")
        cat_id_map = {}
        for cat in src.query(Category).order_by(Category.id).all():
            existing = dest.query(Category).filter_by(name=cat.name).first()
            if existing:
                existing.display_order = cat.display_order
                cat_id_map[cat.id] = existing.id
            else:
                new_cat = Category(
                    id=cat.id,
                    name=cat.name,
                    display_order=cat.display_order
                )
                dest.add(new_cat)
                dest.flush()
                cat_id_map[cat.id] = new_cat.id
        dest.commit()

        with dest_engine.connect() as conn:
            conn.execute(text("SELECT setval('categories_id_seq', (SELECT COALESCE(MAX(id), 1) FROM categories))"))
            conn.commit()

        # ── 3. Migrate Products (including image binary data) ───────────────
        logger.info("Migrating Products and Image Blobs...")
        uploads_folder = os.path.join(BASE_DIR, "static", "uploads", "products")
        
        for prod in src.query(Product).order_by(Product.id).all():
            # Retrieve or load binary image data if missing
            img_data = prod.image_data
            img_mime = prod.image_mime or "image/jpeg"
            
            if not img_data and prod.image and os.path.exists(uploads_folder):
                local_img_path = os.path.join(uploads_folder, prod.image)
                if os.path.isfile(local_img_path):
                    try:
                        with open(local_img_path, "rb") as f:
                            img_data = f.read()
                        logger.info("Loaded missing binary image from disk for product '%s'", prod.name)
                    except Exception as err:
                        logger.warning("Could not load image %s from disk: %s", prod.image, err)

            target_cat_id = cat_id_map.get(prod.category_id, prod.category_id)
            existing = dest.query(Product).filter_by(id=prod.id).first()
            if existing:
                existing.name = prod.name
                existing.description = prod.description
                existing.category_id = target_cat_id
                existing.price_cents = prod.price_cents
                existing.image = prod.image
                existing.is_active = prod.is_active
                existing.quantity = getattr(prod, "quantity", 99)
                if img_data:
                    existing.image_data = img_data
                    existing.image_mime = img_mime
            else:
                new_prod = Product(
                    id=prod.id,
                    name=prod.name,
                    description=prod.description,
                    category_id=target_cat_id,
                    price_cents=prod.price_cents,
                    image=prod.image,
                    image_data=img_data,
                    image_mime=img_mime,
                    is_active=prod.is_active,
                    quantity=getattr(prod, "quantity", 99),
                    created_at=prod.created_at,
                    updated_at=prod.updated_at
                )
                dest.add(new_prod)
        dest.commit()

        with dest_engine.connect() as conn:
            conn.execute(text("SELECT setval('products_id_seq', (SELECT COALESCE(MAX(id), 1) FROM products))"))
            conn.commit()

        # ── 4. Migrate Orders ───────────────────────────────────────────────
        logger.info("Migrating Orders...")
        order_id_map = {}
        for ord_obj in src.query(Order).order_by(Order.id).all():
            existing = dest.query(Order).filter_by(local_id=ord_obj.local_id).first()
            if existing:
                existing.status = ord_obj.status
                existing.total_cents = ord_obj.total_cents
                existing.synced_at = ord_obj.synced_at
                existing.device_id = ord_obj.device_id
                order_id_map[ord_obj.id] = existing.id
            else:
                new_ord = Order(
                    id=ord_obj.id,
                    local_id=ord_obj.local_id,
                    status=ord_obj.status,
                    total_cents=ord_obj.total_cents,
                    created_at=ord_obj.created_at,
                    synced_at=ord_obj.synced_at,
                    device_id=ord_obj.device_id
                )
                dest.add(new_ord)
                dest.flush()
                order_id_map[ord_obj.id] = new_ord.id
        dest.commit()

        with dest_engine.connect() as conn:
            conn.execute(text("SELECT setval('orders_id_seq', (SELECT COALESCE(MAX(id), 1) FROM orders))"))
            conn.commit()

        # ── 5. Migrate Order Items ──────────────────────────────────────────
        logger.info("Migrating Order Items...")
        for item in src.query(OrderItem).order_by(OrderItem.id).all():
            target_order_id = order_id_map.get(item.order_id, item.order_id)
            existing = dest.query(OrderItem).filter_by(id=item.id).first()
            if not existing:
                new_item = OrderItem(
                    id=item.id,
                    order_id=target_order_id,
                    product_id=item.product_id,
                    product_name_snapshot=item.product_name_snapshot,
                    unit_price_cents_snapshot=item.unit_price_cents_snapshot,
                    quantity=item.quantity,
                    subtotal_cents=item.subtotal_cents
                )
                dest.add(new_item)
        dest.commit()

        with dest_engine.connect() as conn:
            conn.execute(text("SELECT setval('order_items_id_seq', (SELECT COALESCE(MAX(id), 1) FROM order_items))"))
            conn.commit()

        # ── 6. Migrate Serial Keys ──────────────────────────────────────────
        logger.info("Migrating Serial Keys...")
        for key in src.query(SerialKey).all():
            existing = dest.query(SerialKey).filter_by(serial_hash=key.serial_hash).first()
            if not existing:
                new_key = SerialKey(
                    id=key.id,
                    serial_hash=key.serial_hash,
                    label=key.label,
                    device_id=key.device_id,
                    is_active=key.is_active,
                    activated_at=key.activated_at,
                    expires_at=key.expires_at,
                    created_at=key.created_at
                )
                dest.add(new_key)
        dest.commit()

        with dest_engine.connect() as conn:
            conn.execute(text("SELECT setval('serial_keys_id_seq', (SELECT COALESCE(MAX(id), 1) FROM serial_keys))"))
            conn.commit()

        # ── 7. Migrate Sync Logs ────────────────────────────────────────────
        logger.info("Migrating Sync Logs...")
        for log_entry in src.query(SyncLog).all():
            existing = dest.query(SyncLog).filter_by(id=log_entry.id).first()
            if not existing:
                new_log = SyncLog(
                    id=log_entry.id,
                    timestamp=log_entry.timestamp,
                    direction=log_entry.direction,
                    status=log_entry.status,
                    detail=log_entry.detail,
                    device_id=log_entry.device_id
                )
                dest.add(new_log)
        dest.commit()

        with dest_engine.connect() as conn:
            conn.execute(text("SELECT setval('sync_logs_id_seq', (SELECT COALESCE(MAX(id), 1) FROM sync_logs))"))
            conn.commit()

        # ── 8. Migration Verification ───────────────────────────────────────
        logger.info("=== Verification Report ===")
        dest_admins = dest.query(AdminUser).count()
        dest_cats = dest.query(Category).count()
        dest_prods = dest.query(Product).count()
        dest_prods_with_img = dest.query(Product).filter(Product.image_data != None).count()
        dest_orders = dest.query(Order).count()
        dest_items = dest.query(OrderItem).count()
        dest_serials = dest.query(SerialKey).count()
        
        logger.info("Supabase Admin Users: %d", dest_admins)
        logger.info("Supabase Categories: %d", dest_cats)
        logger.info("Supabase Products: %d (Products with Binary Image Data: %d)", dest_prods, dest_prods_with_img)
        logger.info("Supabase Orders: %d", dest_orders)
        logger.info("Supabase Order Items: %d", dest_items)
        logger.info("Supabase Serial Keys: %d", dest_serials)
        logger.info("=== Database Migration Completed Successfully ===")
        return True

    except Exception as exc:
        dest.rollback()
        logger.exception("Migration failed with error: %s", exc)
        return False
    finally:
        src.close()
        dest.close()
        src_engine.dispose()
        dest_engine.dispose()

if __name__ == "__main__":
    success = run_migration()
    if not success:
        sys.exit(1)
