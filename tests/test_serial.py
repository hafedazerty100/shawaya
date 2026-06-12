"""
tests/test_serial.py — Tests for serial activation and HMAC validation.
"""

from datetime import datetime, timezone, timedelta
from extensions import db
from models import SerialKey
from utils import (
    hash_serial,
    generate_activation_token,
    validate_activation_token,
    extract_token_device_id,
)


def test_validate_activation_token_signature(server_app):
    """Test HMAC validation of the activation token locally."""
    with server_app.app_context():
        serial_hash = hash_serial("TEST-SERIAL-1234")
        device_id = "device-1"

        # Generate a token and validate it
        token = generate_activation_token(serial_hash, device_id)
        assert validate_activation_token(token) is True

        # Extract device_id
        extracted = extract_token_device_id(token)
        assert extracted == device_id

        # Tamper with the token payload and ensure it fails validation
        tampered_token = token.replace(device_id, "device-2")
        assert validate_activation_token(tampered_token) is False

        # Empty token should be invalid
        assert validate_activation_token("") is False


def test_api_activation_success(server_app, server_client):
    """Test successful first-time activation via server endpoint."""
    raw_serial = "ACTIVATE-ME-NOW-123"
    serial_hash = hash_serial(raw_serial)
    device_id = "kiosk-1"

    with server_app.app_context():
        # Seed a serial key in the DB
        key = SerialKey(
            serial_hash=serial_hash,
            is_active=False,
            device_id=None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=365)
        )
        db.session.add(key)
        db.session.commit()

    # Call endpoint with correct X-API-KEY and valid serial
    api_key = server_app.config.get("SYNC_API_KEY")
    resp = server_client.post(
        "/api/validate-serial",
        json={"serial_hash": serial_hash, "device_id": device_id},
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"}
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["valid"] is True
    assert "activation_token" in data

    # Verify key status is active and bound to the device in DB
    with server_app.app_context():
        db_key = SerialKey.query.filter_by(serial_hash=serial_hash).first()
        assert db_key.is_active is True
        assert db_key.device_id == device_id


def test_api_activation_prevent_double_activation(server_app, server_client):
    """Test that a serial already bound to a device cannot be activated on another device."""
    raw_serial = "SHARED-SERIAL-999"
    serial_hash = hash_serial(raw_serial)
    device_id_1 = "kiosk-1"
    device_id_2 = "kiosk-2"

    with server_app.app_context():
        # Seed a serial key that is already active on kiosk-1
        key = SerialKey(
            serial_hash=serial_hash,
            is_active=True,
            device_id=device_id_1,
            expires_at=datetime.now(timezone.utc) + timedelta(days=365)
        )
        db.session.add(key)
        db.session.commit()

    api_key = server_app.config.get("SYNC_API_KEY")

    # Activation on same device (re-activation) should succeed/allow
    resp_same = server_client.post(
        "/api/validate-serial",
        json={"serial_hash": serial_hash, "device_id": device_id_1},
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"}
    )
    assert resp_same.status_code == 200

    # Activation on a DIFFERENT device must fail (401)
    resp_diff = server_client.post(
        "/api/validate-serial",
        json={"serial_hash": serial_hash, "device_id": device_id_2},
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"}
    )
    assert resp_diff.status_code == 401
    assert "already activated" in resp_diff.get_json()["error"].lower()


def test_api_activation_expired_or_revoked(server_app, server_client):
    """Test activation attempts with expired serials are rejected with 403."""
    raw_serial = "EXPIRED-KEY-888"
    serial_hash = hash_serial(raw_serial)

    with server_app.app_context():
        # Seed an expired serial key
        key = SerialKey(
            serial_hash=serial_hash,
            is_active=False,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1)
        )
        db.session.add(key)
        db.session.commit()

    api_key = server_app.config.get("SYNC_API_KEY")
    resp = server_client.post(
        "/api/validate-serial",
        json={"serial_hash": serial_hash, "device_id": "kiosk-1"},
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"}
    )
    assert resp.status_code == 403
    assert "expired" in resp.get_json()["error"].lower()
