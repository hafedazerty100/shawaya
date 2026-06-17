"""
db_sync.py — Master-master replication/sync scheduler for the 3 Neon database instances.

Runs in a background thread on the server, connecting to each database instance
to merge and sync products, categories, orders, order items, serial keys, and admin users.
"""

import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from extensions import DB_URLS
from models import AdminUser, Category, Product, Order, OrderItem, SerialKey

logger = logging.getLogger("db_sync")

def get_session(url: str):
    """Attempt connection and return a custom SQLAlchemy session and engine."""
    try:
        # 5 seconds timeout to prevent blocking thread on dead endpoints
        engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
        Session = sessionmaker(bind=engine)
        session = Session()
        # Verify connection works
        session.execute(text("SELECT 1"))
        return session, engine
    except Exception as exc:
        logger.warning("Database at %s is currently unreachable: %s", url.split("@")[-1], exc)
        return None, None

def replicate_databases():
    """Consolidate and write records across all online Neon databases."""
    logger.info("Starting master-master database replication cycle...")
    
    # 1. Connect to all online DBs
    sessions = {}
    for i, url in enumerate(DB_URLS):
        session, engine = get_session(url)
        if session:
            sessions[i] = (session, engine)
            
    if len(sessions) < 2:
        logger.warning("Replication skipped: less than 2 database accounts are currently online/reachable.")
        # Disconnect any single online session
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
            # Merge Admin Users
            for u in session.query(AdminUser).all():
                if u.username not in merged_admins:
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
            for p in session.query(Product).all():
                existing = merged_products.get(p.id)
                if not existing or (p.updated_at and existing["updated_at"] and p.updated_at > existing["updated_at"]):
                    merged_products[p.id] = {
                        "id": p.id,
                        "category_id": p.category_id,
                        "name": p.name,
                        "description": p.description,
                        "price_cents": p.price_cents,
                        "image": p.image,
                        "is_active": p.is_active,
                        "created_at": p.created_at,
                        "updated_at": p.updated_at,
                        "image_data": p.image_data,
                        "image_mime": p.image_mime
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
                cat = session.query(Category).get(cat_id)
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

            # Sync Products
            for prod_id, data in merged_products.items():
                # Enforce FK constraints
                if not session.query(Category).get(data["category_id"]):
                    continue
                prod = session.query(Product).get(prod_id)
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
                        image_data=data["image_data"],
                        image_mime=data["image_mime"]
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
                    prod.image_data = data["image_data"]
                    prod.image_mime = data["image_mime"]
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
            
            session.commit()
            logger.info("Successfully replicated data to database index %d.", idx)
        except Exception as write_exc:
            session.rollback()
            logger.error("Replication writing failed on DB index %d: %s", idx, write_exc)

    # 4. Cleanup resources
    for s, e in sessions.values():
        s.close()
        e.dispose()
        
    logger.info("Database replication cycle finished successfully.")
