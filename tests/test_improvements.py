"""
test_improvements.py — Güvenlik ve doğruluk düzeltmelerini kilitleyen testler.

Kapsam:
  - /api/config GET secret maskeleme
  - /api/config POST anthropic.api_key koruması
  - /api/reports path traversal engeli
  - reporter HTML escape (XSS) ve SKIP durumu
  - sender/receiver TOTP entegrasyonu
  - analyzer model konfigürasyonu
"""

import csv
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent


# ═══════════════════════════════════════════════════════════════════
#  /api/config — secret maskeleme & koruma
# ═══════════════════════════════════════════════════════════════════

class TestConfigSecretRedaction:

    def _write_cfg(self, tmp_path, monkeypatch):
        import app as app_module
        cfg_path = tmp_path / "config.yaml"
        cfg = {
            "ems": {"smtp_host": "h", "password": "cok-gizli", "totp_secret": "TOTPSECRET"},
            "anthropic": {"api_key": "sk-ant-gercek-key", "model": "claude-opus-4-8"},
        }
        cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
        monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
        return cfg_path

    def test_get_config_masks_password(self, flask_client, tmp_path, monkeypatch):
        self._write_cfg(tmp_path, monkeypatch)
        data = flask_client.get("/api/config").get_json()
        assert data["ok"] is True
        assert "cok-gizli" not in json.dumps(data)
        # UI truthiness'e bakar — maske dolu bir string olmalı
        assert data["config"]["ems"]["password"]

    def test_get_config_masks_totp_and_api_key(self, flask_client, tmp_path, monkeypatch):
        self._write_cfg(tmp_path, monkeypatch)
        data = flask_client.get("/api/config").get_json()
        raw = json.dumps(data)
        assert "TOTPSECRET" not in raw
        assert "sk-ant-gercek-key" not in raw
        assert data["config"]["anthropic"]["api_key"]

    def test_get_config_model_not_masked(self, flask_client, tmp_path, monkeypatch):
        self._write_cfg(tmp_path, monkeypatch)
        data = flask_client.get("/api/config").get_json()
        assert data["config"]["anthropic"]["model"] == "claude-opus-4-8"

    def test_save_config_preserves_api_key_when_empty(self, flask_client, tmp_path, monkeypatch):
        """UI api_key alanını boş gönderdiğinde kayıtlı key silinmemeli."""
        cfg_path = self._write_cfg(tmp_path, monkeypatch)
        payload = {"config": {"anthropic": {"api_key": "", "model": "claude-opus-4-8"}}}
        flask_client.post("/api/config", json=payload)
        saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert saved["anthropic"]["api_key"] == "sk-ant-gercek-key"

    def test_save_config_preserves_secrets_when_masked(self, flask_client, tmp_path, monkeypatch):
        """Maske değeri ('••••') geri gönderilirse gerçek secret korunmalı."""
        cfg_path = self._write_cfg(tmp_path, monkeypatch)
        payload = {"config": {
            "ems": {"smtp_host": "h", "password": "••••••••", "totp_secret": "••••••••"},
            "anthropic": {"api_key": "••••••••••••"},
        }}
        flask_client.post("/api/config", json=payload)
        saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert saved["ems"]["password"] == "cok-gizli"
        assert saved["ems"]["totp_secret"] == "TOTPSECRET"
        assert saved["anthropic"]["api_key"] == "sk-ant-gercek-key"

    def test_save_config_overwrites_api_key_when_provided(self, flask_client, tmp_path, monkeypatch):
        cfg_path = self._write_cfg(tmp_path, monkeypatch)
        payload = {"config": {"anthropic": {"api_key": "sk-ant-yeni"}}}
        flask_client.post("/api/config", json=payload)
        saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert saved["anthropic"]["api_key"] == "sk-ant-yeni"


# ═══════════════════════════════════════════════════════════════════
#  /api/reports — path traversal
# ═══════════════════════════════════════════════════════════════════

class TestReportPathTraversal:

    def test_traversal_blocked(self, flask_client, tmp_path):
        import app as app_module
        secret = tmp_path / "secret.txt"
        secret.write_text("gizli", encoding="utf-8")
        # reports dizini flask_client fixture'ında tmp_path/reports'a bakar;
        # üst dizindeki dosyaya ../ ile erişilememeli
        resp = flask_client.get("/api/reports/..%2fsecret.txt")
        assert resp.status_code == 404

    def test_absolute_path_blocked(self, flask_client):
        resp = flask_client.get("/api/reports/%2fetc%2fpasswd", follow_redirects=True)
        assert resp.status_code == 404

    def test_normal_report_still_served(self, flask_client, tmp_path):
        import app as app_module
        (app_module.REPORTS_DIR / "rapor.html").write_text("<html>ok</html>", encoding="utf-8")
        resp = flask_client.get("/api/reports/rapor.html")
        assert resp.status_code == 200
        assert b"ok" in resp.data


# ═══════════════════════════════════════════════════════════════════
#  reporter — XSS escape & SKIP
# ═══════════════════════════════════════════════════════════════════

def _result(passed=True, summary="Özet", combination="EMS/iOS ← Gmail/Android",
            scenario_type="Plain Text", issues=None, checks=None):
    return {
        "combination": combination,
        "scenario_type": scenario_type,
        "scenario_key": "plain_text",
        "test_time": "2026-07-10 10:00:00",
        "analysis": {
            "passed": passed,
            "confidence": "HIGH" if passed else "LOW",
            "checks": checks or [],
            "summary": summary,
            "issues": issues or [],
            "recommendations": [],
        },
    }


class TestReporterEscaping:

    def test_html_report_escapes_summary(self, tmp_path):
        from reporter import generate_html_report
        out = tmp_path / "r.html"
        generate_html_report(
            [_result(summary='<script>alert("xss")</script>')], str(out)
        )
        content = out.read_text(encoding="utf-8")
        assert "<script>alert" not in content
        assert "&lt;script&gt;" in content

    def test_html_report_escapes_check_details(self, tmp_path):
        from reporter import generate_html_report
        out = tmp_path / "r.html"
        checks = [{"name": "<img src=x onerror=alert(1)>", "passed": False,
                   "detail": "<b>bozuk</b>"}]
        generate_html_report([_result(passed=False, checks=checks)], str(out))
        content = out.read_text(encoding="utf-8")
        assert "<img src=x onerror" not in content

    def test_html_report_escapes_issues(self, tmp_path):
        from reporter import generate_html_report
        out = tmp_path / "r.html"
        generate_html_report(
            [_result(passed=False, issues=["<iframe src=evil>"])], str(out)
        )
        content = out.read_text(encoding="utf-8")
        assert "<iframe" not in content


class TestReporterSkipStatus:

    def test_skipped_result_not_counted_as_fail_html(self, tmp_path):
        from reporter import generate_html_report
        out = tmp_path / "r.html"
        results = [
            _result(passed=True),
            _result(passed=None, scenario_type="S/MIME", summary="Sertifika yok — atlandı"),
        ]
        generate_html_report(results, str(out))
        content = out.read_text(encoding="utf-8")
        assert "ATLANDI" in content
        # 1 geçerli senaryonun 1'i PASS → %100
        assert "100.0%" in content

    def test_skipped_result_csv_status(self, tmp_path):
        from reporter import generate_csv_results
        out = tmp_path / "r.csv"
        results = [_result(passed=True), _result(passed=None), _result(passed=False)]
        generate_csv_results(results, str(out))
        with open(out, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        assert [r["Sonuç"] for r in rows] == ["PASS", "SKIP", "FAIL"]

    def test_all_skipped_no_division_error(self, tmp_path):
        from reporter import generate_html_report
        out = tmp_path / "r.html"
        generate_html_report([_result(passed=None)], str(out))
        assert out.exists()


# ═══════════════════════════════════════════════════════════════════
#  sender / receiver — TOTP entegrasyonu
# ═══════════════════════════════════════════════════════════════════

class TestSenderTotpLogin:

    def test_totp_password_appends_code(self, server_cfg, mock_smtp):
        from sender import MailSender
        cfg = {**server_cfg, "auth_method": "totp_password", "totp_secret": "JBSWY3DPEHPK3PXP"}
        with patch("sender.generate_totp", return_value="123456"):
            s = MailSender(cfg)
            s.send_plain_text("to@test.local", "Konu", "Gövde")
        login_args = mock_smtp.login.call_args[0]
        assert login_args[1] == "secret123456"

    def test_otp_only_uses_code_alone(self, server_cfg, mock_smtp):
        from sender import MailSender
        cfg = {**server_cfg, "auth_method": "otp_only", "totp_secret": "JBSWY3DPEHPK3PXP"}
        with patch("sender.generate_totp", return_value="654321"):
            s = MailSender(cfg)
            s.send_plain_text("to@test.local", "Konu", "Gövde")
        login_args = mock_smtp.login.call_args[0]
        assert login_args[1] == "654321"

    def test_password_method_unchanged(self, server_cfg, mock_smtp):
        from sender import MailSender
        s = MailSender(server_cfg)  # auth_method verilmedi → password
        s.send_plain_text("to@test.local", "Konu", "Gövde")
        login_args = mock_smtp.login.call_args[0]
        assert login_args[1] == "secret"


class TestReceiverTotpLogin:

    def test_totp_password_appends_code(self, server_cfg, mock_imap_empty):
        from receiver import MailReceiver
        cfg = {**server_cfg, "auth_method": "totp_password", "totp_secret": "JBSWY3DPEHPK3PXP"}
        with patch("receiver.generate_totp", return_value="111222"):
            r = MailReceiver(cfg)
            r.wait_for_message("<x>", "[TEST]", wait_seconds=0,
                               max_retries=1, retry_interval=0)
        login_args = mock_imap_empty.login.call_args[0]
        assert login_args[1] == "secret111222"


# ═══════════════════════════════════════════════════════════════════
#  analyzer — refusal ve hata durumları
# ═══════════════════════════════════════════════════════════════════

class TestAnalyzerRobustness:

    def test_refusal_returns_fail(self, received_msg, combination_meta):
        import anthropic as anthropic_sdk
        from analyzer import MailAnalyzer
        resp = MagicMock()
        resp.stop_reason = "refusal"
        resp.content = []
        client = MagicMock()
        client.messages.create.return_value = resp
        with patch("analyzer.anthropic.Anthropic", return_value=client):
            a = MailAnalyzer("sk-ant-test")
            result = a.analyze("plain_text", {"msg_id": "<x>"}, received_msg, combination_meta)
        assert result["passed"] is False

    def test_connection_error_returns_fail(self, received_msg, combination_meta):
        import anthropic as anthropic_sdk
        from analyzer import MailAnalyzer
        client = MagicMock()
        client.messages.create.side_effect = anthropic_sdk.APIConnectionError(
            request=MagicMock()
        )
        with patch("analyzer.anthropic.Anthropic", return_value=client):
            a = MailAnalyzer("sk-ant-test")
            result = a.analyze("plain_text", {"msg_id": "<x>"}, received_msg, combination_meta)
        assert result["passed"] is False
        assert "erişim hatası" in result["summary"]
