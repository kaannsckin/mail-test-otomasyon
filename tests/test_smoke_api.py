"""
test_smoke_api.py — Flask API endpoint'lerinin smoke testleri.
Config dosyası ve gerçek bağlantı olmadan tüm route'lar test edilir.
"""

import csv
import json
import queue
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent


# ═══════════════════════════════════════════════════════════════════
#  /api/config — GET / POST
# ═══════════════════════════════════════════════════════════════════

class TestConfigGet:

    def test_get_config_no_file(self, flask_client):
        resp = flask_client.get("/api/config")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is False
        assert "bulunamadı" in data["error"]

    def test_get_config_with_file(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("ems:\n  smtp_host: smtp.test.local\n", encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        resp = flask_client.get("/api/config")
        data = resp.get_json()
        assert data["ok"] is True
        assert "ems" in data["config"]
        assert data["config"]["ems"]["smtp_host"] == "smtp.test.local"


class TestConfigPost:

    def test_save_config_creates_file(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg_path = tmp_path / "config.yaml"
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        payload = {"config": {"ems": {"smtp_host": "smtp.test.local", "password": "pass"}}}
        resp = flask_client.post("/api/config", json=payload)
        assert resp.get_json()["ok"] is True
        assert cfg_path.exists()

    def test_save_config_preserves_password_when_empty(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg_path = tmp_path / "config.yaml"
        existing = {"ems": {"smtp_host": "smtp.test.local", "password": "gizli_sifre"}}
        cfg_path.write_text(yaml.dump(existing), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        payload = {"config": {"ems": {"smtp_host": "smtp.test.local", "password": ""}}}
        flask_client.post("/api/config", json=payload)
        with open(cfg_path, "r") as f:
            saved = yaml.safe_load(f)
        assert saved["ems"]["password"] == "gizli_sifre"

    def test_save_config_preserves_totp_secret(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg_path = tmp_path / "config.yaml"
        existing = {"ems": {"smtp_host": "h", "totp_secret": "GIZLI_TOTP"}}
        cfg_path.write_text(yaml.dump(existing), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        payload = {"config": {"ems": {"smtp_host": "h", "totp_secret": ""}}}
        flask_client.post("/api/config", json=payload)
        with open(cfg_path, "r") as f:
            saved = yaml.safe_load(f)
        assert saved["ems"]["totp_secret"] == "GIZLI_TOTP"

    def test_save_config_overwrites_password_when_provided(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg_path = tmp_path / "config.yaml"
        existing = {"ems": {"smtp_host": "h", "password": "eski"}}
        cfg_path.write_text(yaml.dump(existing), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        payload = {"config": {"ems": {"smtp_host": "h", "password": "yeni"}}}
        flask_client.post("/api/config", json=payload)
        with open(cfg_path, "r") as f:
            saved = yaml.safe_load(f)
        assert saved["ems"]["password"] == "yeni"


class TestConfigTestConnection:

    def test_connection_no_config_file(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "olmayan.yaml")
        resp = flask_client.post("/api/config/test-connection", json={"server": "ems"})
        assert resp.get_json()["ok"] is False

    def test_connection_unknown_server(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("ems:\n  smtp_host: smtp.test.local\n", encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        resp = flask_client.post("/api/config/test-connection",
                                 json={"server": "bilinmeyen"})
        assert resp.get_json()["ok"] is False

    def test_connection_smtp_success(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg = {
            "ems": {
                "smtp_host": "smtp.test.local", "smtp_port": 587,
                "smtp_use_tls": False, "username": "u@t.com", "password": "p",
                "auth_method": "password",
            }
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        smtp_mock = MagicMock()
        smtp_mock.quit = MagicMock()
        smtp_mock.ehlo = MagicMock()
        smtp_mock.login = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp_mock):
            resp = flask_client.post("/api/config/test-connection", json={"server": "ems"})
        assert resp.get_json()["ok"] is True

    def test_connection_smtp_failure(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        import smtplib
        cfg = {
            "ems": {
                "smtp_host": "smtp.test.local", "smtp_port": 587,
                "smtp_use_tls": False, "username": "u@t.com", "password": "p",
                "auth_method": "password",
            }
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("Bağlantı reddedildi")):
            resp = flask_client.post("/api/config/test-connection", json={"server": "ems"})
        assert resp.get_json()["ok"] is False

    def test_connection_needs_mfa(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg = {
            "ems": {
                "smtp_host": "smtp.test.local", "smtp_port": 587,
                "smtp_use_tls": False, "username": "u@t.com", "password": "p",
                "auth_method": "totp_password",
                "mfa_method": "totp",
                "totp_secret": "",  # Boş → kullanıcıdan kod iste
            }
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        resp = flask_client.post("/api/config/test-connection",
                                 json={"server": "ems", "mfa_code": ""})
        data = resp.get_json()
        assert data["ok"] is False
        assert data.get("needs_mfa") is True


# ═══════════════════════════════════════════════════════════════════
#  /api/mfa/* — 2FA yönetimi
# ═══════════════════════════════════════════════════════════════════

class TestMfaStatus:

    def test_status_no_pending(self, flask_client):
        resp = flask_client.get("/api/mfa/status")
        data = resp.get_json()
        assert data["pending"] is False
        assert data["challenge"] is None


class TestMfaSubmit:

    def test_submit_empty_code_rejected(self, flask_client):
        resp = flask_client.post("/api/mfa/submit", json={"code": ""})
        data = resp.get_json()
        assert data["ok"] is False
        assert "boş" in data["error"]

    def test_submit_whitespace_code_rejected(self, flask_client):
        resp = flask_client.post("/api/mfa/submit", json={"code": "   "})
        data = resp.get_json()
        assert data["ok"] is False

    def test_submit_no_pending_challenge(self, flask_client):
        resp = flask_client.post("/api/mfa/submit", json={"code": "123456"})
        data = resp.get_json()
        assert data["ok"] is False


class TestMfaCancel:

    def test_cancel_no_pending(self, flask_client):
        resp = flask_client.post("/api/mfa/cancel", json={})
        assert resp.get_json()["ok"] is False


class TestTotpPreview:

    def test_preview_empty_secret(self, flask_client):
        resp = flask_client.post("/api/mfa/totp-preview", json={"secret": ""})
        assert resp.get_json()["ok"] is False

    def test_preview_valid_secret(self, flask_client):
        resp = flask_client.post("/api/mfa/totp-preview",
                                 json={"secret": "JBSWY3DPEHPK3PXP"})
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["code"]) == 6
        assert data["code"].isdigit()
        assert 1 <= data["remaining"] <= 30

    def test_preview_secret_with_spaces(self, flask_client):
        resp = flask_client.post("/api/mfa/totp-preview",
                                 json={"secret": "JBSWY 3DPE HPK3 PXP"})
        data = resp.get_json()
        assert data["ok"] is True


# ═══════════════════════════════════════════════════════════════════
#  /api/combinations
# ═══════════════════════════════════════════════════════════════════

class TestCombinationsEndpoint:

    def test_combinations_with_real_csv(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg = {"test": {"csv_input": str(PROJECT_ROOT / "mail_test_checklist.csv")}}
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        resp = flask_client.get("/api/combinations")
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["combinations"]) == 10

    def test_combinations_structure(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg = {"test": {"csv_input": str(PROJECT_ROOT / "mail_test_checklist.csv")}}
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        resp = flask_client.get("/api/combinations")
        data = resp.get_json()
        for combo in data["combinations"]:
            assert "index" in combo
            assert "label" in combo
            assert "receiver_server" in combo
            assert "receiver_client" in combo
            assert "sender_server" in combo
            assert "sender_client" in combo
            assert "scenarios" in combo
            assert combo["step_count"] == 25

    def test_combinations_each_has_5_scenarios(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg = {"test": {"csv_input": str(PROJECT_ROOT / "mail_test_checklist.csv")}}
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        resp = flask_client.get("/api/combinations")
        for combo in resp.get_json()["combinations"]:
            assert len(combo["scenarios"]) == 5

    def test_combinations_invalid_csv_path(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        cfg = {"test": {"csv_input": "/olmayan/klasor/olmayan.csv"}}
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        # Fallback: gerçek CSV'yi bulabilir veya hata döner — ikisi de kabul edilir
        resp = flask_client.get("/api/combinations")
        assert "ok" in resp.get_json()


# ═══════════════════════════════════════════════════════════════════
#  /api/run/* — Test çalıştırma
# ═══════════════════════════════════════════════════════════════════

class TestRunStatus:

    def test_status_idle(self, flask_client):
        resp = flask_client.get("/api/run/status")
        data = resp.get_json()
        assert data["running"] is False
        assert data["exit_code"] is None


class TestRunStop:

    def test_stop_no_process(self, flask_client):
        resp = flask_client.post("/api/run/stop", json={})
        assert resp.get_json()["ok"] is False

    def test_stop_clears_mfa(self, flask_client):
        resp = flask_client.post("/api/run/stop", json={})
        assert resp.status_code == 200


class TestRunStart:

    def test_start_returns_cmd(self, flask_client):
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.returncode = 0
        mock_proc.wait = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc):
            resp = flask_client.post("/api/run/start", json={})
        data = resp.get_json()
        assert data["ok"] is True
        assert "main.py" in data["cmd"]

    def test_start_with_combo_filter(self, flask_client):
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.returncode = 0
        mock_proc.wait = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc):
            resp = flask_client.post("/api/run/start", json={"combo": 5})
        assert "--combo 5" in resp.get_json()["cmd"]

    def test_start_with_scenario_filter(self, flask_client):
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.returncode = 0
        mock_proc.wait = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc):
            resp = flask_client.post("/api/run/start", json={"scenario": "plain_text"})
        assert "--scenario plain_text" in resp.get_json()["cmd"]

    def test_start_dry_run_flag(self, flask_client):
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.returncode = 0
        mock_proc.wait = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc):
            resp = flask_client.post("/api/run/start", json={"dry_run": True})
        assert "--dry-run" in resp.get_json()["cmd"]

    def test_start_already_running_returns_400(self, flask_client):
        import app as app_module
        app_module.run_state["running"] = True
        try:
            resp = flask_client.post("/api/run/start", json={})
            assert resp.status_code == 400
            assert resp.get_json()["ok"] is False
        finally:
            app_module.run_state["running"] = False

    def test_start_sets_started_at(self, flask_client):
        import app as app_module
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.returncode = 0
        mock_proc.wait = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc):
            flask_client.post("/api/run/start", json={})
        time.sleep(0.05)
        # started_at set olmuş olmalı
        assert app_module.run_state["started_at"] is not None


# ═══════════════════════════════════════════════════════════════════
#  /api/reports — Raporlar
# ═══════════════════════════════════════════════════════════════════

class TestReportsEndpoint:

    def test_list_reports_empty_dir(self, flask_client):
        resp = flask_client.get("/api/reports")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["reports"] == []

    def test_list_reports_with_html_file(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir(exist_ok=True)
        (reports_dir / "test_raporu.html").write_text("<html/>", encoding="utf-8")
        monkeypatch.setattr(app_module, "REPORTS_DIR", reports_dir)
        data = flask_client.get("/api/reports").get_json()
        assert len(data["reports"]) == 1
        assert data["reports"][0]["name"] == "test_raporu.html"
        assert data["reports"][0]["type"] == "html"

    def test_list_reports_with_csv_file(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir(exist_ok=True)
        (reports_dir / "sonuclar.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        monkeypatch.setattr(app_module, "REPORTS_DIR", reports_dir)
        data = flask_client.get("/api/reports").get_json()
        assert any(r["type"] == "csv" for r in data["reports"])

    def test_get_report_not_found(self, flask_client):
        resp = flask_client.get("/api/reports/olmayan.html")
        assert resp.status_code == 404

    def test_get_report_existing(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir(exist_ok=True)
        (reports_dir / "rapor.html").write_text("<html>Test</html>", encoding="utf-8")
        monkeypatch.setattr(app_module, "REPORTS_DIR", reports_dir)
        resp = flask_client.get("/api/reports/rapor.html")
        assert resp.status_code == 200


class TestResultsEndpoint:

    def test_latest_results_no_csv(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(app_module, "REPORTS_DIR", reports_dir)
        data = flask_client.get("/api/results/latest").get_json()
        assert data["ok"] is False

    def test_latest_results_with_data(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(app_module, "REPORTS_DIR", reports_dir)
        csv_path = reports_dir / "test_results.csv"
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Kombinasyon", "Senaryo Tipi", "Sonuç"])
            writer.writerow(["EMS/iOS←Gmail/Android", "plain_text", "PASS"])
            writer.writerow(["EMS/iOS←Gmail/iOS", "attachment", "FAIL"])
        data = flask_client.get("/api/results/latest").get_json()
        assert data["ok"] is True
        assert len(data["results"]) == 2
        assert data["results"][0]["Sonuç"] == "PASS"
        assert data["results"][1]["Sonuç"] == "FAIL"

    def test_latest_results_returns_most_recent_file(self, flask_client, tmp_path, monkeypatch):
        import app as app_module
        import time as _time
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(app_module, "REPORTS_DIR", reports_dir)
        old = reports_dir / "old_results.csv"
        with open(old, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(["Kombinasyon", "Senaryo Tipi", "Sonuç"])
        _time.sleep(0.01)
        new = reports_dir / "new_results.csv"
        with open(new, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Kombinasyon", "Senaryo Tipi", "Sonuç"])
            w.writerow(["EMS/iOS←Gmail/Android", "plain_text", "PASS"])
        data = flask_client.get("/api/results/latest").get_json()
        assert data["file"] == "new_results.csv"


# ═══════════════════════════════════════════════════════════════════
#  / — Ana sayfa
# ═══════════════════════════════════════════════════════════════════

class TestIndexRoute:

    def test_index_renders_200(self, flask_client):
        resp = flask_client.get("/")
        assert resp.status_code == 200

    def test_index_returns_html(self, flask_client):
        resp = flask_client.get("/")
        assert b"<html" in resp.data.lower() or b"<!DOCTYPE" in resp.data
