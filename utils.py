"""
utils.py — Shared helper functions.

Covers:
  - Price formatting (cents → display string)
  - HMAC-based activation token generation and validation
  - Secure image saving (Pillow validation + resize + UUID filename)
  - Serial key hashing (SHA-256)
  - API-key-protected route decorator
"""

import hashlib
import hmac
import io
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import current_app, jsonify, request
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

# ─── Allowed image extensions and MIME types ─────────────────────────────────
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
ALLOWED_PIL_FORMATS = {"PNG", "JPEG", "WEBP"}
MAX_IMAGE_WIDTH = 1024  # pixels — resize wider images


# ─── Price helpers ────────────────────────────────────────────────────────────

def format_price(cents: int) -> str:
    """Convert an integer cent value to a display string, e.g. 450 → '$4.50'."""
    return f"${cents / 100:.2f}"


def dollars_to_cents(dollars: float) -> int:
    """Convert a dollar float (from user input) to integer cents."""
    return round(dollars * 100)


# ─── Serial key hashing ───────────────────────────────────────────────────────

def hash_serial(raw_serial: str) -> str:
    """Return the SHA-256 hex digest of a raw serial string."""
    return hashlib.sha256(raw_serial.strip().encode("utf-8")).hexdigest()


# ─── HMAC activation tokens ───────────────────────────────────────────────────

def _hmac_key() -> bytes:
    """Return the SECRET_KEY as bytes for HMAC operations."""
    return current_app.config["SECRET_KEY"].encode("utf-8")


def generate_activation_token(serial_hash: str, device_id: str) -> str:
    """
    Generate a signed activation token the desktop stores locally.

    Format: <serial_hash>:<device_id>:<timestamp>:<hmac>
    """
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    payload = f"{serial_hash}:{device_id}:{timestamp}"
    sig = hmac.new(_hmac_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def validate_activation_token(token: str) -> bool:
    """
    Validate the HMAC signature of a locally stored activation token.
    Returns True if the token is valid; False otherwise.
    """
    if not token:
        return False
    try:
        parts = token.rsplit(":", 1)
        if len(parts) != 2:
            return False
        payload, provided_sig = parts
        expected_sig = hmac.new(
            _hmac_key(), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected_sig, provided_sig)
    except Exception:
        logger.warning("Token validation raised an exception", exc_info=True)
        return False


def extract_token_device_id(token: str) -> str | None:
    """Pull the device_id out of a valid activation token."""
    try:
        parts = token.rsplit(":", 1)[0].split(":")
        # parts = [serial_hash, device_id, timestamp]
        if len(parts) >= 3:
            return parts[1]
    except Exception:
        pass
    return None


# ─── Secure image upload ──────────────────────────────────────────────────────

def allowed_image_extension(filename: str) -> bool:
    """Check that the filename has an allowed image extension."""
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def save_product_image(file_storage) -> str | None:
    """
    Validate, resize, and save an uploaded product image.

    Args:
        file_storage: A werkzeug FileStorage object from request.files.

    Returns:
        The saved UUID-based filename (e.g. 'abc123.jpg'), or None on failure.

    Raises:
        ValueError with a user-facing message on validation failure.
    """
    if not file_storage or not file_storage.filename:
        return None

    # 1. Extension check (first gate — fast)
    if not allowed_image_extension(file_storage.filename):
        raise ValueError(
            f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    # 2. Read the file data into memory
    file_data = file_storage.read()
    if len(file_data) > current_app.config["MAX_CONTENT_LENGTH"]:
        raise ValueError("File exceeds the maximum allowed size of 5 MB.")

    # 3. Validate with Pillow (actually decode the image — rejects fake images)
    try:
        img = Image.open(io.BytesIO(file_data))
        img.verify()  # Raises if not a valid image
        # Re-open after verify() — Pillow closes the file after verify
        img = Image.open(io.BytesIO(file_data))
    except (UnidentifiedImageError, Exception) as exc:
        raise ValueError(f"Invalid image file: {exc}") from exc

    # 4. Check format whitelist
    if img.format not in ALLOWED_PIL_FORMATS:
        raise ValueError(
            f"Unsupported image format '{img.format}'. "
            f"Allowed: {', '.join(ALLOWED_PIL_FORMATS)}"
        )

    # 5. Resize if wider than MAX_IMAGE_WIDTH (preserve aspect ratio)
    if img.width > MAX_IMAGE_WIDTH:
        ratio = MAX_IMAGE_WIDTH / img.width
        new_size = (MAX_IMAGE_WIDTH, int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    # 6. Convert to RGB if necessary (e.g. RGBA PNG → JPEG would fail)
    save_format = img.format or "JPEG"
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    if ext == "jpg":
        ext = "jpeg"
    if save_format == "JPEG" and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # 7. Generate a UUID-based filename to prevent collisions / path traversal
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)
    dest_path = os.path.join(upload_folder, unique_name)

    # 8. Save the (possibly resized) image
    img.save(dest_path, format=save_format.upper())
    logger.info("Saved product image: %s", unique_name)
    return unique_name


def delete_product_image(filename: str) -> None:
    """Delete a product image file from the uploads folder (best-effort)."""
    if not filename:
        return
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    path = os.path.join(upload_folder, filename)
    try:
        if os.path.isfile(path):
            os.remove(path)
            logger.info("Deleted product image: %s", filename)
    except OSError as exc:
        logger.warning("Could not delete image %s: %s", filename, exc)


# ─── API key protection decorator ────────────────────────────────────────────

def api_key_required(f):
    """
    Decorator that requires a valid X-API-KEY header on Flask route handlers.
    Returns 401 JSON on failure and logs the attempt.
    """

    @wraps(f)
    def decorated(*args, **kwargs):
        from models import SyncLog
        from extensions import db

        provided_key = request.headers.get("X-API-KEY", "")
        expected_key = current_app.config.get("SYNC_API_KEY", "")

        if not provided_key or not hmac.compare_digest(provided_key, expected_key):
            device_id = request.headers.get("X-Device-ID", "unknown")
            log = SyncLog(
                direction="push",
                status="error",
                detail=f"Invalid or missing X-API-KEY from IP {request.remote_addr}",
                device_id=device_id,
            )
            try:
                db.session.add(log)
                db.session.commit()
            except Exception:
                db.session.rollback()
            logger.warning(
                "API key rejection: IP=%s device=%s", request.remote_addr, device_id
            )
            return jsonify({"error": "Unauthorized — invalid API key"}), 401

        return f(*args, **kwargs)

    return decorated
