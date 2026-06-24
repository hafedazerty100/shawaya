"""
routes/admin.py — Admin dashboard Blueprint.

All routes require login via Flask-Login. First login with default credentials
forces a password change before anything else can be accessed.
"""

import logging
import os
import secrets
import uuid
import requests
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

from extensions import db, csrf
from forms import (
    CategoryForm,
    ChangePasswordForm,
    LoginForm,
    ProductForm,
    SerialKeyGenerateForm,
)
from models import AdminUser, Category, Order, Product, SerialKey, SyncLog, OrderItem
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

    # ── Period filter ───────────────────────────────────────────────────────
    period = request.args.get("period", "").lower()
    date_str = request.args.get("date", "").strip()   # e.g. "2025-06-01"
    start_str = request.args.get("start_date", "").strip()
    end_str = request.args.get("end_date", "").strip()
    now_utc = datetime.now(timezone.utc)

    # First handle custom date range
    start_date = None
    end_date = None

    if start_str and end_str:
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_date = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
            period = "custom"
        except ValueError:
            pass
    elif date_str:
        try:
            start_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_date = start_date + timedelta(days=1)
            period = "day"
        except ValueError:
            pass
            
    # Fallback to period logic
    if not start_date:
        if period == "month":
            start_date = now_utc - timedelta(days=30)
            end_date = None
        elif period == "week":
            start_date = now_utc - timedelta(days=7)
            end_date = None
        elif period == "all":
            start_date = datetime.fromtimestamp(0, tz=timezone.utc)
            end_date = None
        else:
            # Default to today (current day)
            period = "today"
            start_date = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = None

    # ── Today's badges ──────────────────────────────────────────────────────
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    today_orders = Order.query.filter(Order.created_at >= today_start).count()
    today_revenue_cents = (
        db.session.query(func.sum(Order.total_cents))
        .filter(Order.created_at >= today_start)
        .scalar() or 0
    )
    today_revenue = format_price(today_revenue_cents)
    pending_count = Order.query.filter_by(status="pending").count()
    recent_logs = SyncLog.query.order_by(SyncLog.timestamp.desc()).limit(10).all()

    # ── Period / day stats ──────────────────────────────────────────────────
    base_q = Order.query.filter(Order.created_at >= start_date)
    if end_date:
        base_q = base_q.filter(Order.created_at < end_date)

    period_orders = base_q.count()
    period_revenue_cents = (
        db.session.query(func.sum(Order.total_cents))
        .filter(Order.created_at >= start_date,
                *([Order.created_at < end_date] if end_date else []))
        .scalar() or 0
    )
    period_revenue = format_price(period_revenue_cents)

    # ── Top selling ─────────────────────────────────────────────────────────
    top_q = (
        db.session.query(
            OrderItem.product_name_snapshot,
            func.sum(OrderItem.quantity).label("total_qty"),
            func.sum(OrderItem.subtotal_cents).label("total_sales"),
        )
        .join(Order)
        .filter(Order.created_at >= start_date)
    )
    if end_date:
        top_q = top_q.filter(Order.created_at < end_date)
    top_selling = (
        top_q.group_by(OrderItem.product_name_snapshot)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(5)
        .all()
    )
    formatted_top_selling = [
        {"name": r[0], "qty": r[1], "sales": format_price(r[2])}
        for r in top_selling
    ]

    # ── Daily breakdown ─────────────────────────────────────────────────────
    from sqlalchemy import cast, Date as SADate
    daily_q = (
        db.session.query(
            cast(Order.created_at, SADate).label("day"),
            func.count(Order.id).label("orders"),
            func.sum(Order.total_cents).label("revenue_cents"),
        )
        .filter(Order.created_at >= start_date)
    )
    if end_date:
        daily_q = daily_q.filter(Order.created_at < end_date)
    daily_breakdown = (
        daily_q.group_by(cast(Order.created_at, SADate))
        .order_by(cast(Order.created_at, SADate).desc())
        .all()
    )
    daily_rows = [
        {"day": str(r.day), "orders": r.orders, "revenue": format_price(r.revenue_cents or 0)}
        for r in daily_breakdown
    ]

    # Chart datasets
    chart_labels = [str(r.day) for r in reversed(daily_breakdown)]
    chart_revenue = [float(r.revenue_cents or 0) / 100 for r in reversed(daily_breakdown)]
    chart_orders = [int(r.orders or 0) for r in reversed(daily_breakdown)]

    # System Diagnostics
    import platform
    import os
    db_size = "Unknown"
    try:
        db_uri = db.engine.url.database
        if db_uri and os.path.exists(db_uri):
            db_size = f"{os.path.getsize(db_uri) / (1024 * 1024):.2f} MB"
        else:
            from sqlalchemy import text
            res = db.session.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))")).scalar()
            if res:
                db_size = res
    except Exception:
        pass

    sync_total = SyncLog.query.count()
    sync_success = SyncLog.query.filter_by(status="success").count()
    sync_rate = f"{(sync_success / sync_total * 100):.1f}%" if sync_total > 0 else "100%"

    diagnostics = {
        "db_engine": db.engine.name.upper(),
        "db_size": db_size,
        "os": platform.system(),
        "sync_rate": sync_rate,
        "active_devices": SerialKey.query.filter_by(is_active=True).count()
    }

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
        daily_rows=daily_rows,
        date_str=date_str,
        start_str=start_str,
        end_str=end_str,
        chart_labels=chart_labels,
        chart_revenue=chart_revenue,
        chart_orders=chart_orders,
        diagnostics=diagnostics,
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
    cat = db.get_or_404(Category, cat_id)
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
            image_data = None
            image_mime = None
            
            if form.image.data and form.image.data.filename:
                image_data = form.image.data.read()
                image_mime = form.image.data.mimetype
                image_filename = str(uuid.uuid4()) + ".jpg"
            elif form.image_url.data:
                try:
                    resp = requests.get(form.image_url.data, timeout=5)
                    resp.raise_for_status()
                    image_data = resp.content
                    image_mime = resp.headers.get("Content-Type", "image/jpeg")
                    image_filename = str(uuid.uuid4()) + ".jpg"
                except Exception as exc:
                    flash(f"Could not download image from URL: {exc}", "warning")

            product = Product(
                name=form.name.data.strip(),
                description=form.description.data.strip() if form.description.data else "",
                category_id=form.category_id.data,
                price_cents=da_to_cents(float(form.price.data.replace(",", "."))),
                image=image_filename,
                image_data=image_data,
                image_mime=image_mime,
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
    if isinstance(form.image.data, str):
        form.image.data = None
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
                product.image_data = form.image.data.read()
                product.image_mime = form.image.data.mimetype
                product.image = str(uuid.uuid4()) + ".jpg"
            elif form.image_url.data:
                try:
                    resp = requests.get(form.image_url.data, timeout=5)
                    resp.raise_for_status()
                    product.image_data = resp.content
                    product.image_mime = resp.headers.get("Content-Type", "image/jpeg")
                    product.image = str(uuid.uuid4()) + ".jpg"
                except Exception as exc:
                    flash(f"Could not download image from URL: {exc}", "warning")

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
    start_str = request.args.get("start_date", "").strip()
    end_str = request.args.get("end_date", "").strip()
    page = request.args.get("page", 1, type=int)

    query = Order.query
    if status_filter in ("pending", "synced", "failed"):
        query = query.filter_by(status=status_filter)

    if start_str:
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            query = query.filter(Order.created_at >= start_date)
        except ValueError:
            pass

    if end_str:
        try:
            end_date = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            from datetime import timedelta
            query = query.filter(Order.created_at < end_date + timedelta(days=1))
        except ValueError:
            pass

    pagination = query.order_by(Order.created_at.desc()).paginate(
        page=page, per_page=ITEMS_PER_PAGE, error_out=False
    )
    return render_template(
        "admin/orders.html",
        orders=pagination.items,
        pagination=pagination,
        status_filter=status_filter,
        start_str=start_str,
        end_str=end_str,
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


# ─── Super-Admin & Advanced Control Panel ───────────────────────────────────

@admin_bp.route("/orders/export")
@login_required
def export_orders():
    import csv
    import io
    from flask import Response

    start_str = request.args.get("start_date", "").strip()
    end_str = request.args.get("end_date", "").strip()

    query = Order.query
    if start_str:
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            query = query.filter(Order.created_at >= start_date)
        except ValueError:
            pass
    if end_str:
        try:
            end_date = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            from datetime import timedelta
            query = query.filter(Order.created_at < end_date + timedelta(days=1))
        except ValueError:
            pass

    orders = query.order_by(Order.created_at.desc()).all()

    dest = io.StringIO()
    writer = csv.writer(dest)
    writer.writerow(["ID", "Local ID", "Status", "Total (DA)", "Created At", "Synced At", "Device ID"])

    for o in orders:
        writer.writerow([
            o.id,
            o.local_id,
            o.status,
            f"{o.total_cents / 100:.2f}",
            o.created_at.strftime("%Y-%m-%d %H:%M:%S") if o.created_at else "",
            o.synced_at.strftime("%Y-%m-%d %H:%M:%S") if o.synced_at else "",
            o.device_id or ""
        ])

    output = dest.getvalue()
    dest.close()

    bom_output = "\ufeff" + output
    filename = f"orders_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        bom_output.encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )


@admin_bp.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
@login_required
def edit_order(order_id: int):
    order = db.get_or_404(Order, order_id)
    products = Product.query.filter_by(is_active=True).order_by(Product.name).all()

    if request.method == "POST":
        try:
            # Edit metadata
            status = request.form.get("status")
            device_id = request.form.get("device_id")
            created_at_str = request.form.get("created_at")

            if status in ("pending", "synced", "failed"):
                order.status = status
            order.device_id = device_id or None

            if created_at_str:
                # Expecting format 'YYYY-MM-DDTHH:MM' from datetime-local input
                naive_dt = datetime.strptime(created_at_str, "%Y-%m-%dT%H:%M")
                order.created_at = naive_dt.replace(tzinfo=timezone.utc)

            db.session.commit()
            flash("Order metadata updated successfully.", "success")
            return redirect(url_for("admin.edit_order", order_id=order.id))
        except Exception as exc:
            db.session.rollback()
            logger.error("Failed to update order metadata: %s", exc)
            flash("Failed to update order metadata. Check format.", "danger")

    return render_template("admin/order_edit.html", order=order, products=products)


@admin_bp.route("/orders/<int:order_id>/delete", methods=["POST"])
@login_required
def delete_order(order_id: int):
    order = db.get_or_404(Order, order_id)
    try:
        db.session.delete(order)
        db.session.commit()
        flash(f"Order #{order_id} deleted successfully.", "success")
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to delete order %s: %s", order_id, exc)
        flash("Failed to delete order.", "danger")
    return redirect(url_for("admin.orders"))


@admin_bp.route("/orders/<int:order_id>/items/update", methods=["POST"])
@login_required
def update_order_items(order_id: int):
    order = db.get_or_404(Order, order_id)
    try:
        # 1. Update existing items
        for item in list(order.items):
            qty_key = f"qty_{item.id}"
            price_key = f"price_{item.id}"

            if request.form.get(f"delete_{item.id}") == "1":
                db.session.delete(item)
                continue

            if qty_key in request.form:
                qty = int(request.form[qty_key])
                if qty <= 0:
                    db.session.delete(item)
                    continue
                item.quantity = qty

            if price_key in request.form:
                price_val = float(request.form[price_key].replace(",", "."))
                item.unit_price_cents_snapshot = da_to_cents(price_val)

            item.subtotal_cents = item.quantity * item.unit_price_cents_snapshot

        # 2. Add new item
        new_prod_id = request.form.get("new_product_id")
        if new_prod_id:
            new_prod_id = int(new_prod_id)
            new_qty = int(request.form.get("new_quantity", 1))
            if new_qty > 0:
                prod = db.get_or_404(Product, new_prod_id)
                new_item = OrderItem(
                    order_id=order.id,
                    product_id=prod.id,
                    product_name_snapshot=prod.name,
                    unit_price_cents_snapshot=prod.price_cents,
                    quantity=new_qty,
                    subtotal_cents=prod.price_cents * new_qty
                )
                db.session.add(new_item)
                order.items.append(new_item)

        db.session.flush()

        # 3. Recalculate total_cents
        total = 0
        for item in order.items:
            if item not in db.session.deleted:
                total += item.subtotal_cents
        order.total_cents = total

        db.session.commit()
        flash("Order items updated successfully.", "success")
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to update order items: %s", exc)
        flash("Failed to update order items. Check values.", "danger")

    return redirect(url_for("admin.edit_order", order_id=order.id))


@admin_bp.route("/db-browser")
@login_required
def db_browser():
    table_name = request.args.get("table", "products").lower()
    page = request.args.get("page", 1, type=int)
    search = request.args.get("q", "").strip()

    models_map = {
        "products": Product,
        "categories": Category,
        "orders": Order,
        "order_items": OrderItem,
        "serial_keys": SerialKey,
        "sync_logs": SyncLog,
        "admin_users": AdminUser,
    }

    if table_name not in models_map:
        table_name = "products"

    model = models_map[table_name]
    query = model.query

    if search:
        if table_name == "products":
            query = query.filter(Product.name.ilike(f"%{search}%") | Product.description.ilike(f"%{search}%"))
        elif table_name == "categories":
            query = query.filter(Category.name.ilike(f"%{search}%"))
        elif table_name == "orders":
            query = query.filter(Order.local_id.ilike(f"%{search}%") | Order.device_id.ilike(f"%{search}%") | Order.status.ilike(f"%{search}%"))
        elif table_name == "order_items":
            query = query.filter(OrderItem.product_name_snapshot.ilike(f"%{search}%"))
        elif table_name == "serial_keys":
            query = query.filter(SerialKey.label.ilike(f"%{search}%") | SerialKey.device_id.ilike(f"%{search}%"))
        elif table_name == "sync_logs":
            query = query.filter(SyncLog.detail.ilike(f"%{search}%") | SyncLog.device_id.ilike(f"%{search}%"))
        elif table_name == "admin_users":
            query = query.filter(AdminUser.username.ilike(f"%{search}%"))

    if hasattr(model, "id"):
        query = query.order_by(model.id.desc())
    elif hasattr(model, "timestamp"):
        query = query.order_by(model.timestamp.desc())

    pagination = query.paginate(page=page, per_page=ITEMS_PER_PAGE, error_out=False)
    columns = [col.name for col in model.__table__.columns]

    return render_template(
        "admin/db_browser.html",
        table_name=table_name,
        tables=list(models_map.keys()),
        columns=columns,
        items=pagination.items,
        pagination=pagination,
        search=search,
    )


@admin_bp.route("/db-browser/<string:table_name>/<int:row_id>/delete", methods=["POST"])
@login_required
def db_delete_row(table_name: str, row_id: int):
    models_map = {
        "products": Product,
        "categories": Category,
        "orders": Order,
        "order_items": OrderItem,
        "serial_keys": SerialKey,
        "sync_logs": SyncLog,
        "admin_users": AdminUser,
    }
    if table_name not in models_map:
        flash("Invalid table name.", "danger")
        return redirect(url_for("admin.db_browser"))

    model = models_map[table_name]
    row = db.get_or_404(model, row_id)
    try:
        db.session.delete(row)
        db.session.commit()
        flash(f"Row {row_id} deleted successfully from {table_name}.", "success")
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to delete row %s from %s: %s", row_id, table_name, exc)
        flash("Failed to delete row due to database constraints.", "danger")

    return redirect(url_for("admin.db_browser", table=table_name))


@admin_bp.route("/sync-databases", methods=["POST"])
@login_required
@csrf.exempt
def sync_databases():
    """Manually trigger database replication across all 3 databases with push/pull strategies."""
    from flask import jsonify, request
    from db_sync import replicate_databases
    
    strategy = "push"
    if request.is_json:
        strategy = request.json.get("strategy", "push")
    else:
        strategy = request.args.get("strategy", "push")
    if strategy not in ["push", "pull"]:
        strategy = "push"
        
    try:
        res = replicate_databases(strategy=strategy)
        if res is None:
            res = {"success": True}
        if res.get("success"):
            return jsonify({
                "success": True, 
                "message": "تمت مزامنة قواعد البيانات بنجاح.",
                "details": res
            }), 200
        else:
            msg = res.get("message") if res else "فشلت المزامنة لجميع قواعد البيانات."
            return jsonify({
                "success": False, 
                "message": msg,
                "details": res
            }), 200
    except Exception as exc:
        logger.error("Manual database sync failed: %s", exc)
        return jsonify({"success": False, "message": f"فشلت المزامنة: {str(exc)}"}), 500


def mask_db_url(url: str) -> str:
    """Mask credentials in database connection string."""
    try:
        from sqlalchemy.engine import make_url
        parsed = make_url(url)
        password = "***" if parsed.password else None
        return f"{parsed.drivername}://{parsed.username}:{password}@{parsed.host}/{parsed.database}"
    except Exception:
        if "@" in url:
            parts = url.split("@")
            prefix = parts[0]
            suffix = parts[1]
            if ":" in prefix:
                prefix_parts = prefix.split(":")
                return f"{prefix_parts[0]}:{prefix_parts[1]}:***@{suffix}"
        return url


# Module-level variables for configuration file management (supports test monkeypatching)
DB_CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
urls_file = os.path.join(DB_CONFIG_DIR, "db_urls.json")


def commit_and_push_db_urls():
    """Commit the updated db_urls.json file and push it to GitHub."""
    import subprocess
    import os
    import logging

    logger = logging.getLogger("routes.admin.db_git")
    try:
        if not os.path.exists(urls_file):
            logger.warning("db_urls.json not found in %s", DB_CONFIG_DIR)
            return False
            
        # Configure git identity locally
        subprocess.run(["git", "config", "user.name", "Shawaya POS Server"], cwd=DB_CONFIG_DIR, capture_output=True)
        subprocess.run(["git", "config", "user.email", "server@shawaya.local"], cwd=DB_CONFIG_DIR, capture_output=True)
        
        # Add and commit
        subprocess.run(["git", "add", "db_urls.json"], cwd=DB_CONFIG_DIR, capture_output=True)
        res = subprocess.run(["git", "commit", "-m", "chore: add new database URL dynamically"], cwd=DB_CONFIG_DIR, capture_output=True, text=True)
        
        if "nothing to commit" in res.stdout or "no changes added to commit" in res.stdout:
            logger.info("No changes to commit for db_urls.json")
            return True
            
        # Push to remote
        github_token = os.environ.get("GITHUB_TOKEN")
        if github_token:
            remote_url = f"https://{github_token}@github.com/hafedazerty100/shawaya.git"
            push_res = subprocess.run(["git", "push", remote_url, "main"], cwd=DB_CONFIG_DIR, capture_output=True, text=True)
            logger.info("Git push with token result: %s", push_res.stdout)
        else:
            push_res = subprocess.run(["git", "push", "origin", "main"], cwd=DB_CONFIG_DIR, capture_output=True, text=True)
            logger.info("Git push standard result: %s", push_res.stdout)
            
        return True
    except Exception as exc:
        logger.exception("Failed to commit and push db_urls.json: %s", exc)
        return False


@admin_bp.route("/db-settings", methods=["GET", "POST"])
@login_required
def db_settings():
    """Manage multi-database connections and add new connection strings dynamically."""
    from extensions import DB_URLS
    from flask import current_app
    import json
    import os
    from sqlalchemy import create_engine, text

    if request.method == "POST":
        new_url = request.form.get("db_url", "").strip()
        if not new_url:
            flash("يرجى إدخال عنوان قاعدة البيانات.", "danger")
            return redirect(url_for("admin.db_settings"))

        if not (new_url.startswith("postgresql://") or new_url.startswith("postgres://")):
            flash("عنوان قاعدة البيانات غير صالح. يجب أن يبدأ بـ postgresql://", "danger")
            return redirect(url_for("admin.db_settings"))

        # Reformat postgres:// to postgresql:// if needed for SQLAlchemy compatibility
        formatted_url = new_url
        if formatted_url.startswith("postgres://"):
            formatted_url = "postgresql://" + formatted_url[len("postgres://"):]

        # Verify reachability & initialize schema
        try:
            # 1. Connectivity check
            engine = create_engine(formatted_url, connect_args={"connect_timeout": 5})
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            
            # 2. Re-create all tables
            from extensions import db
            db.metadata.create_all(bind=engine)
            
            # 3. Seed default admin if missing
            with engine.connect() as conn:
                res = conn.execute(text("SELECT COUNT(*) FROM admin_users"))
                count = res.scalar()
                if count == 0:
                    from werkzeug.security import generate_password_hash
                    username = current_app.config.get("ADMIN_DEFAULT_USERNAME", "admin")
                    password = current_app.config.get("ADMIN_DEFAULT_PASSWORD", "changeme123")
                    pw_hash = generate_password_hash(password)
                    conn.execute(
                        text("INSERT INTO admin_users (username, password_hash, must_change_password) VALUES (:u, :p, :m)"),
                        {"u": username, "p": pw_hash, "m": (password == "changeme123")}
                    )
                    conn.commit()
            
            engine.dispose()
        except Exception as exc:
            logger.error("Failed to connect or initialize new database: %s", exc)
            flash(f"فشل الاتصال بقاعدة البيانات الجديدة أو تهيئتها: {str(exc)}", "danger")
            return redirect(url_for("admin.db_settings"))

        # Add to list and save if not already present
        if formatted_url not in DB_URLS:
            DB_URLS.append(formatted_url)
            
        try:
            with open(urls_file, "w", encoding="utf-8") as f:
                json.dump(DB_URLS, f, indent=2)
        except Exception as exc:
            logger.error("Failed to write to db_urls.json: %s", exc)
            flash("فشل حفظ إعدادات قاعدة البيانات محلياً.", "danger")
            return redirect(url_for("admin.db_settings"))

        # Git commit and push
        pushed = commit_and_push_db_urls()
        if pushed:
            flash("تمت إضافة قاعدة البيانات بنجاح! جاري إعادة بناء ونشر المشروع لتفعيلها...", "success")
        else:
            flash("تم حفظ قاعدة البيانات محلياً، ولكن فشل دفع التحديثات إلى مستودع Git.", "warning")

        return redirect(url_for("admin.db_settings"))

    # Masked URLs for display
    masked_urls = [mask_db_url(url) for url in DB_URLS]
    return render_template("admin/db_settings.html", urls=masked_urls)

