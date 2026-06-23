"""
db_sync.py — Master-master replication/sync scheduler for the Neon database instances.

Runs in a background thread on the server, connecting to each database instance
to merge and sync products, categories, orders, order items, serial keys, and admin users.

STORAGE OPTIMIZATION:
  image_data (BYTEA) is intentionally EXCLUDED from replication. Images are stored
  in the primary database only and served via the Flask app. Replicating raw binary
  blobs across all replicas would exhaust the Neon 5GB free tier extremely fast.
  Each replica stores only the image filename reference.

CONNECTION OPTIMIZATION:
  Uses NullPool for replication engines to prevent idle connection leaks.
  Each cycle opens exactly len(DB_URLS) connections and closes all of them before sleeping.
"""

import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from extensions import DB_URLS
from models import AdminUser, Category, Product, Order, OrderItem, SerialKey

logger = logging.getLogger("db_sync")

def get_session(url: str):
    """Attempt connection and return a raw SQLAlchemy session and engine.

    Uses NullPool to ensure connections are closed immediately after use,
    preventing connection exhaustion on Neon's free tier.
    """
    try:
        connect_args = {}
        if "sqlite" not in url:
            connect_args["connect_timeout"] = 5

        engine = create_engine(
            url,
            poolclass=NullPool,  # Never keep idle connections — critical for Neon limits
            connect_args=connect_args,
        )
        Session = sessionmaker(bind=engine)
        session = Session()
        # Verify connection works before proceeding
        session.execute(text("SELECT 1"))
        return session, engine
    except Exception as exc:
        logger.warning("Database at %s is currently unreachable: %s", url.split("@")[-1], exc)
        return None, None

def replicate_databases():
    """Consolidate and write records across all online Neon databases.

    NOTE: image_data is intentionally excluded from replication to conserve storage.
    Only the image filename (reference) is synced. Images are served from the primary DB.
    """
    logger.info("Starting master-master database replication cycle...")
    
    # 1. Connect to all online DBs
    sessions = {}
    for i, url in enumerate(DB_URLS):
        session, engine = get_session(url)
        if session:
            sessions[i] = (session, engine)
            
    if len(sessions) < 2:
        logger.warning("Replication skipped: less than 2 database accounts are currently online/reachable.")
        for s, e in sessions.values():
            s.close()
            e.dispose()
        return

    # 2. Extract and merge data in memory
    merged_admins = {}
    merged_categories = {}
    merged_products = {}
    merged_orders = {}  # key: local_id -> dict of order attributes + item lists
    merged_serials = {}

    for idx, (session, _) in sessions.items():
        try:
            # Merge Admin Users (prioritize custom/changed passwords over default seeded ones)
            for u in session.query(AdminUser).all():
                existing = merged_admins.get(u.username)
                if not existing or (not u.must_change_password and existing["must_change_password"]):
                    merged_admins[u.username] = {
                        "username": u.username,
                        "password_hash": u.password_hash,
                        "must_change_password": u.must_change_password,
                        "created_at": u.created_at
                    }
                    
            # Merge Categories
            for c in session.query(Category).all():
                if c.id not in merged_categories:
                    merged_categories[c.id] = {
                        "id": c.id,
                        "name": c.name,
                        "display_order": c.display_order
                    }
                    
            # Merge Products (keep latest version by updated_at)
            # NOTE: image_data is intentionally excluded — see module docstring
            for p in session.query(Product).all():
                existing = merged_products.get(p.id)
                if not existing or (p.updated_at and existing["updated_at"] and p.updated_at > existing["updated_at"]):
                    merged_products[p.id] = {
                        "id": p.id,
                        "category_id": p.category_id,
                        "name": p.name,
                        "description": p.description,
                        "price_cents": p.price_cents,
                        "image": p.image,          # Filename reference only
                        "is_active": p.is_active,
                        "created_at": p.created_at,
                        "updated_at": p.updated_at,
                        # image_data and image_mime are DELIBERATELY excluded here.
                        # Replicating raw binary blobs across 3 Neon accounts wastes
                        # ~3x storage and is the #1 cause of hitting the 5GB limit fast.
                    }
                    
            # Merge Orders and their items (preserve "synced" status)
            for o in session.query(Order).all():
                existing = merged_orders.get(o.local_id)
                if not existing or (o.status == "synced" and existing["status"] != "synced"):
                    items = []
                    for item in o.items:
                        items.append({
                            "product_id": item.product_id,
                            "product_name_snapshot": item.product_name_snapshot,
                            "unit_price_cents_snapshot": item.unit_price_cents_snapshot,
                            "quantity": item.quantity,
                            "subtotal_cents": item.subtotal_cents
                        })
                    merged_orders[o.local_id] = {
                        "local_id": o.local_id,
                        "status": o.status,
                        "total_cents": o.total_cents,
                        "created_at": o.created_at,
                        "synced_at": o.synced_at,
                        "device_id": o.device_id,
                        "items": items
                    }
                    
            # Merge Serial Keys (preserve active states)
            for s in session.query(SerialKey).all():
                existing = merged_serials.get(s.serial_hash)
                if not existing or (s.is_active and not existing["is_active"]):
                    merged_serials[s.serial_hash] = {
                        "serial_hash": s.serial_hash,
                        "label": s.label,
                        "device_id": s.device_id,
                        "is_active": s.is_active,
                        "activated_at": s.activated_at,
                        "expires_at": s.expires_at,
                        "created_at": s.created_at
                    }
        except Exception as query_exc:
            logger.error("Data extraction query failed on DB index %d: %s", idx, query_exc)

    # 3. Synchronize consolidated data back to all reachable databases
    for idx, (session, _) in sessions.items():
        try:
            # Sync Admin Users
            for username, data in merged_admins.items():
                admin = session.query(AdminUser).filter_by(username=username).first()
                if not admin:
                    admin = AdminUser(
                        username=data["username"],
                        password_hash=data["password_hash"],
                        must_change_password=data["must_change_password"],
                        created_at=data["created_at"]
                    )
                    session.add(admin)
                else:
                    admin.password_hash = data["password_hash"]
                    admin.must_change_password = data["must_change_password"]
            session.flush()

            # Sync Categories
            for cat_id, data in merged_categories.items():
                cat = session.get(Category, cat_id)
                if not cat:
                    cat = Category(
                        id=data["id"],
                        name=data["name"],
                        display_order=data["display_order"]
                    )
                    session.add(cat)
                else:
                    cat.name = data["name"]
                    cat.display_order = data["display_order"]
            session.flush()

            # Sync Products (image_data excluded — see module docstring)
            for prod_id, data in merged_products.items():
                # Enforce FK constraints
                if not session.get(Category, data["category_id"]):
                    continue
                prod = session.get(Product, prod_id)
                if not prod:
                    prod = Product(
                        id=data["id"],
                        category_id=data["category_id"],
                        name=data["name"],
                        description=data["description"],
                        price_cents=data["price_cents"],
                        image=data["image"],
                        is_active=data["is_active"],
                        created_at=data["created_at"],
                        updated_at=data["updated_at"],
                        # image_data intentionally NOT set here — see module docstring
                    )
                    session.add(prod)
                else:
                    prod.category_id = data["category_id"]
                    prod.name = data["name"]
                    prod.description = data["description"]
                    prod.price_cents = data["price_cents"]
                    prod.image = data["image"]
                    prod.is_active = data["is_active"]
                    prod.updated_at = data["updated_at"]
                    # image_data intentionally NOT updated here — see module docstring
            session.flush()

            # Sync Serial Keys
            for serial_hash, data in merged_serials.items():
                sk = session.query(SerialKey).filter_by(serial_hash=serial_hash).first()
                if not sk:
                    sk = SerialKey(
                        serial_hash=data["serial_hash"],
                        label=data["label"],
                        device_id=data["device_id"],
                        is_active=data["is_active"],
                        activated_at=data["activated_at"],
                        expires_at=data["expires_at"],
                        created_at=data["created_at"]
                    )
                    session.add(sk)
                else:
                    sk.label = data["label"]
                    sk.device_id = data["device_id"]
                    sk.is_active = data["is_active"]
                    sk.activated_at = data["activated_at"]
            session.flush()

            # Sync Orders & Order Items
            for local_id, data in merged_orders.items():
                order = session.query(Order).filter_by(local_id=local_id).first()
                if not order:
                    order = Order(
                        local_id=data["local_id"],
                        status=data["status"],
                        total_cents=data["total_cents"],
                        created_at=data["created_at"],
                        synced_at=data["synced_at"],
                        device_id=data["device_id"]
                    )
                    session.add(order)
                    session.flush()  # Resolve DB autoincrement ID for order items mapping
                    
                    for item_data in data["items"]:
                        item = OrderItem(
                            order_id=order.id,
                            product_id=item_data["product_id"],
                            product_name_snapshot=item_data["product_name_snapshot"],
                            unit_price_cents_snapshot=item_data["unit_price_cents_snapshot"],
                            quantity=item_data["quantity"],
                            subtotal_cents=item_data["subtotal_cents"]
                        )
                        session.add(item)
                else:
                    order.status = data["status"]
                    order.synced_at = data["synced_at"]
            
            # Reset postgres sequences to prevent IntegrityError on new inserts
            if "sqlite" not in str(session.bind.url):
                for table in ["products", "categories", "orders", "order_items", "serial_keys", "admin_users"]:
                    try:
                        session.execute(text(
                            f"SELECT setval(seq, COALESCE((SELECT MAX(id) FROM {table}), 1)) "
                            f"FROM (SELECT pg_get_serial_sequence('{table}', 'id') AS seq) s "
                            f"WHERE seq IS NOT NULL"
                        ))
                    except Exception as seq_exc:
                        logger.warning("Failed to reset sequence for table %s: %s", table, seq_exc)
            
            session.commit()
            logger.info("Successfully replicated data to database index %d.", idx)
        except Exception as write_exc:
            session.rollback()
            logger.error("Replication writing failed on DB index %d: %s", idx, write_exc)

    # 4. Cleanup resources — NullPool ensures connections are fully closed
    for s, e in sessions.values():
        s.close()
        e.dispose()
        
    logger.info("Database replication cycle finished successfully.")
