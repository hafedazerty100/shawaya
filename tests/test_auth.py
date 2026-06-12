"""
tests/test_auth.py — Tests for API key authentication and logs.
"""

from extensions import db
from models import SyncLog


def test_api_auth_missing_key(server_app, server_client):
    """Test API endpoints return 401 when X-API-KEY header is missing."""
    endpoints = [
        ("/api/products", "GET"),
        ("/api/validate-serial", "POST"),
        ("/api/sync/orders", "POST"),
    ]

    for url, method in endpoints:
        if method == "GET":
            resp = server_client.get(url)
        else:
            resp = server_client.post(url, json={})
        
        assert resp.status_code == 401
        assert "unauthorized" in resp.get_json()["error"].lower()

    # Verify that auth failures were logged to the SyncLog table
    with server_app.app_context():
        logs = SyncLog.query.filter_by(status="error").all()
        # At least one log per failure
        assert len(logs) >= len(endpoints)
        assert any("invalid or missing x-api-key" in log.detail.lower() for log in logs)


def test_api_auth_invalid_key(server_app, server_client):
    """Test API endpoints return 401 when X-API-KEY header is incorrect."""
    headers = {"X-API-KEY": "wrong-key"}
    
    # Check GET products
    resp_get = server_client.get("/api/products", headers=headers)
    assert resp_get.status_code == 401

    # Check POST validate-serial
    resp_post = server_client.post("/api/validate-serial", json={}, headers=headers)
    assert resp_post.status_code == 401


def test_api_auth_valid_key(server_app, server_client):
    """Test API endpoints bypass 401 unauthorized when correct X-API-KEY is provided."""
    api_key = server_app.config.get("SYNC_API_KEY", "dev-insecure-sync-api-key")
    headers = {"X-API-KEY": api_key}

    # GET products should return 200 (since it doesn't require extra body parameters)
    resp = server_client.get("/api/products", headers=headers)
    assert resp.status_code == 200

    # POST validate-serial should return 400 (Bad Request due to empty payload, NOT 401)
    resp_post = server_client.post("/api/validate-serial", json={}, headers=headers)
    assert resp_post.status_code == 400
