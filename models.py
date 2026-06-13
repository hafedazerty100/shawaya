"""
models.py — SQLAlchemy ORM models for the Coffee Shop POS.

All monetary values are stored as integers (cents) to avoid floating-point
rounding errors. For display, use utils.format_price(cents).

Indexes are added on high-frequency query columns for performance.
"""

import uuid
from datetime import datetime, timezone
from flask_login import UserMixin

from extensions import db


def _utcnow() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


class AdminUser(db.Model, UserMixin):
    """Server-side admin user for the dashboard."""

    __tablename__ = "admin_users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    # Forces password change on first login when seeded with defaults
    must_change_password = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self) -> str:
        return f"<AdminUser {self.username!r}>"


class Category(db.Model):
    """Product category (e.g. Espresso, Cold Brew, Pastries)."""

    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    display_order = db.Column(db.Integer, nullable=False, default=0)

    products = db.relationship(
        "Product", back_populates="category", lazy="select", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Category {self.name!r}>"


class Product(db.Model):
    """Menu item available for ordering."""

    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(
        db.Integer, db.ForeignKey("categories.id"), nullable=False, index=True
    )
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    # Stored in cents — e.g. 450 DA → 45000 cents
    price_cents = db.Column(db.Integer, nullable=False)
    image = db.Column(db.String(255), nullable=True)  # UUID-based filename
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    category = db.relationship("Category", back_populates="products")

    def __repr__(self) -> str:
        return f"<Product {self.name!r} {self.price_cents / 100:.2f} DA>"


class Order(db.Model):
    """A customer order placed at the kiosk."""

    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    # Client-generated UUID for idempotent syncing — unique across all kiosks
    local_id = db.Column(
        db.String(36),
        unique=True,
        nullable=False,
        index=True,
        default=lambda: str(uuid.uuid4()),
    )
    # Status lifecycle: pending → synced | failed
    status = db.Column(
        db.String(20),
        nullable=False,
        default="pending",
        index=True,
    )
    total_cents = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    synced_at = db.Column(db.DateTime(timezone=True), nullable=True)
    device_id = db.Column(db.String(100), nullable=True)

    items = db.relationship(
        "OrderItem",
        back_populates="order",
        lazy="select",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Order {self.local_id!r} status={self.status!r}>"


class OrderItem(db.Model):
    """A line item within an Order. Snapshots product name/price at time of sale."""

    __tablename__ = "order_items"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(
        db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True
    )
    product_id = db.Column(
        db.Integer, db.ForeignKey("products.id"), nullable=True, index=True
    )
    # Snapshot fields preserve historical data even if the product changes later
    product_name_snapshot = db.Column(db.String(150), nullable=False)
    unit_price_cents_snapshot = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    subtotal_cents = db.Column(db.Integer, nullable=False)

    order = db.relationship("Order", back_populates="items")
    product = db.relationship("Product")

    def __repr__(self) -> str:
        return (
            f"<OrderItem order={self.order_id} "
            f"product={self.product_name_snapshot!r} qty={self.quantity}>"
        )


class SyncLog(db.Model):
    """Audit log for every sync attempt (push orders / pull products)."""

    __tablename__ = "sync_logs"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    # direction: "push" (orders → server) | "pull" (products ← server)
    direction = db.Column(db.String(10), nullable=False)
    # status: "success" | "error"
    status = db.Column(db.String(10), nullable=False, index=True)
    detail = db.Column(db.Text, nullable=True)
    device_id = db.Column(db.String(100), nullable=True)

    def __repr__(self) -> str:
        return f"<SyncLog {self.direction!r} {self.status!r} @ {self.timestamp}>"


class SerialKey(db.Model):
    """
    Stores serial keys as SHA-256 hashes for security.
    Raw serials are NEVER stored in the database.
    """

    __tablename__ = "serial_keys"

    id = db.Column(db.Integer, primary_key=True)
    # SHA-256 hash of the raw serial string
    serial_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    label = db.Column(db.String(100), nullable=True)  # Human-readable name / device ID
    device_id = db.Column(db.String(100), nullable=True)  # Populated on activation
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    activated_at = db.Column(db.DateTime(timezone=True), nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self) -> str:
        return f"<SerialKey {self.label!r} active={self.is_active}>"
