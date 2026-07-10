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
#  HTTP Basic Auth (UI_PASSWORD)
# ═══════════════════════════════════════════════════════════════════

class TestBasicAuth:

    def test_no_password_env_no_auth_required(self, flask_client, monkeypatch):
        monkeypatch.delenv("UI_PASSWORD", raising=False)
        assert flask_client.get("/api/run/status").status_code == 200

    def test_password_set_unauthenticated_401(self, flask_client, monkeypatch):
        monkeypatch.setenv("UI_PASSWORD", "parola123")
        resp = flask_client.get("/api/run/status")
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers

    def test_password_set_wrong_credentials_401(self, flask_client, monkeypatch):
        import base64
        monkeypatch.setenv("UI_PASSWORD", "parola123")
        creds = base64.b64encode(b"admin:yanlis").decode()
        resp = flask_client.get("/api/run/status",
                                headers={"Authorization": f"Basic {creds}"})
        assert resp.status_code == 401

    def test_password_set_correct_credentials_200(self, flask_client, monkeypatch):
        import base64
        monkeypatch.setenv("UI_PASSWORD", "parola123")
        creds = base64.b64encode(b"admin:parola123").decode()
        resp = flask_client.get("/api/run/status",
                                headers={"Authorization": f"Basic {creds}"})
        assert resp.status_code == 200

    def test_custom_username(self, flask_client, monkeypatch):
        import base64
        monkeypatch.setenv("UI_PASSWORD", "parola123")
        monkeypatch.setenv("UI_USERNAME", "testci")
        ok = base64.b64encode(b"testci:parola123").decode()
        bad = base64.b64encode(b"admin:parola123").decode()
        assert flask_client.get("/api/run/status",
                                headers={"Authorization": f"Basic {ok}"}).status_code == 200
        assert flask_client.get("/api/run/status",
                                headers={"Authorization": f"Basic {bad}"}).status_code == 401


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
#  MFA süreçler arası köprü (dosya tabanlı)
# ═══════════════════════════════════════════════════════════════════

class TestMfaBridge:
    """CLI subprocess'i (main.py) ile Flask sürecinin dosya köprüsü üzerinden
    2FA kodu alışverişini iki ayrı MFAManager örneğiyle simüle eder."""

    def _managers(self, tmp_path):
        from auth_manager import MFAManager
        cli = MFAManager(bridge_dir=tmp_path / "bridge")
        flask_side = MFAManager(bridge_dir=tmp_path / "bridge")
        return cli, flask_side

    def test_bridge_submit_flow(self, tmp_path):
        import threading
        cli, flask_side = self._managers(tmp_path)
        results = {}

        t = threading.Thread(
            target=lambda: results.update(
                code=cli.mfa_challenge("ems", "EMS Sunucusu", method="sms")
            ),
            daemon=True,
        )
        t.start()

        # Flask tarafı challenge'ı görene kadar bekle
        pending = None
        for _ in range(40):
            pending = flask_side.get_pending()
            if pending:
                break
            import time as _t; _t.sleep(0.05)
        assert pending is not None
        assert pending["server_key"] == "ems"
        assert pending["method"] == "sms"

        assert flask_side.submit_code("998877") is True
        t.join(timeout=5)
        assert results.get("code") == "998877"

    def test_bridge_cancel_flow(self, tmp_path):
        import threading, time as _t
        cli, flask_side = self._managers(tmp_path)
        results = {}

        t = threading.Thread(
            target=lambda: results.update(
                code=cli.mfa_challenge("gmail", "Gmail", method="totp")
            ),
            daemon=True,
        )
        t.start()
        for _ in range(40):
            if flask_side.get_pending():
                break
            _t.sleep(0.05)
        assert flask_side.cancel() is True
        t.join(timeout=5)
        assert results.get("code") is None

    def test_stale_challenge_ignored(self, tmp_path):
        import json as _json, time as _t
        from auth_manager import MFAManager
        mgr = MFAManager(bridge_dir=tmp_path)
        (tmp_path / "challenge.json").write_text(_json.dumps({
            "server_key": "ems", "server_label": "EMS", "method": "totp",
            "prompt": "p", "created_at": _t.time() - 1000,
        }), encoding="utf-8")
        assert mgr.get_pending() is None
        assert not (tmp_path / "challenge.json").exists()  # temizlenmiş olmalı

    def test_code_cache_reused_for_reconnect(self, tmp_path):
        """Alınan kod kısa süre içinde aynı sunucu için tekrar sorulmadan dönmeli."""
        import threading
        cli, flask_side = self._managers(tmp_path)
        results = {}
        t = threading.Thread(
            target=lambda: results.update(
                code=cli.mfa_challenge("ems", "EMS", method="sms")
            ),
            daemon=True,
        )
        t.start()
        import time as _t
        for _ in range(40):
            if flask_side.get_pending():
                break
            _t.sleep(0.05)
        flask_side.submit_code("112233")
        t.join(timeout=5)
        assert results["code"] == "112233"
        # İkinci istek beklemeden önbellekten dönmeli
        assert cli.mfa_challenge("ems", "EMS", method="sms") == "112233"

    def test_flask_endpoints_see_bridged_challenge(self, flask_client, tmp_path, monkeypatch):
        """Subprocess'in yazdığı challenge dosyası API üzerinden görünmeli,
        submit edilen kod response dosyasına yazılmalı."""
        import json as _json, time as _t
        from auth_manager import mfa_manager
        monkeypatch.setattr(mfa_manager, "bridge_dir", tmp_path)
        (tmp_path / "challenge.json").write_text(_json.dumps({
            "server_key": "ems", "server_label": "EMS On-Prem", "method": "totp",
            "prompt": "Kodu girin", "created_at": _t.time(),
        }), encoding="utf-8")

        status = flask_client.get("/api/mfa/status").get_json()
        assert status["pending"] is True
        assert status["challenge"]["server_label"] == "EMS On-Prem"

        resp = flask_client.post("/api/mfa/submit", json={"code": "445566"})
        assert resp.get_json()["ok"] is True
        written = _json.loads((tmp_path / "response.json").read_text(encoding="utf-8"))
        assert written["code"] == "445566"

    def test_sender_falls_back_to_challenge_without_secret(self, server_cfg, mock_smtp,
                                                           monkeypatch):
        from sender import MailSender
        monkeypatch.setenv("MFA_INTERACTIVE", "1")
        cfg = {**server_cfg, "auth_method": "totp_password", "totp_secret": "",
               "label": "EMS Test"}
        with patch("sender.mfa_manager.mfa_challenge", return_value="777888") as challenge:
            s = MailSender(cfg)
            s.send_plain_text("to@test.local", "Konu", "Gövde")
        assert challenge.called
        assert mock_smtp.login.call_args[0][1] == "secret777888"

    def test_sender_no_challenge_without_interactive_env(self, server_cfg, mock_smtp,
                                                         monkeypatch):
        """Yalın CLI'da (env yok) modal akışı devreye girmemeli — bloklama riski."""
        from sender import MailSender
        monkeypatch.delenv("MFA_INTERACTIVE", raising=False)
        cfg = {**server_cfg, "auth_method": "totp_password", "totp_secret": ""}
        with patch("sender.mfa_manager.mfa_challenge") as challenge:
            s = MailSender(cfg)
            s.send_plain_text("to@test.local", "Konu", "Gövde")
        assert not challenge.called
        assert mock_smtp.login.call_args[0][1] == "secret"


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
