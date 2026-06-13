"""
routes/admin.py — Admin dashboard Blueprint.

All routes require login via Flask-Login. First login with default credentials
forces a password change before anything else can be accessed.
"""

import logging
import secrets
from datetime import datetime, timezone

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db
from forms import (
    CategoryForm,
    ChangePasswordForm,
    LoginForm,
    ProductForm,
    SerialKeyGenerateForm,
)
from models import AdminUser, Category, Order, Product, SerialKey, SyncLog
from utils import (
    delete_product_image,
    da_to_cents,
    format_price,
    hash_serial,
    save_product_image,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
logger = logging.getLogger("routes.admin")

ITEMS_PER_PAGE = 20


# ─── Auth ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = AdminUser.query.filter_by(username=form.username.data).first()
        if user and check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            logger.info("Admin login: %s from %s", user.username, request.remote_addr)
            if user.must_change_password:
                flash("Please change your password before continuing.", "warning")
                return redirect(url_for("admin.change_password"))
            next_page = request.args.get("next")
            return redirect(next_page or url_for("admin.dashboard"))
        else:
            logger.warning(
                "Failed login attempt for '%s' from %s",
                form.username.data,
                request.remote_addr,
            )
            flash("Invalid username or password.", "danger")

    return render_template("admin/login.html", form=form)


@admin_bp.route("/logout")
@login_required
def logout():
    logger.info("Admin logout: %s", current_user.username)
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("admin.login"))


@admin_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not check_password_hash(current_user.password_hash, form.current_password.data):
            flash("Current password is incorrect.", "danger")
        else:
            try:
                current_user.password_hash = generate_password_hash(form.new_password.data)
                current_user.must_change_password = False
                db.session.commit()
                flash("Password changed successfully.", "success")
                return redirect(url_for("admin.dashboard"))
            except Exception as exc:
                db.session.rollback()
                logger.error("Password change failed: %s", exc)
                flash("An error occurred. Please try again.", "danger")
    return render_template("admin/change_password.html", form=form)


# ─── Before-request: force password change ───────────────────────────────────

@admin_bp.before_request
def force_password_change():
    """Redirect to change-password if the flag is set, blocking all other pages."""
    if (
        current_user.is_authenticated
        and current_user.must_change_password
        and request.endpoint not in ("admin.change_password", "admin.logout")
    ):
        return redirect(url_for("admin.change_password"))


# ─── Dashboard ────────────────────────────────────────────────────────────────

@admin_bp.route("/dashboard")
@login_required
def dashboard():
    from sqlalchemy import func
    from datetime import timedelta
    from models import OrderItem

    # Determine filter period
    period = request.args.get("period", "week").lower()
    now_utc = datetime.now(timezone.utc)

    if period == "today":
        start_date = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start_date = now_utc - timedelta(days=30)
    elif period == "all":
        start_date = datetime.fromtimestamp(0, tz=timezone.utc)
    else:
        period = "week"
        start_date = now_utc - timedelta(days=7)

    # General today's stats for top badges
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    today_orders = Order.query.filter(Order.created_at >= today_start).count()
    today_revenue_cents = db.session.query(func.sum(Order.total_cents)).filter(Order.created_at >= today_start).scalar() or 0
    today_revenue = format_price(today_revenue_cents)

    pending_count = Order.query.filter_by(status="pending").count()
    recent_logs = (
        SyncLog.query.order_by(SyncLog.timestamp.desc()).limit(10).all()
    )

    # Period statistics
    period_orders = Order.query.filter(Order.created_at >= start_date).count()
    period_revenue_cents = db.session.query(func.sum(Order.total_cents)).filter(Order.created_at >= start_date).scalar() or 0
    period_revenue = format_price(period_revenue_cents)

    # Top selling products in this period
    top_selling = (
        db.session.query(
            OrderItem.product_name_snapshot,
            func.sum(OrderItem.quantity).label("total_qty"),
            func.sum(OrderItem.subtotal_cents).label("total_sales")
        )
        .join(Order)
        .filter(Order.created_at >= start_date)
        .group_by(OrderItem.product_name_snapshot)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(5)
        .all()
    )

    formatted_top_selling = [
        {
            "name": item[0],
            "qty": item[1],
            "sales": format_price(item[2])
        }
        for item in top_selling
    ]

    return render_template(
        "admin/dashboard.html",
        today_orders=today_orders,
        today_revenue=today_revenue,
        pending_count=pending_count,
        recent_logs=recent_logs,
        period=period,
        period_orders=period_orders,
        period_revenue=period_revenue,
        top_selling=formatted_top_selling,
    )


# ─── Categories ───────────────────────────────────────────────────────────────

@admin_bp.route("/categories", methods=["GET", "POST"])
@login_required
def categories():
    form = CategoryForm()
    if form.validate_on_submit():
        try:
            cat = Category(
                name=form.name.data.strip(),
                display_order=form.display_order.data,
            )
            db.session.add(cat)
            db.session.commit()
            flash(f"Category '{cat.name}' created.", "success")
            return redirect(url_for("admin.categories"))
        except Exception as exc:
            db.session.rollback()
            logger.error("Category create failed: %s", exc)
            flash("Failed to create category.", "danger")

    all_cats = Category.query.order_by(Category.display_order, Category.name).all()
    return render_template("admin/categories.html", form=form, categories=all_cats)


@admin_bp.route("/categories/<int:cat_id>/edit", methods=["GET", "POST"])
@login_required
def edit_category(cat_id: int):
    cat = db.get_or_404(Category, cat_id)
    form = CategoryForm(obj=cat)
    if form.validate_on_submit():
        try:
            cat.name = form.name.data.strip()
            cat.display_order = form.display_order.data
            db.session.commit()
            flash(f"Category '{cat.name}' updated.", "success")
            return redirect(url_for("admin.categories"))
        except Exception as exc:
            db.session.rollback()
            logger.error("Category update failed: %s", exc)
            flash("Failed to update category.", "danger")
    return render_template("admin/categories.html", form=form, edit_cat=cat,
                           categories=Category.query.order_by(Category.display_order).all())


@admin_bp.route("/categories/<int:cat_id>/delete", methods=["POST"])
@login_required
def delete_category(cat_id: int):
    cat = Category.query.get_or_404(cat_id)
    try:
        db.session.delete(cat)
        db.session.commit()
        flash(f"Category '{cat.name}' deleted.", "success")
    except Exception as exc:
        db.session.rollback()
        logger.error("Category delete failed: %s", exc)
        flash("Cannot delete category — it may have products.", "danger")
    return redirect(url_for("admin.categories"))


# ─── Products ─────────────────────────────────────────────────────────────────

@admin_bp.route("/products")
@login_required
def products():
    search = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    query = Product.query.join(Category)
    if search:
        query = query.filter(Product.name.ilike(f"%{search}%"))
    pagination = query.order_by(Category.display_order, Product.name).paginate(
        page=page, per_page=ITEMS_PER_PAGE, error_out=False
    )
    return render_template(
        "admin/products.html",
        products=pagination.items,
        pagination=pagination,
        search=search,
    )


@admin_bp.route("/products/new", methods=["GET", "POST"])
@login_required
def new_product():
    form = ProductForm()
    form.category_id.choices = [
        (c.id, c.name)
        for c in Category.query.order_by(Category.display_order, Category.name).all()
    ]
    if form.validate_on_submit():
        try:
            image_filename = None
            if form.image.data and form.image.data.filename:
                image_filename = save_product_image(form.image.data)

            product = Product(
                name=form.name.data.strip(),
                description=form.description.data.strip() if form.description.data else "",
                category_id=form.category_id.data,
                price_cents=da_to_cents(float(form.price.data.replace(",", "."))),
                image=image_filename,
                is_active=form.is_active.data,
            )
            db.session.add(product)
            db.session.commit()
            flash(f"Product '{product.name}' created.", "success")
            return redirect(url_for("admin.products"))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            db.session.rollback()
            logger.error("Product create failed: %s", exc)
            flash("Failed to create product.", "danger")
    return render_template("admin/product_form.html", form=form, title="New Product")


@admin_bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def edit_product(product_id: int):
    product = db.get_or_404(Product, product_id)
    form = ProductForm(obj=product)
    form.category_id.choices = [
        (c.id, c.name)
        for c in Category.query.order_by(Category.display_order, Category.name).all()
    ]
    # Pre-populate price field (stored as cents, display as DA decimal)
    if request.method == "GET":
        form.price.data = f"{product.price_cents / 100:.2f}"

    if form.validate_on_submit():
        try:
            if form.image.data and form.image.data.filename:
                old_image = product.image
                product.image = save_product_image(form.image.data)
                if old_image:
                    delete_product_image(old_image)

            product.name = form.name.data.strip()
            product.description = form.description.data.strip() if form.description.data else ""
            product.category_id = form.category_id.data
            product.price_cents = da_to_cents(float(form.price.data.replace(",", ".")))
            product.is_active = form.is_active.data
            db.session.commit()
            flash(f"Product '{product.name}' updated.", "success")
            return redirect(url_for("admin.products"))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            db.session.rollback()
            logger.error("Product update failed: %s", exc)
            flash("Failed to update product.", "danger")
    return render_template(
        "admin/product_form.html", form=form, product=product, title="Edit Product"
    )


@admin_bp.route("/products/<int:product_id>/delete", methods=["POST"])
@login_required
def delete_product(product_id: int):
    product = db.get_or_404(Product, product_id)
    try:
        if product.image:
            delete_product_image(product.image)
        db.session.delete(product)
        db.session.commit()
        flash(f"Product '{product.name}' deleted.", "success")
    except Exception as exc:
        db.session.rollback()
        logger.error("Product delete failed: %s", exc)
        flash("Failed to delete product.", "danger")
    return redirect(url_for("admin.products"))


# ─── Orders ───────────────────────────────────────────────────────────────────

@admin_bp.route("/orders")
@login_required
def orders():
    status_filter = request.args.get("status", "").strip()
    page = request.args.get("page", 1, type=int)
    query = Order.query
    if status_filter in ("pending", "synced", "failed"):
        query = query.filter_by(status=status_filter)
    pagination = query.order_by(Order.created_at.desc()).paginate(
        page=page, per_page=ITEMS_PER_PAGE, error_out=False
    )
    return render_template(
        "admin/orders.html",
        orders=pagination.items,
        pagination=pagination,
        status_filter=status_filter,
    )


@admin_bp.route("/orders/<int:order_id>")
@login_required
def order_detail(order_id: int):
    order = db.get_or_404(Order, order_id)
    return render_template("admin/order_detail.html", order=order)


# ─── Sync Log ─────────────────────────────────────────────────────────────────

@admin_bp.route("/sync_log")
@login_required
def sync_log():
    direction_filter = request.args.get("direction", "").strip()
    status_filter = request.args.get("status", "").strip()
    page = request.args.get("page", 1, type=int)
    query = SyncLog.query
    if direction_filter in ("push", "pull"):
        query = query.filter_by(direction=direction_filter)
    if status_filter in ("success", "error"):
        query = query.filter_by(status=status_filter)
    pagination = query.order_by(SyncLog.timestamp.desc()).paginate(
        page=page, per_page=ITEMS_PER_PAGE, error_out=False
    )
    return render_template(
        "admin/sync_log.html",
        logs=pagination.items,
        pagination=pagination,
        direction_filter=direction_filter,
        status_filter=status_filter,
    )


# ─── Serial Keys ──────────────────────────────────────────────────────────────

@admin_bp.route("/serial_keys", methods=["GET", "POST"])
@login_required
def serial_keys():
    form = SerialKeyGenerateForm()
    generated_serials = []

    if form.validate_on_submit():
        quantity = form.quantity.data or 1
        label = form.label.data.strip() if form.label.data else ""
        expires_at = None
        if form.expires_at.data:
            expires_at = datetime.combine(
                form.expires_at.data, datetime.min.time()
            ).replace(tzinfo=timezone.utc)

        try:
            for i in range(quantity):
                raw_serial = secrets.token_urlsafe(24)  # Cryptographically random
                serial_key = SerialKey(
                    serial_hash=hash_serial(raw_serial),
                    label=f"{label} #{i+1}" if label and quantity > 1 else label,
                    is_active=False,
                    expires_at=expires_at,
                )
                db.session.add(serial_key)
                generated_serials.append(raw_serial)

            db.session.commit()
            flash(
                f"Generated {quantity} serial key(s). "
                "Copy them now — they cannot be retrieved later!",
                "success",
            )
        except Exception as exc:
            db.session.rollback()
            logger.error("Serial key generation failed: %s", exc)
            flash("Failed to generate serial keys.", "danger")

    all_keys = SerialKey.query.order_by(SerialKey.created_at.desc()).all()
    return render_template(
        "admin/serial_keys.html",
        form=form,
        serial_keys=all_keys,
        generated_serials=generated_serials,
    )


@admin_bp.route("/serial_keys/<int:key_id>/revoke", methods=["POST"])
@login_required
def revoke_serial_key(key_id: int):
    key = db.get_or_404(SerialKey, key_id)
    try:
        key.is_active = False
        key.device_id = None
        db.session.commit()
        flash(f"Serial key '{key.label or key.id}' revoked.", "success")
    except Exception as exc:
        db.session.rollback()
        logger.error("Serial key revoke failed: %s", exc)
        flash("Failed to revoke serial key.", "danger")
    return redirect(url_for("admin.serial_keys"))
