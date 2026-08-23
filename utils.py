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
    """Convert an integer cent value to a display string, e.g. 450 → '4.50 DA'."""
    return f"{cents / 100:.2f} DA"


def da_to_cents(da: float) -> int:
    """Convert a DA float (from user input) to integer cents."""
    return round(da * 100)


# ─── Serial key hashing ───────────────────────────────────────────────────────

def hash_serial(raw_serial: str) -> str:
    """Return the SHA-256 hex digest of a raw serial string."""
    if not raw_serial:
        return ""
    clean_serial = str(raw_serial).strip()
    return hashlib.sha256(clean_serial.encode("utf-8")).hexdigest()


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
    Validate the HMAC signature of the activation token locally.
    """
    if not token:
        return False
    try:
        parts = token.rsplit(":", 1)
        if len(parts) != 2:
            return False
        payload, sig = parts[0], parts[1]
        expected_sig = hmac.new(_hmac_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected_sig)
    except Exception:
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
    except (UnidentifiedImageError, OSError, Exception) as exc:
        raise ValueError(f"Invalid image file: {exc}")

    # 4. Generate unique UUID filename
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)
    save_path = os.path.join(upload_folder, filename)

    # 5. Re-open to process (verify() closes/invalidates the image handle)
    img = Image.open(io.BytesIO(file_data))

    # Convert RGBA / P mode images to RGB for JPEG saving
    if ext in ("jpg", "jpeg") and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Resize if image exceeds max width while preserving aspect ratio
    if img.width > MAX_IMAGE_WIDTH:
        ratio = MAX_IMAGE_WIDTH / float(img.width)
        new_height = int(float(img.height) * ratio)
        img = img.resize((MAX_IMAGE_WIDTH, new_height), Image.Resampling.LANCZOS)

    # Save to disk
    img.save(save_path, optimize=True, quality=85)
    logger.info("Saved product image: %s", filename)
    return filename


def delete_product_image(filename: str | None) -> None:
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

        provided_key = request.headers.get("X-API-KEY", "").strip()
        expected_key = current_app.config.get("SYNC_API_KEY", "").strip()

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


def check_and_apply_updates():
    """
    Check for updates via git fetch. If new commits exist on origin/main,
    apply them with git reset --hard, install requirements, and restart.
    Returns True only if an update was applied (process restarts).
    """
    import os
    import sys
    import subprocess
    
    project_dir = os.path.dirname(os.path.abspath(__file__))
    git_dir = os.path.join(project_dir, ".git")
    if not os.path.isdir(git_dir):
        logger.info("[UPDATE] Not a git repository — skipping update check.")
        return False
        
    # 1. Fetch latest from remote with retry
    logger.info("[UPDATE] Checking for updates...")
    git_cmd = ["git", "fetch", "origin", "main"]

    fetch_ok = False
    for attempt in range(1, 3):
        try:
            res = subprocess.run(
                git_cmd,
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )
            if res.returncode == 0:
                fetch_ok = True
                break
            else:
                logger.warning("[UPDATE] Git fetch attempt %d failed: %s", attempt, res.stderr.strip() or "unknown error")
        except subprocess.TimeoutExpired:
            logger.warning("[UPDATE] Git fetch attempt %d timed out.", attempt)
        except Exception as e:
            logger.warning("[UPDATE] Git fetch attempt %d error: %s", attempt, e)

        if attempt < 2:
            time.sleep(1)

    if not fetch_ok:
        logger.warning("[UPDATE] Could not reach GitHub remote — skipping update for this cycle.")
        return False

    try:
        # 2. Compare local HEAD vs origin/main
        local_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project_dir,
            capture_output=True, text=True
        ).stdout.strip()
        remote_hash = subprocess.run(
            ["git", "rev-parse", "origin/main"], cwd=project_dir,
            capture_output=True, text=True
        ).stdout.strip()
        
        if not local_hash or not remote_hash:
            logger.warning("[UPDATE] Could not determine git hashes.")
            return False

        if local_hash == remote_hash:
            logger.info("[UPDATE] Already up to date (%s).", local_hash[:7])
            return False

        # 3. New updates available — apply them
        logger.info("[UPDATE] New updates found: %s → %s. Applying...", local_hash[:7], remote_hash[:7])
        subprocess.run(
            ["git", "reset", "--hard", "origin/main"],
            cwd=project_dir, capture_output=True, text=True, check=True
        )
        
        # 4. Install updated requirements
        req_file = os.path.join(project_dir, "requirements_windows.txt")
        if not os.path.isfile(req_file):
            req_file = os.path.join(project_dir, "requirements.txt")
        if os.path.isfile(req_file):
            try:
                logger.info("[UPDATE] Installing updated requirements...")
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", req_file],
                    cwd=project_dir, timeout=120, capture_output=True
                )
            except Exception as pip_err:
                logger.warning("[UPDATE] pip install had issues (continuing): %s", pip_err)

        # 5. Restart the process with the new code
        logger.info("[UPDATE] Update applied successfully. Restarting...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    except subprocess.TimeoutExpired:
        logger.warning("[UPDATE] Git fetch timed out after 30s — will retry next cycle.")
    except Exception as exc:
        logger.warning("[UPDATE] Update check failed: %s", exc)
        
    return False

