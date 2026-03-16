"""
test_app.py — Flask app endpoint testleri.
"""

import json
import pytest
import sys
import os

# app.py'yi import edebilmek için proje kök dizinini path'e ekle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app


@pytest.fixture
def client():
    """Flask test client."""
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


# ── Temel endpoint testleri ────────────────────────────────────
class TestBasicEndpoints:
    def test_index_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_index_contains_html(self, client):
        resp = client.get("/")
        data = resp.data.lower()
        assert b"<html" in data or b"<!doctype" in data

    def test_config_get(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_status_endpoint(self, client):
        resp = client.get("/api/run/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "running" in data


# ── Config save/load ───────────────────────────────────────────
class TestConfig:
    def test_save_config_accepts_post(self, client):
        resp = client.post(
            "/api/config",
            data=json.dumps({"config": {"ems": {"smtp_host": "test.local"}}}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_config_get_returns_ok_field(self, client):
        resp = client.get("/api/config")
        data = resp.get_json()
        assert "ok" in data


# ── Run endpoints ──────────────────────────────────────────────
class TestRunEndpoints:
    def test_status_when_not_running(self, client):
        resp = client.get("/api/run/status")
        data = resp.get_json()
        assert data["running"] is False

    def test_stop_when_not_running(self, client):
        resp = client.post("/api/run/stop")
        assert resp.status_code == 200


# ── MFA endpoints ──────────────────────────────────────────────
class TestMFAEndpoints:
    def test_mfa_status_returns_json(self, client):
        resp = client.get("/api/mfa/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_mfa_submit_without_challenge(self, client):
        resp = client.post(
            "/api/mfa/submit",
            data=json.dumps({"code": "123456"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400)

    def test_mfa_cancel(self, client):
        resp = client.post("/api/mfa/cancel")
        assert resp.status_code in (200, 400)


# ── Verification endpoints ────────────────────────────────────
class TestVerificationEndpoints:
    def test_pending_verifications(self, client):
        resp = client.get("/api/verification/pending")
        assert resp.status_code == 200

    def test_respond_verification_bad_request(self, client):
        resp = client.post(
            "/api/verification/respond",
            data=json.dumps({"id": "nonexistent", "passed": True}),
            content_type="application/json",
        )
        # pending olmadığında hata veya başarılı dönebilir
        assert resp.status_code in (200, 400, 404)
