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

def mask_db_url(url: str) -> str:
    """Mask credentials in database connection string for logs/JSON response."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.netloc.split("@")[-1]
        return f"{parsed.scheme}://***@{host}{parsed.path}"
    except Exception:
        if "@" in url:
            return "postgresql://***@" + url.split("@")[-1]
        return "postgresql://***"

def replicate_databases(strategy: str = "push") -> dict:
    """Consolidate and write records across all online Neon databases.

    NOTE: image_data is intentionally excluded from replication to conserve storage.
    Only the image filename (reference) is synced. Images are served from the primary DB.
    """
    logger.info("Starting database replication cycle using strategy: %s...", strategy)
    
    # 1. Connect to all online DBs
    sessions = {}
    failed_dbs = []
    for i, url in enumerate(DB_URLS):
        session, engine = get_session(url)
        if session:
            sessions[i] = (session, engine)
        else:
            failed_dbs.append(mask_db_url(url))
            
    if len(sessions) < 2:
        logger.warning("Replication skipped: less than 2 database accounts are currently online/reachable.")
        for s, e in sessions.values():
            s.close()
            e.dispose()
        return {
            "success": False,
            "message": "تم تخطي المزامنة: عدد قواعد البيانات المتصلة أقل من 2.",
            "reachable_count": len(sessions),
            "total_count": len(DB_URLS),
            "synced_databases": [mask_db_url(DB_URLS[idx]) for idx in sessions.keys()],
            "failed_databases": failed_dbs,
            "merged_counts": {}
        }

    # 2. Extract and merge data in memory
    merged_admins = {}
    merged_categories = {}
    category_id_by_name = {}  # name -> unified_id
    db_cat_map = {}           # (db_idx, local_cat_id) -> unified_id

    merged_products = {}
    product_id_by_name = {}   # name -> unified_id
    db_prod_map = {}          # (db_idx, local_prod_id) -> unified_id

    merged_orders = {}  # key: local_id -> dict of order attributes + item lists
    merged_serials = {}

    active_idx = 0
    try:
        from extensions import get_active_db_index
        active_idx = get_active_db_index()
    except Exception:
        pass

    if strategy == "push":
        if active_idx not in sessions:
            logger.warning("Push replication failed: active database (index %d) is offline.", active_idx)
            for s, e in sessions.values():
                s.close()
                e.dispose()
            return {
                "success": False,
                "message": f"فشلت المزامنة: قاعدة البيانات النشطة (مؤشر {active_idx}) غير متصلة.",
                "reachable_count": len(sessions),
                "total_count": len(DB_URLS),
                "synced_databases": [],
                "failed_databases": failed_dbs,
                "merged_counts": {}
            }
        
        session_0 = sessions[active_idx][0]
        # Extract from active DB only
        try:
            for u in session_0.query(AdminUser).all():
                merged_admins[u.username] = {
                    "username": u.username,
                    "password_hash": u.password_hash,
                    "must_change_password": u.must_change_password,
                    "created_at": u.created_at
                }
            for c in session_0.query(Category).all():
                name_clean = c.name.strip()
                merged_categories[c.id] = {
                    "id": c.id,
                    "name": c.name,
                    "display_order": c.display_order
                }
                category_id_by_name[name_clean] = c.id
                db_cat_map[(active_idx, c.id)] = c.id
                
            for p in session_0.query(Product).all():
                name_clean = p.name.strip()
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
                    "quantity": p.quantity,
                }
                product_id_by_name[name_clean] = p.id
                db_prod_map[(active_idx, p.id)] = p.id

            for o in session_0.query(Order).all():
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

            for s in session_0.query(SerialKey).all():
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
            logger.error("Data extraction query failed on primary DB: %s", query_exc)
            
    else:
        # Original master-master pull/merge logic
        for idx, (session, _) in sessions.items():
            try:
                # Merge Admin Users
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
                    name_clean = c.name.strip()
                    if name_clean in category_id_by_name:
                        unified_id = category_id_by_name[name_clean]
                        db_cat_map[(idx, c.id)] = unified_id
                    else:
                        if c.id in merged_categories:
                            unified_id = max(merged_categories.keys()) + 1 if merged_categories else 1
                        else:
                            unified_id = c.id
                        
                        merged_categories[unified_id] = {
                            "id": unified_id,
                            "name": c.name,
                            "display_order": c.display_order
                        }
                        category_id_by_name[name_clean] = unified_id
                        db_cat_map[(idx, c.id)] = unified_id
                        
                # Merge Products
                for p in session.query(Product).all():
                    name_clean = p.name.strip()
                    unified_cat_id = db_cat_map.get((idx, p.category_id))
                    if not unified_cat_id:
                        logger.warning("Product %s references missing category ID %s in DB index %d.", p.name, p.category_id, idx)
                        continue

                    if name_clean in product_id_by_name:
                        unified_prod_id = product_id_by_name[name_clean]
                        db_prod_map[(idx, p.id)] = unified_prod_id
                        
                        existing = merged_products[unified_prod_id]
                        if not p.updated_at or not existing["updated_at"] or p.updated_at > existing["updated_at"]:
                            merged_products[unified_prod_id] = {
                                "id": unified_prod_id,
                                "category_id": unified_cat_id,
                                "name": p.name,
                                "description": p.description,
                                "price_cents": p.price_cents,
                                "image": p.image,
                                "is_active": p.is_active,
                                "created_at": p.created_at,
                                "updated_at": p.updated_at,
                                "quantity": p.quantity,
                            }
                    else:
                        if p.id in merged_products:
                            unified_prod_id = max(merged_products.keys()) + 1 if merged_products else 1
                        else:
                            unified_prod_id = p.id
                            
                        merged_products[unified_prod_id] = {
                            "id": unified_prod_id,
                            "category_id": unified_cat_id,
                            "name": p.name,
                            "description": p.description,
                            "price_cents": p.price_cents,
                            "image": p.image,
                            "is_active": p.is_active,
                            "created_at": p.created_at,
                            "updated_at": p.updated_at,
                            "quantity": p.quantity,
                        }
                        product_id_by_name[name_clean] = unified_prod_id
                        db_prod_map[(idx, p.id)] = unified_prod_id
                        
                # Merge Orders
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
                        
                # Merge Serial Keys
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

    # Re-map OrderItem product_id references to use unified product IDs using snapshot name
    for order_data in merged_orders.values():
        for item_data in order_data["items"]:
            if item_data["product_name_snapshot"]:
                unified_prod_id = product_id_by_name.get(item_data["product_name_snapshot"].strip())
                if unified_prod_id:
                    item_data["product_id"] = unified_prod_id

    results = {
        "success": True,
        "reachable_count": len(sessions),
        "total_count": len(DB_URLS),
        "synced_databases": [],
        "failed_databases": failed_dbs,
        "merged_counts": {
            "admin_users": len(merged_admins),
            "categories": len(merged_categories),
            "products": len(merged_products),
            "orders": len(merged_orders),
            "serial_keys": len(merged_serials)
        }
    }

    # 3. Synchronize consolidated data back to all reachable databases
    for idx, (session, _) in sessions.items():
        if idx == active_idx and strategy == "push":
            logger.info("Push strategy: skipping write back for active database index %d.", active_idx)
            results["synced_databases"].append(mask_db_url(DB_URLS[idx]))
            continue
            
        try:
            # If strategy is push, prune records not in primary DB first
            if strategy == "push":
                # Prune Admin Users
                for admin in session.query(AdminUser).all():
                    if admin.username not in merged_admins:
                        session.delete(admin)
                session.flush()

                # Prune Products (set OrderItem.product_id referencing deleted product to None)
                for prod in session.query(Product).all():
                    if prod.id not in merged_products:
                        session.query(OrderItem).filter_by(product_id=prod.id).update({OrderItem.product_id: None})
                        session.flush()
                        session.delete(prod)
                session.flush()

                # Prune Categories
                for cat in session.query(Category).all():
                    if cat.id not in merged_categories:
                        session.delete(cat)
                session.flush()

                # Prune Serial Keys
                for sk in session.query(SerialKey).all():
                    if sk.serial_hash not in merged_serials:
                        session.delete(sk)
                session.flush()

                # Prune Orders
                for order in session.query(Order).all():
                    if order.local_id not in merged_orders:
                        session.delete(order)
                session.flush()

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

            # Align Category IDs in local session before full category sync
            current_cats = session.query(Category).all()
            for cat in current_cats:
                name_clean = cat.name.strip()
                unified_id = category_id_by_name.get(name_clean)
                if unified_id and cat.id != unified_id:
                    old_name = cat.name
                    cat.name = f"{old_name}_temp_sync_{cat.id}"
                    session.flush()
                    
                    new_cat = session.get(Category, unified_id)
                    if not new_cat:
                        new_cat = Category(
                            id=unified_id,
                            name=old_name,
                            display_order=cat.display_order
                        )
                        session.add(new_cat)
                        session.flush()
                        
                    # Re-associate local Products to use unified Category ID
                    session.query(Product).filter_by(category_id=cat.id).update({Product.category_id: unified_id})
                    session.flush()
                    
                    session.delete(cat)
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

            # Align Product IDs in local session before full product sync
            current_prods = session.query(Product).all()
            for prod in current_prods:
                name_clean = prod.name.strip()
                unified_id = product_id_by_name.get(name_clean)
                if unified_id and prod.id != unified_id:
                    new_prod = session.get(Product, unified_id)
                    if not new_prod:
                        new_prod = Product(
                            id=unified_id,
                            category_id=prod.category_id,
                            name=prod.name,
                            description=prod.description,
                            price_cents=prod.price_cents,
                            image=prod.image,
                            image_data=prod.image_data,
                            image_mime=prod.image_mime,
                            is_active=prod.is_active,
                            created_at=prod.created_at,
                            updated_at=prod.updated_at,
                            quantity=prod.quantity,
                        )
                        session.add(new_prod)
                        session.flush()
                        
                    # Re-associate local OrderItems referencing the old product ID
                    session.query(OrderItem).filter_by(product_id=prod.id).update({OrderItem.product_id: unified_id})
                    session.flush()
                    
                    session.delete(prod)
                    session.flush()

            # Sync Products
            for prod_id, data in merged_products.items():
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
                        quantity=data.get("quantity", 0),
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
                    prod.quantity = data.get("quantity", 0)
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
                    session.flush()
                    
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
            
            # Reset postgres sequences
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
            results["synced_databases"].append(mask_db_url(DB_URLS[idx]))
        except Exception as write_exc:
            session.rollback()
            logger.error("Replication writing failed on DB index %d: %s", idx, write_exc)
            results["failed_databases"].append(mask_db_url(DB_URLS[idx]))

    # 4. Cleanup resources — NullPool ensures connections are fully closed
    for s, e in sessions.values():
        s.close()
        e.dispose()
        
    logger.info("Database replication cycle finished successfully.")
    
    if not results["synced_databases"]:
        results["success"] = False
        
    return results
