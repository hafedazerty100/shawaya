"""
tests/test_orders.py — Test cases for ordering logic and sync.
"""

import uuid
from extensions import db
from models import Category, Product, Order, OrderItem


def test_create_order_success(desktop_app, desktop_client):
    """Test successful local order creation on the kiosk."""
    with desktop_app.app_context():
        # Setup category and product
        cat = Category(id=1, name="Coffee", display_order=1)
        prod = Product(
            id=10,
            category_id=1,
            name="Espresso",
            price_cents=250,
            is_active=True,
        )
        db.session.add_all([cat, prod])
        db.session.commit()

    order_payload = {
        "local_id": str(uuid.uuid4()),
        "device_id": "test-kiosk",
        "items": [
            {"product_id": 10, "quantity": 2}
        ]
    }

    # Post order creation request (should create as draft)
    resp = desktop_client.post("/api/orders", json=order_payload)
    assert resp.status_code == 201
    data = resp.get_json()
    assert "order_id" in data
    assert data["local_id"] == order_payload["local_id"]

    # Verify db contains correct draft records
    with desktop_app.app_context():
        order = Order.query.filter_by(local_id=order_payload["local_id"]).first()
        assert order is not None
        assert order.total_cents == 500
        assert order.status == "draft"

    # Confirm order (after simulated printing)
    resp_confirm = desktop_client.post(f"/api/orders/{data['order_id']}/confirm")
    assert resp_confirm.status_code == 200

    # Verify order is now pending
    with desktop_app.app_context():
        order = Order.query.filter_by(local_id=order_payload["local_id"]).first()
        assert order.status == "pending"
        assert len(order.items) == 1
        assert order.items[0].product_id == 10
        assert order.items[0].quantity == 2


def test_create_order_idempotency(desktop_app, desktop_client):
    """Test order creation idempotency: submitting same local_id returns 200/201 without duplication."""
    with desktop_app.app_context():
        cat = Category(id=1, name="Coffee", display_order=1)
        prod = Product(id=10, category_id=1, name="Espresso", price_cents=250, is_active=True)
        db.session.add_all([cat, prod])
        db.session.commit()

    order_payload = {
        "local_id": str(uuid.uuid4()),
        "device_id": "test-kiosk",
        "items": [
            {"product_id": 10, "quantity": 1}
        ]
    }

    # First request
    resp1 = desktop_client.post("/api/orders", json=order_payload)
    assert resp1.status_code == 201

    # Second request with identical body (same local_id)
    resp2 = desktop_client.post("/api/orders", json=order_payload)
    assert resp2.status_code == 200
    assert resp2.get_json()["local_id"] == order_payload["local_id"]

    # Verify database has only one order
    with desktop_app.app_context():
        orders = Order.query.filter_by(local_id=order_payload["local_id"]).all()
        assert len(orders) == 1


def test_create_order_validation_failures(desktop_app, desktop_client):
    """Test various input validation failure cases for order creation."""
    with desktop_app.app_context():
        cat = Category(id=1, name="Coffee", display_order=1)
        prod_active = Product(id=10, category_id=1, name="Espresso", price_cents=250, is_active=True)
        prod_inactive = Product(id=11, category_id=1, name="Latte", price_cents=350, is_active=False)
        db.session.add_all([cat, prod_active, prod_inactive])
        db.session.commit()

    # Case 1: Empty items list
    payload1 = {"local_id": str(uuid.uuid4()), "items": []}
    resp1 = desktop_client.post("/api/orders", json=payload1)
    assert resp1.status_code == 400
    assert "must have at least one item" in resp1.get_json()["error"].lower()

    # Case 2: Inactive product
    payload2 = {
        "local_id": str(uuid.uuid4()),
        "items": [{"product_id": 11, "quantity": 1}]
    }
    resp2 = desktop_client.post("/api/orders", json=payload2)
    assert resp2.status_code == 400
    assert "not found or inactive" in resp2.get_json()["error"].lower()

    # Case 3: Missing product
    payload3 = {
        "local_id": str(uuid.uuid4()),
        "items": [{"product_id": 999, "quantity": 1}]
    }
    resp3 = desktop_client.post("/api/orders", json=payload3)
    assert resp3.status_code == 400
    assert "not found or inactive" in resp3.get_json()["error"].lower()

    # Case 4: Invalid quantity
    payload4 = {
        "local_id": str(uuid.uuid4()),
        "items": [{"product_id": 10, "quantity": 0}]
    }
    resp4 = desktop_client.post("/api/orders", json=payload4)
    assert resp4.status_code == 400
    assert "invalid quantity" in resp4.get_json()["error"].lower()


def test_server_sync_orders_success(server_app, server_client):
    """Test server-side order sync ingestion endpoint."""
    sync_api_key = server_app.config.get("SYNC_API_KEY", "dev-insecure-sync-api-key")
    headers = {
        "X-API-KEY": sync_api_key,
        "Content-Type": "application/json"
    }

    local_id = str(uuid.uuid4())
    sync_payload = {
        "orders": [
            {
                "local_id": local_id,
                "total_cents": 500,
                "created_at": "2026-06-12T14:00:00Z",
                "device_id": "test-kiosk",
                "items": [
                    {
                        "product_id": 10,
                        "product_name_snapshot": "Espresso",
                        "unit_price_cents_snapshot": 250,
                        "quantity": 2,
                        "subtotal_cents": 500
                    }
                ]
            }
        ]
    }

    resp = server_client.post("/api/sync/orders", json=sync_payload, headers=headers)
    assert resp.status_code == 200
    results = resp.get_json().get("results", {})
    assert local_id in results
    assert results[local_id]["status"] == "ok"

    # Verify orders synced to the server database
    with server_app.app_context():
        order = Order.query.filter_by(local_id=local_id).first()
        assert order is not None
        assert order.status == "synced"
        assert order.total_cents == 500
        assert len(order.items) == 1
        assert order.items[0].product_name_snapshot == "Espresso"
        assert order.items[0].quantity == 2


def test_api_revenue_endpoint(desktop_app, desktop_client, monkeypatch):
    """Test desktop /api/revenue with direct Postgres connection mock and fallback."""
    from datetime import datetime, timezone
    
    with desktop_app.app_context():
        # Setup category and product if not present
        if not Category.query.get(1):
            cat = Category(id=1, name="Coffee", display_order=1)
            db.session.add(cat)
        if not Product.query.get(10):
            prod = Product(id=10, category_id=1, name="Espresso", price_cents=250, is_active=True)
            db.session.add(prod)
        db.session.commit()

        # Add a local unsynced order
        local_order = Order(
            local_id=str(uuid.uuid4()),
            status="pending",
            total_cents=500,
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(local_order)
        db.session.commit()

    # Case 1: Online direct Postgres queries succeed (mock _get_remote_revenue returning 1000 cents)
    monkeypatch.setattr("routes.desktop._get_remote_revenue", lambda start, end: 1000)
    resp = desktop_client.get("/api/revenue")
    assert resp.status_code == 200
    data = resp.get_json()
    # local order (500 cents) + mock server (1000 cents) = 1500 cents
    assert data["total_cents"] == 1500
    assert data["total_display"] == "15.00 DA"

    # Case 2: Online queries fail, fallback to purely local DB orders
    monkeypatch.setattr("routes.desktop._get_remote_revenue", lambda start, end: None)
    class MockResponse:
        status_code = 404
        text = "Not found"
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: MockResponse())

    resp = desktop_client.get("/api/revenue")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_cents"] == 500
    assert data["total_display"] == "5.00 DA"


def test_desktop_sync_all_endpoint(desktop_app, desktop_client, monkeypatch):
    """Test desktop /api/sync-all endpoint triggers full synchronization pipeline."""
    # Mock all backend sync steps
    monkeypatch.setattr("sync.sync_orders", lambda app: 1)
    monkeypatch.setattr("sync.pull_products", lambda app: 2)
    monkeypatch.setattr("sync.pull_orders_from_server", lambda app: 3)
    monkeypatch.setattr("sync.sync_deleted_orders", lambda app: 4)

    resp = desktop_client.post("/api/sync-all")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["orders_pushed"] == 1
    assert data["products_pulled"] == 2
    assert data["orders_pulled"] == 3
    assert data["deletions_synced"] == 4
    assert "تمت المزامنة بنجاح!" in data["message"]


def test_server_sync_databases_endpoint(server_app, server_client, monkeypatch):
    """Test server /admin/sync-databases manually triggers replication (requires auth)."""
    from werkzeug.security import generate_password_hash
    from models import AdminUser

    # 1. Without login should redirect (since @login_required is used)
    resp = server_client.post("/admin/sync-databases")
    assert resp.status_code == 302

    # 2. Login
    with server_app.app_context():
        admin = AdminUser(
            username="admin_sync_test",
            password_hash=generate_password_hash("password123"),
            must_change_password=False
        )
        db.session.add(admin)
        db.session.commit()

    resp_login = server_client.post("/admin/login", data={
        "username": "admin_sync_test",
        "password": "password123"
    }, follow_redirects=True)
    assert resp_login.status_code == 200

    # Mock replication call
    replicate_called = False
    def mock_replicate():
        nonlocal replicate_called
        replicate_called = True

    monkeypatch.setattr("db_sync.replicate_databases", mock_replicate)

    resp_sync = server_client.post("/admin/sync-databases")
    assert resp_sync.status_code == 200
    assert resp_sync.get_json()["success"] is True
    assert replicate_called is True


