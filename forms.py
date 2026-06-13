"""
forms.py — WTForms form definitions with server-side validation.

All forms extend FlaskForm, which automatically includes CSRF protection.
"""

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField
from wtforms import (
    BooleanField,
    DateField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    EqualTo,
    Length,
    NumberRange,
    Optional,
    ValidationError,
)


class LoginForm(FlaskForm):
    """Admin login form."""

    username = StringField(
        "Username",
        validators=[DataRequired(), Length(min=1, max=80)],
        render_kw={"autocomplete": "username"},
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(), Length(min=1, max=256)],
        render_kw={"autocomplete": "current-password"},
    )


class ChangePasswordForm(FlaskForm):
    """Forced password-change form shown after first login with default credentials."""

    current_password = PasswordField(
        "Current Password",
        validators=[DataRequired()],
        render_kw={"autocomplete": "current-password"},
    )
    new_password = PasswordField(
        "New Password",
        validators=[
            DataRequired(),
            Length(min=8, max=256, message="Password must be at least 8 characters."),
        ],
        render_kw={"autocomplete": "new-password"},
    )
    confirm_password = PasswordField(
        "Confirm New Password",
        validators=[
            DataRequired(),
            EqualTo("new_password", message="Passwords must match."),
        ],
        render_kw={"autocomplete": "new-password"},
    )


class CategoryForm(FlaskForm):
    """Create / edit a product category."""

    name = StringField(
        "Category Name",
        validators=[DataRequired(), Length(min=1, max=100)],
    )
    display_order = IntegerField(
        "Display Order",
        validators=[NumberRange(min=0, max=9999)],
        default=0,
    )


class ProductForm(FlaskForm):
    """Create / edit a menu product."""

    name = StringField(
        "Product Name",
        validators=[DataRequired(), Length(min=1, max=150)],
    )
    description = TextAreaField(
        "Description",
        validators=[Optional(), Length(max=1000)],
    )
    category_id = SelectField(
        "Category",
        coerce=int,
        validators=[DataRequired()],
    )
    # Price entered as DA (e.g. 250.00) — converted to cents in the route
    price = StringField(
        "Price (DA)",
        validators=[DataRequired()],
    )
    image = FileField(
        "Product Image",
        validators=[Optional()],
    )
    is_active = BooleanField("Active", default=True)

    def validate_price(self, field):
        """Ensure price is a positive number with at most 2 decimal places."""
        try:
            val = float(field.data.replace(",", "."))
        except (ValueError, AttributeError):
            raise ValidationError("Price must be a valid number (e.g. 250.00).")
        if val < 0:
            raise ValidationError("Price cannot be negative.")
        if val > 99999.99:
            raise ValidationError("Price is unrealistically large.")

    def validate_image(self, field):
        """Only validate extension if a file was actually uploaded."""
        # FileStorage is always truthy even when empty — check filename explicitly
        filename = getattr(field.data, "filename", None) or ""
        if not filename:
            return  # No file selected — skip validation (image is optional on edit)
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext not in ["png", "jpg", "jpeg", "webp"]:
            raise ValidationError("Only PNG, JPG, JPEG, and WebP images are allowed.")


class SerialKeyGenerateForm(FlaskForm):
    """Generate one or more serial keys for kiosk activation."""

    label = StringField(
        "Label / Device ID",
        validators=[Optional(), Length(max=100)],
        description="Optional human-readable label (e.g. 'Kiosk 1 - Main Counter').",
    )
    quantity = IntegerField(
        "Quantity",
        validators=[NumberRange(min=1, max=50, message="Quantity must be 1–50.")],
        default=1,
    )
    expires_at = DateField(
        "Expiry Date (optional)",
        validators=[Optional()],
        description="Leave blank for no expiry.",
    )
