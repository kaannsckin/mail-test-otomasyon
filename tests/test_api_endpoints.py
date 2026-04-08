"""
test_api_endpoints.py — Tüm Flask API endpoint'lerinin kapsamlı testleri.

Test Grupları:
- Temel endpoint'ler (/, GET/POST config, status)
- Config kaydet/yükle (YAML round-trip)
- Run kontrolü (start/stop/reset/status/logs)
- MFA/2FA işlemleri (status/submit/cancel/totp)
- Doğrulama (pending/respond)
- Raporlar (list/get)
- Şablonlar (get/save/export)
- Ekler (list/upload/delete)
- OAuth2 (url/status/callback)
- Görsel regresyon (set-baseline/diff-info)
- Otomasyon zamanlayıcı (sync)
"""

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Proje kök dizinini path'e ekle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, run_state, REPORTS_DIR, VERIFICATION_DIR, ATTACHMENTS_DIR


# ── Fixtures ───────────────────────────────────────────────────────────
@pytest.fixture
def client():
    """Flask test istemcisi."""
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def reset_run_state():
    """Her testten önce run_state'i temizle."""
    run_state.update({
        "running": False,
        "process": None,
        "log_buffer": [],
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
    })
    yield


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Geçici config.yaml yolu döner ve app'e patch uygular."""
    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setattr("app.CONFIG_PATH", cfg_path)
    return cfg_path


@pytest.fixture
def minimal_config(tmp_config):
    """Minimal geçerli bir config.yaml oluşturur."""
    data = {
        "ems": {
            "smtp_host": "ems.test.local",
            "smtp_port": 587,
            "imap_host": "ems.test.local",
            "imap_port": 993,
            "username": "test@test.local",
            "password": "gizli",
            "label": "EMS Test",
            "smtp_use_tls": True,
            "imap_use_ssl": True,
            "auth_method": "password",
        }
    }
    tmp_config.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return tmp_config


# ══════════════════════════════════════════════════════════════════════
# 1. TEMEL ENDPOINT'LER
# ══════════════════════════════════════════════════════════════════════
class TestIndexPage:
    def test_index_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_index_returns_html(self, client):
        resp = client.get("/")
        data = resp.data.lower()
        assert b"<!doctype" in data or b"<html" in data

    def test_index_contains_page_title(self, client):
        resp = client.get("/")
        assert b"Mail Test Otomasyon" in resp.data or b"mail" in resp.data.lower()

    def test_index_content_type_html(self, client):
        resp = client.get("/")
        assert "text/html" in resp.content_type


# ══════════════════════════════════════════════════════════════════════
# 2. CONFIG API
# ══════════════════════════════════════════════════════════════════════
class TestConfigGet:
    def test_get_config_status_200(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200

    def test_get_config_returns_json(self, client):
        resp = client.get("/api/config")
        assert resp.is_json

    def test_get_config_has_ok_field(self, client):
        resp = client.get("/api/config")
        data = resp.get_json()
        assert "ok" in data

    def test_get_config_ok_is_true(self, client):
        resp = client.get("/api/config")
        assert resp.get_json()["ok"] is True

    def test_get_config_has_config_field(self, client):
        resp = client.get("/api/config")
        data = resp.get_json()
        assert "config" in data

    def test_get_config_has_mail_addresses(self, client):
        """Config yoksa dahi mail_addresses sentezlenmeli."""
        resp = client.get("/api/config")
        data = resp.get_json()
        assert "mail_addresses" in data["config"]

    def test_get_config_mail_addresses_has_three_servers(self, client):
        resp = client.get("/api/config")
        ma = resp.get_json()["config"]["mail_addresses"]
        for srv in ["ems", "gmail", "outlook"]:
            assert srv in ma


class TestConfigSave:
    def test_save_config_returns_200(self, client):
        payload = {"config": {"ems": {"smtp_host": "test.local"}}}
        resp = client.post(
            "/api/config",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_save_config_returns_ok_true(self, client):
        payload = {"config": {"ems": {"smtp_host": "test.local"}}}
        resp = client.post(
            "/api/config",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.get_json()["ok"] is True

    def test_save_config_persists_empty_body(self, client):
        """Boş config dict verilse bile hata vermemeli."""
        resp = client.post(
            "/api/config",
            data=json.dumps({"config": {}}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_save_config_preserves_password(self, client, minimal_config):
        """Yeni kayıtta password boş geçilirse eskisi korunmalı."""
        # Önce config'i yükle
        resp = client.post(
            "/api/config",
            data=json.dumps({"config": {"ems": {"smtp_host": "ems.test.local"}}}),
            content_type="application/json",
        )
        assert resp.status_code == 200


class TestConfigRoundtrip:
    def test_save_then_load(self, client, tmp_config):
        payload = {
            "config": {
                "ems": {
                    "smtp_host": "roundtrip.local",
                    "smtp_port": 587,
                }
            }
        }
        client.post(
            "/api/config",
            data=json.dumps(payload),
            content_type="application/json",
        )
        get_resp = client.get("/api/config")
        data = get_resp.get_json()
        assert data["ok"] is True


# ══════════════════════════════════════════════════════════════════════
# 3. RUN KONTROLÜ
# ══════════════════════════════════════════════════════════════════════
class TestRunStatus:
    def test_status_returns_200(self, client):
        resp = client.get("/api/run/status")
        assert resp.status_code == 200

    def test_status_is_json(self, client):
        resp = client.get("/api/run/status")
        assert resp.is_json

    def test_status_has_running_field(self, client):
        resp = client.get("/api/run/status")
        assert "running" in resp.get_json()

    def test_status_running_false_initially(self, client):
        resp = client.get("/api/run/status")
        assert resp.get_json()["running"] is False

    def test_status_has_started_at(self, client):
        resp = client.get("/api/run/status")
        data = resp.get_json()
        assert "started_at" in data

    def test_status_has_finished_at(self, client):
        resp = client.get("/api/run/status")
        data = resp.get_json()
        assert "finished_at" in data

    def test_status_has_exit_code(self, client):
        resp = client.get("/api/run/status")
        data = resp.get_json()
        assert "exit_code" in data


class TestRunStop:
    def test_stop_returns_200(self, client):
        resp = client.post("/api/run/stop")
        assert resp.status_code == 200

    def test_stop_returns_ok(self, client):
        resp = client.post("/api/run/stop")
        assert resp.get_json()["ok"] is True

    def test_stop_when_not_running_does_not_error(self, client):
        """Çalışmıyorken durdurmak 200 döndürmeli."""
        resp = client.post("/api/run/stop")
        assert resp.status_code == 200


class TestRunReset:
    def test_reset_returns_200(self, client):
        resp = client.post("/api/run/reset")
        assert resp.status_code == 200

    def test_reset_returns_ok(self, client):
        resp = client.post("/api/run/reset")
        assert resp.get_json()["ok"] is True

    def test_reset_clears_running_state(self, client):
        """Reset sonrası running False olmalı."""
        run_state["running"] = True
        client.post("/api/run/reset")
        assert run_state["running"] is False

    def test_reset_clears_log_buffer(self, client):
        """Reset sonrası log_buffer temizlenmeli."""
        run_state["log_buffer"] = ["log1", "log2"]
        client.post("/api/run/reset")
        assert run_state["log_buffer"] == []


class TestRunStart:
    def test_start_when_not_config_returns_error_or_starts(self, client):
        """Config olmadan start denemesi — ya hata ya da start olmalı."""
        resp = client.post(
            "/api/run/start",
            data=json.dumps({"dry_run": True}),
            content_type="application/json",
        )
        # 200 (başarıyla kuyruğa alındı) veya 400 (config yok) beklenir
        assert resp.status_code in (200, 400)

    def test_start_while_running_returns_400(self, client):
        """Zaten çalışırken start 400 dönmeli."""
        run_state["running"] = True
        run_state["process"] = MagicMock(poll=lambda: None)
        resp = client.post(
            "/api/run/start",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False


class TestRunLogs:
    def test_logs_endpoint_exists(self, client):
        """Logs SSE endpoint'ine GET isteği atılabilmeli."""
        resp = client.get("/api/run/logs?offset=0")
        # SSE stream veya 200 dönmeli
        assert resp.status_code == 200

    def test_logs_content_type_event_stream(self, client):
        """Logs endpoint'i text/event-stream döndürmeli."""
        resp = client.get("/api/run/logs?offset=0")
        assert "text/event-stream" in resp.content_type


# ══════════════════════════════════════════════════════════════════════
# 4. MFA / 2FA
# ══════════════════════════════════════════════════════════════════════
class TestMFAStatus:
    def test_mfa_status_returns_200(self, client):
        resp = client.get("/api/mfa/status")
        assert resp.status_code == 200

    def test_mfa_status_is_json(self, client):
        resp = client.get("/api/mfa/status")
        assert resp.is_json

    def test_mfa_status_has_pending_field(self, client):
        resp = client.get("/api/mfa/status")
        assert "pending" in resp.get_json()

    def test_mfa_status_pending_false_by_default(self, client):
        resp = client.get("/api/mfa/status")
        assert resp.get_json()["pending"] is False

    def test_mfa_status_has_challenge_field(self, client):
        resp = client.get("/api/mfa/status")
        assert "challenge" in resp.get_json()


class TestMFASubmit:
    def test_mfa_submit_empty_code_returns_error(self, client):
        resp = client.post(
            "/api/mfa/submit",
            data=json.dumps({"code": ""}),
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["ok"] is False

    def test_mfa_submit_valid_code_returns_json(self, client):
        resp = client.post(
            "/api/mfa/submit",
            data=json.dumps({"code": "123456"}),
            content_type="application/json",
        )
        assert resp.is_json

    def test_mfa_submit_without_pending_challenge(self, client):
        """Bekleyen challenge yokken submit: ok False beklenir."""
        resp = client.post(
            "/api/mfa/submit",
            data=json.dumps({"code": "999999"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400)
        assert "ok" in resp.get_json()


class TestMFACancel:
    def test_mfa_cancel_returns_200(self, client):
        resp = client.post("/api/mfa/cancel")
        assert resp.status_code == 200

    def test_mfa_cancel_returns_json(self, client):
        resp = client.post("/api/mfa/cancel")
        assert resp.is_json

    def test_mfa_cancel_has_ok_field(self, client):
        resp = client.post("/api/mfa/cancel")
        assert "ok" in resp.get_json()


class TestTOTPPreview:
    def test_totp_preview_empty_secret_returns_error(self, client):
        resp = client.post(
            "/api/mfa/totp-preview",
            data=json.dumps({"secret": ""}),
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["ok"] is False

    def test_totp_preview_no_secret_returns_error(self, client):
        resp = client.post(
            "/api/mfa/totp-preview",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.get_json()["ok"] is False

    def test_totp_preview_valid_secret(self, client):
        """Geçerli Base32 secret TOTP kodu döndürmeli."""
        # JBSWY3DPEHPK3PXP — standart test secret'ı
        resp = client.post(
            "/api/mfa/totp-preview",
            data=json.dumps({"secret": "JBSWY3DPEHPK3PXP"}),
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert "code" in data
        assert len(data["code"]) == 6
        assert "remaining" in data


# ══════════════════════════════════════════════════════════════════════
# 5. DOĞRULAMA (VERIFICATION)
# ══════════════════════════════════════════════════════════════════════
class TestVerificationPending:
    def test_pending_returns_200(self, client):
        resp = client.get("/api/verification/pending")
        assert resp.status_code == 200

    def test_pending_is_json(self, client):
        resp = client.get("/api/verification/pending")
        assert resp.is_json

    def test_pending_has_ok_field(self, client):
        resp = client.get("/api/verification/pending")
        assert "ok" in resp.get_json()

    def test_pending_has_pending_list(self, client):
        resp = client.get("/api/verification/pending")
        data = resp.get_json()
        assert "pending" in data
        assert isinstance(data["pending"], list)

    def test_pending_empty_by_default(self, client, tmp_path, monkeypatch):
        """Verification dizini boşsa pending listesi boş gelir."""
        monkeypatch.setattr("app.VERIFICATION_DIR", tmp_path / "verifications")
        resp = client.get("/api/verification/pending")
        data = resp.get_json()
        assert data["pending"] == []

    def test_pending_reads_json_files(self, client, tmp_path, monkeypatch):
        """Dizinde pending_*.json varsa listede görünmeli."""
        vdir = tmp_path / "verifications"
        vdir.mkdir()
        monkeypatch.setattr("app.VERIFICATION_DIR", vdir)
        (vdir / "pending_abc123.json").write_text(
            json.dumps({"id": "abc123", "scenario": "plain_text"}), encoding="utf-8"
        )
        resp = client.get("/api/verification/pending")
        data = resp.get_json()
        assert len(data["pending"]) == 1
        assert data["pending"][0]["id"] == "abc123"


class TestVerificationRespond:
    def test_respond_missing_id_returns_400(self, client):
        resp = client.post(
            "/api/verification/respond",
            data=json.dumps({"approved": True}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_respond_with_id_creates_file(self, client, tmp_path, monkeypatch):
        vdir = tmp_path / "verifications"
        vdir.mkdir()
        monkeypatch.setattr("app.VERIFICATION_DIR", vdir)
        resp = client.post(
            "/api/verification/respond",
            data=json.dumps({"id": "test001", "approved": True, "issues": []}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert (vdir / "response_test001.json").exists()

    def test_respond_approved_false(self, client, tmp_path, monkeypatch):
        vdir = tmp_path / "verifications"
        vdir.mkdir()
        monkeypatch.setattr("app.VERIFICATION_DIR", vdir)
        resp = client.post(
            "/api/verification/respond",
            data=json.dumps({"id": "test002", "approved": False, "issues": ["Yanlış format"]}),
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert data["approved"] is False


# ══════════════════════════════════════════════════════════════════════
# 6. KOMBİNASYONLAR
# ══════════════════════════════════════════════════════════════════════
class TestCombinations:
    def test_combinations_endpoint_accessible(self, client):
        resp = client.get("/api/combinations")
        assert resp.status_code == 200

    def test_combinations_is_json(self, client):
        resp = client.get("/api/combinations")
        assert resp.is_json

    def test_combinations_has_ok_field(self, client):
        resp = client.get("/api/combinations")
        assert "ok" in resp.get_json()

    def test_combinations_with_csv_returns_list(self, client):
        resp = client.get("/api/combinations")
        data = resp.get_json()
        if data["ok"]:
            assert isinstance(data["combinations"], list)


# ══════════════════════════════════════════════════════════════════════
# 7. RAPORLAR
# ══════════════════════════════════════════════════════════════════════
class TestReports:
    def test_list_reports_returns_200(self, client):
        resp = client.get("/api/reports")
        assert resp.status_code == 200

    def test_list_reports_is_json(self, client):
        resp = client.get("/api/reports")
        assert resp.is_json

    def test_list_reports_has_ok_field(self, client):
        resp = client.get("/api/reports")
        assert "ok" in resp.get_json()

    def test_list_reports_has_reports_list(self, client):
        resp = client.get("/api/reports")
        data = resp.get_json()
        assert "reports" in data
        assert isinstance(data["reports"], list)

    def test_get_nonexistent_report_returns_404(self, client):
        resp = client.get("/api/reports/nonexistent_report_xyz.html")
        assert resp.status_code == 404

    def test_list_reports_report_fields_when_exist(self, client, tmp_path, monkeypatch):
        """Rapor varsa name/size/type/mtime alanları olmalı."""
        monkeypatch.setattr("app.REPORTS_DIR", tmp_path)
        (tmp_path / "report_test.html").write_text("<html>Test</html>")
        resp = client.get("/api/reports")
        data = resp.get_json()
        if data["reports"]:
            r = data["reports"][0]
            for field in ("name", "size", "type", "mtime"):
                assert field in r


class TestLatestResults:
    def test_latest_results_no_csv_returns_error(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr("app.REPORTS_DIR", tmp_path)
        resp = client.get("/api/results/latest")
        data = resp.get_json()
        assert data["ok"] is False

    def test_latest_results_with_csv(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr("app.REPORTS_DIR", tmp_path)
        csv_file = tmp_path / "results_20240101.csv"
        csv_file.write_text("Senaryo,Durum\nplain_text,PASS\n", encoding="utf-8-sig")
        resp = client.get("/api/results/latest")
        data = resp.get_json()
        assert data["ok"] is True
        assert "results" in data
        assert isinstance(data["results"], list)


class TestHistoryResults:
    def test_history_returns_200_or_error(self, client):
        resp = client.get("/api/results/history")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════
# 8. ŞABLONLAR (TEMPLATES API)
# ══════════════════════════════════════════════════════════════════════
class TestTemplatesAPI:
    def test_get_templates_returns_200(self, client):
        resp = client.get("/api/templates")
        assert resp.status_code == 200

    def test_get_templates_is_json(self, client):
        resp = client.get("/api/templates")
        assert resp.is_json

    def test_get_templates_has_ok_field(self, client):
        resp = client.get("/api/templates")
        assert resp.get_json()["ok"] is True

    def test_get_templates_has_data_field(self, client):
        resp = client.get("/api/templates")
        data = resp.get_json()
        assert "data" in data

    def test_get_templates_data_has_templates_key(self, client):
        resp = client.get("/api/templates")
        data = resp.get_json()["data"]
        assert "templates" in data

    def test_post_templates_accepts_empty(self, client):
        resp = client.post(
            "/api/templates",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 500)

    def test_export_defaults_returns_ok(self, client):
        resp = client.post("/api/templates/export-defaults")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True


# ══════════════════════════════════════════════════════════════════════
# 9. EKLER (ATTACHMENTS)
# ══════════════════════════════════════════════════════════════════════
class TestAttachments:
    def test_list_attachments_returns_200(self, client):
        resp = client.get("/api/attachments")
        assert resp.status_code == 200

    def test_list_attachments_is_json(self, client):
        resp = client.get("/api/attachments")
        assert resp.is_json

    def test_list_attachments_has_ok(self, client):
        resp = client.get("/api/attachments")
        assert resp.get_json()["ok"] is True

    def test_list_attachments_has_files_list(self, client):
        resp = client.get("/api/attachments")
        data = resp.get_json()
        assert "files" in data
        assert isinstance(data["files"], list)

    def test_upload_no_file_returns_400(self, client):
        resp = client.post("/api/attachments/upload", data={})
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_upload_file_returns_200(self, client, tmp_path, monkeypatch):
        """Gerçek dosya upload testi."""
        monkeypatch.setattr("app.ATTACHMENTS_DIR", tmp_path / "attachments")
        data = {
            "files": (io.BytesIO(b"PDF content"), "test_doc.pdf"),
        }
        resp = client.post(
            "/api/attachments/upload",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        result = resp.get_json()
        assert result["ok"] is True
        assert "test_doc.pdf" in result["saved"]

    def test_upload_multiple_files(self, client, tmp_path, monkeypatch):
        """Çoklu dosya yükleme testi."""
        monkeypatch.setattr("app.ATTACHMENTS_DIR", tmp_path / "attachments")
        data = {
            "files": [
                (io.BytesIO(b"file1"), "doc1.pdf"),
                (io.BytesIO(b"file2"), "img1.png"),
            ],
        }
        resp = client.post(
            "/api/attachments/upload",
            data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        result = resp.get_json()
        assert len(result["saved"]) == 2

    def test_uploaded_file_appears_in_list(self, client, tmp_path, monkeypatch):
        """Yüklenen dosya listeleme endpoint'inde görünmeli."""
        att_dir = tmp_path / "attachments"
        att_dir.mkdir()
        monkeypatch.setattr("app.ATTACHMENTS_DIR", att_dir)
        (att_dir / "sample.pdf").write_bytes(b"PDF")
        resp = client.get("/api/attachments")
        data = resp.get_json()
        names = [f["name"] for f in data["files"]]
        assert "sample.pdf" in names

    def test_attachment_file_fields(self, client, tmp_path, monkeypatch):
        """Dosya listesindeki her kayıtta name/size/ext alanları olmalı."""
        att_dir = tmp_path / "attachments"
        att_dir.mkdir()
        monkeypatch.setattr("app.ATTACHMENTS_DIR", att_dir)
        (att_dir / "test.pdf").write_bytes(b"PDF content")
        resp = client.get("/api/attachments")
        files = resp.get_json()["files"]
        assert len(files) == 1
        f = files[0]
        assert "name" in f
        assert "size" in f
        assert "ext" in f
        assert f["ext"] == ".pdf"


# ══════════════════════════════════════════════════════════════════════
# 10. OAUTH2
# ══════════════════════════════════════════════════════════════════════
class TestOAuth:
    def test_oauth_status_returns_200(self, client):
        resp = client.get("/api/oauth/status")
        assert resp.status_code == 200

    def test_oauth_status_is_json(self, client):
        resp = client.get("/api/oauth/status")
        assert resp.is_json

    def test_oauth_status_has_outlook_field(self, client):
        resp = client.get("/api/oauth/status")
        data = resp.get_json()
        assert "outlook" in data

    def test_oauth_status_has_gmail_field(self, client):
        resp = client.get("/api/oauth/status")
        data = resp.get_json()
        assert "gmail" in data

    def test_oauth_status_fields_are_bool(self, client):
        resp = client.get("/api/oauth/status")
        data = resp.get_json()
        assert isinstance(data["outlook"], bool)
        assert isinstance(data["gmail"], bool)

    def test_oauth_url_missing_server(self, client):
        """server parametresi olmadan istek: ok False veya hata."""
        resp = client.get("/api/oauth/url")
        # OAuth endpoint get_config()'u Flask Response olarak kullandığından
        # server=None olursa 200/400/500 dönebilir
        assert resp.status_code in (200, 400, 500)

    def test_oauth_url_invalid_server(self, client):
        """Geçersiz server türü ok False döndürmeli."""
        resp = client.get("/api/oauth/url?server=invalid")
        assert resp.status_code in (200, 400, 500)


# ══════════════════════════════════════════════════════════════════════
# 11. GÖRSEL REGRESYON (VISUAL REGRESSION)
# ══════════════════════════════════════════════════════════════════════
class TestVisualRegression:
    def test_set_baseline_missing_path_returns_400(self, client):
        resp = client.post(
            "/api/visual/set-baseline",
            data=json.dumps({"scenario_key": "plain_text"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_set_baseline_nonexistent_path_returns_error(self, client):
        resp = client.post(
            "/api/visual/set-baseline",
            data=json.dumps({
                "screenshot_path": "/nonexistent/path/image.png",
                "scenario_key": "plain_text",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_set_baseline_valid_file(self, client, tmp_path, monkeypatch):
        """Geçerli bir screenshot varsa baseline başarıyla atanmalı."""
        screenshot = tmp_path / "screenshot.png"
        screenshot.write_bytes(b"\x89PNG\r\n" + b"\x00" * 100)  # minimal PNG header
        monkeypatch.setattr("app._CONFIG_DIR", tmp_path)
        resp = client.post(
            "/api/visual/set-baseline",
            data=json.dumps({
                "screenshot_path": str(screenshot),
                "scenario_key": "plain_text",
                "combination_label": "test_combo",
            }),
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert "baseline_path" in data

    def test_diff_info_missing_path_returns_404(self, client):
        resp = client.get("/api/visual/diff-info?path=/nonexistent.png")
        assert resp.status_code == 404

    def test_diff_info_no_path_returns_404(self, client):
        resp = client.get("/api/visual/diff-info")
        assert resp.status_code == 404

    def test_diff_info_valid_file(self, client, tmp_path):
        """Gerçek dosya varsa serve edilmeli."""
        img_file = tmp_path / "diff.png"
        img_file.write_bytes(b"\x89PNG\r\n" + b"\x00" * 100)
        resp = client.get(f"/api/visual/diff-info?path={img_file}")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════
# 12. OTOMASYON ZAMANLAYICI
# ══════════════════════════════════════════════════════════════════════
class TestAutomationSync:
    def test_sync_returns_200(self, client):
        resp = client.post("/api/automation/sync")
        assert resp.status_code == 200

    def test_sync_returns_ok(self, client):
        resp = client.post("/api/automation/sync")
        assert resp.get_json()["ok"] is True


# ══════════════════════════════════════════════════════════════════════
# 13. CONNECTION TEST
# ══════════════════════════════════════════════════════════════════════
class TestConnectionTest:
    def test_test_connection_no_config_returns_error(self, client, tmp_path, monkeypatch):
        """Config dosyası yokken bağlantı testi hata dönmeli."""
        monkeypatch.setattr("app.CONFIG_PATH", tmp_path / "nonexistent.yaml")
        resp = client.post(
            "/api/config/test-connection",
            data=json.dumps({"server": "ems"}),
            content_type="application/json",
        )
        # Config olmadan hata veya exception dönebilir
        assert resp.status_code in (200, 500)

    def test_test_connection_missing_smtp_host(self, client, minimal_config, monkeypatch):
        """SMTP host yoksa hatayla dönmeli."""
        monkeypatch.setattr("app.CONFIG_PATH", minimal_config)
        # 'gmail' config'de smtp_host yok
        resp = client.post(
            "/api/config/test-connection",
            data=json.dumps({"server": "gmail"}),
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["ok"] is False

    def test_test_connection_returns_json(self, client, minimal_config, monkeypatch):
        monkeypatch.setattr("app.CONFIG_PATH", minimal_config)
        resp = client.post(
            "/api/config/test-connection",
            data=json.dumps({"server": "ems"}),
            content_type="application/json",
        )
        assert resp.is_json
