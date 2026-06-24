import os
import json
from sqlalchemy import create_engine, text
from app import create_app
from extensions import db
from models import AdminUser
from werkzeug.security import generate_password_hash

# 1. Delete local SQLite files
local_dbs = [
    "local_data.db",
    "server_data.db",
    "coffee_shop.db",
    r"..\desktop_app\local_data.db"
]
for db_file in local_dbs:
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
            print(f"Deleted local database file: {db_file}")
        except Exception as e:
            print(f"Failed to delete {db_file}: {e}")

# 2. Reset db_rotation.json state
rotation_state = {
    "active_index": 0,
    "last_rotated": "2026-06-24T06:00:00+01:00"
}
with open("db_rotation.json", "w", encoding="utf-8") as f:
    json.dump(rotation_state, f, indent=2)
print("Reset db_rotation.json to initial index 0.")

# 3. Drop and recreate tables on all three Neon databases
with open("db_urls.json", "r", encoding="utf-8") as f:
    urls = json.load(f)

app = create_app("server")
with app.app_context():
    for i, url in enumerate(urls):
        print(f"\nResetting database {i}: {url.split('@')[-1]}")
        try:
            # Bind engine
            engine = create_engine(url)
            
            # Drop all tables in database
            with engine.connect() as conn:
                print("Dropping all existing tables...")
                # Drop foreign key dependency tables first or drop with cascade
                tables = ["order_items", "orders", "products", "categories", "serial_keys", "sync_logs", "admin_users"]
                for table in tables:
                    try:
                        conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
                    except Exception as drop_err:
                        print(f"Notice: Drop {table} issue (might not exist): {drop_err}")
                conn.commit()
            
            # Create all tables
            print("Recreating tables...")
            db.metadata.create_all(bind=engine)
            
            # Seed default admin user
            from sqlalchemy.orm import sessionmaker
            Session = sessionmaker(bind=engine)
            session = Session()
            
            # Check if admin already seeded (should be empty now)
            admin = session.query(AdminUser).filter_by(username="admin").first()
            if not admin:
                admin = AdminUser(
                    username="admin",
                    password_hash=generate_password_hash("changeme123"),
                    must_change_password=True
                )
                session.add(admin)
                session.commit()
                print("Seeded default admin user: admin / changeme123")
            session.close()
            engine.dispose()
            print(f"Database {i} reset successfully.")
        except Exception as e:
            print(f"Failed to reset database {i}: {e}")

print("\nAll databases reset successfully!")
