"""
tests/test_upload.py — Tests for product image validation and resizing.
"""

import io
import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage
from utils import save_product_image


def _generate_test_image(format="PNG", size=(100, 100)) -> bytes:
    """Generate mock image bytes in memory."""
    img_io = io.BytesIO()
    image = Image.new("RGB", size, color="red")
    image.save(img_io, format=format)
    img_io.seek(0)
    return img_io.read()


def test_upload_valid_image(desktop_app):
    """Test save_product_image successfully validates and saves a valid image."""
    img_bytes = _generate_test_image("PNG")
    file_storage = FileStorage(
        stream=io.BytesIO(img_bytes),
        filename="shawaya.png",
        content_type="image/png"
    )

    with desktop_app.app_context():
        filename = save_product_image(file_storage)
        assert filename is not None
        assert filename.endswith(".png")


def test_upload_image_resizing(desktop_app):
    """Test that large images are resized to MAX_IMAGE_WIDTH (1024px)."""
    # Create an image wider than 1024 pixels
    img_bytes = _generate_test_image("JPEG", size=(1200, 800))
    file_storage = FileStorage(
        stream=io.BytesIO(img_bytes),
        filename="large_shawaya.jpg",
        content_type="image/jpeg"
    )

    with desktop_app.app_context():
        filename = save_product_image(file_storage)
        assert filename is not None
        
        # Open saved image and check dimensions
        import os
        saved_path = os.path.join(desktop_app.config["UPLOAD_FOLDER"], filename)
        saved_img = Image.open(saved_path)
        assert saved_img.width == 1024
        # Height should be scaled down proportionally (800 * 1024 / 1200 = 682)
        assert saved_img.height == 682


def test_upload_invalid_extension(desktop_app):
    """Test rejection of unsupported file extensions (e.g., .txt, .pdf)."""
    file_storage = FileStorage(
        stream=io.BytesIO(b"dummy file content"),
        filename="hack.sh",
        content_type="application/x-sh"
    )

    with desktop_app.app_context():
        with pytest.raises(ValueError) as exc:
            save_product_image(file_storage)
        assert "invalid file type" in str(exc.value).lower()


def test_upload_fake_image_content(desktop_app):
    """Test rejection of files with a valid image extension but invalid/corrupted contents."""
    # Write non-image bytes to a file named 'fake.png'
    file_storage = FileStorage(
        stream=io.BytesIO(b"GIF89a this is not a real image file content"),
        filename="fake.png",
        content_type="image/png"
    )

    with desktop_app.app_context():
        with pytest.raises(ValueError) as exc:
            save_product_image(file_storage)
        assert "invalid image file" in str(exc.value).lower()


def test_upload_oversized_file(desktop_app):
    """Test rejection of images exceeding the maximum size limit."""
    # Set MAX_CONTENT_LENGTH extremely low for testing purposes
    original_limit = desktop_app.config["MAX_CONTENT_LENGTH"]
    desktop_app.config["MAX_CONTENT_LENGTH"] = 10  # 10 bytes
    
    img_bytes = _generate_test_image("PNG", size=(10, 10))
    file_storage = FileStorage(
        stream=io.BytesIO(img_bytes),
        filename="tiny.png",
        content_type="image/png"
    )

    try:
        with desktop_app.app_context():
            with pytest.raises(ValueError) as exc:
                save_product_image(file_storage)
            assert "exceeds the maximum allowed size" in str(exc.value).lower()
    finally:
        desktop_app.config["MAX_CONTENT_LENGTH"] = original_limit
