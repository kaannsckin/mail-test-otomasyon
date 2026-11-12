"""
test_medium_regressions.py — 3. tur crash testinin orta öncelikli bulguları.

Her test gerçekte yeniden üretilmiş bir hatayı sabitler; yorumlarda eski
(hatalı) davranış yazılıdır.
"""

import io
import json
import os
import smtplib
import sys
from email import message_from_bytes
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_mod
import main as main_mod
from analyzer import MailAnalyzer
from mail_transport import connection_fingerprint, smtp_mode
from receiver import MailReceiver
from sender import MailSender
from tests.test_orchestrator import (_analyzer_mock, _combo, _receiver_mock, _run_cli,
                                     _sender_mock, cli_env, test_cfg)  # noqa: F401


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch):
    for name in ("UI_PASSWORD", "UI_USERNAME", "ALLOW_NO_AUTH"):
        monkeypatch.delenv(name, raising=False)


def _saved():
    return yaml.safe_load(app_mod.CONFIG_PATH.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════
#  Bozuk config üzerine kayıt — veri kaybı
# ═══════════════════════════════════════════════════════════════════

class TestCorruptConfigSave:

    def test_invalid_section_keeps_other_sections_and_backs_up(self, flask_client):
        # Eski: tek bölüm geçersizse kayıt existing={} ile yapılıyor, API key ve
        # Gmail şifresi yedeksiz siliniyordu.
        original = ("anthropic:\n  api_key: sk-REAL-KEY\ngmail:\n  password: GMAILPW\n"
                    "ems: oops-not-a-mapping\n")
        app_mod.CONFIG_PATH.write_text(original, encoding="utf-8")
        r = flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "h"}}})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True and "yedeği" in body["warning"]
        saved = _saved()
        assert saved["anthropic"]["api_key"] == "sk-REAL-KEY"
        assert saved["gmail"]["password"] == "GMAILPW"
        assert saved["ems"]["smtp_host"] == "h"
        backups = list(app_mod.CONFIG_PATH.parent.glob("config.yaml.bozuk-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == original

    def test_unparseable_yaml_is_backed_up_before_repair(self, flask_client):
        original = "anthropic:\n  api_key: sk-REAL-KEY\n  bad: [unclosed\n"
        app_mod.CONFIG_PATH.write_text(original, encoding="utf-8")
        r = flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "h"}}})
        assert r.status_code == 200 and r.get_json().get("warning")
        assert _saved()["ems"]["smtp_host"] == "h"
        backups = list(app_mod.CONFIG_PATH.parent.glob("config.yaml.bozuk-*.bak"))
        assert [b.read_text(encoding="utf-8") for b in backups] == [original]

    def test_valid_config_save_has_no_warning_or_backup(self, flask_client):
        app_mod.CONFIG_PATH.write_text("ems:\n  smtp_host: a\n", encoding="utf-8")
        r = flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "b"}}})
        assert r.get_json() == {"ok": True}
        assert not list(app_mod.CONFIG_PATH.parent.glob("*.bak"))


# ═══════════════════════════════════════════════════════════════════
#  Geçersiz port → 500
# ═══════════════════════════════════════════════════════════════════

class TestPortValidation:

    @pytest.mark.parametrize("port", ["abc", "1e999", "0", 70000, -5, {"a": 1}, [587], True, 1.5e300])
    def test_save_rejects_invalid_port(self, flask_client, port):
        # Eski: "abc" kaydediliyor, sonra diagnostics/test-connection 500 veriyordu
        r = flask_client.post("/api/config", json={"config": {"ems": {"smtp_port": port}}})
        assert r.status_code == 400
        assert "port" in r.get_json()["error"]
        assert not app_mod.CONFIG_PATH.exists()

    def test_save_normalizes_numeric_string_port(self, flask_client):
        r = flask_client.post("/api/config", json={"config": {"ems": {
            "smtp_port": " 587 ", "imap_port": "993", "smtp_host": "h"}}})
        assert r.status_code == 200
        assert _saved()["ems"]["smtp_port"] == 587 and _saved()["ems"]["imap_port"] == 993

    def test_zero_port_from_old_ui_means_unset(self, flask_client):
        # Arayüz boş port alanını eskiden 0 olarak gönderiyordu; kayıt 400 olmamalı
        r = flask_client.post("/api/config", json={"config": {"ems": {
            "smtp_host": "h", "smtp_port": 0, "imap_port": 0}}})
        assert r.status_code == 200
        assert _saved()["ems"]["smtp_port"] == "" and _saved()["ems"]["imap_port"] == ""

    def test_empty_port_allowed(self, flask_client):
        r = flask_client.post("/api/config", json={"config": {"ems": {"smtp_port": ""}}})
        assert r.status_code == 200

    def test_import_rejects_invalid_port(self, flask_client):
        r = flask_client.post("/api/config/import", content_type="multipart/form-data",
                              data={"file": (io.BytesIO(b"ems:\n  smtp_port: abc\n"), "c.yaml")})
        assert r.status_code == 400 and "port" in r.get_json()["error"]

    @pytest.mark.parametrize("port", ["abc", "1e999", ".inf", "{a: 1}"])
    def test_diagnostics_survives_hand_edited_bad_port(self, flask_client, monkeypatch, port):
        app_mod.CONFIG_PATH.write_text(
            f"ems:\n  smtp_host: mail.kurum.local\n  smtp_port: {port}\n"
            "  imap_host: mail.kurum.local\n  imap_port: 993\n", encoding="utf-8")

        def refuse(addr, timeout=None):
            raise ConnectionRefusedError(111, "refused")
        monkeypatch.setattr(app_mod.socket, "create_connection", refuse)
        monkeypatch.setattr(app_mod.socket, "getaddrinfo", lambda *a, **k: [])
        r = flask_client.get("/api/diagnostics/network")
        assert r.status_code == 200
        labels = [t["label"] for t in r.get_json().get("targets", r.get_json().get("results", []))]
        assert not any("EMS SMTP (config)" in l for l in labels)   # geçersiz port atlandı
        assert any("EMS IMAP (config)" in l for l in labels)       # geçerli olan denendi

    @pytest.mark.parametrize("port", ["abc", {"a": 1}, "1e999"])
    def test_test_connection_bad_form_port_is_json(self, flask_client, port):
        r = flask_client.post("/api/config/test-connection", json={
            "server": "ems",
            "server_config": {"smtp_host": "127.0.0.1", "smtp_port": port, "password": "x"}})
        assert r.status_code == 200
        assert r.get_json()["ok"] is False

    def test_transport_helpers_tolerate_bad_port(self):
        assert smtp_mode({"smtp_port": "abc"}) == "starttls"
        assert smtp_mode({"smtp_port": float("inf")}) == "starttls"
        connection_fingerprint({"smtp_port": float("inf")})


# ═══════════════════════════════════════════════════════════════════
#  Derin iç içe YAML → RecursionError 500
# ═══════════════════════════════════════════════════════════════════

DEEP = ("a: " + "[" * 5000 + "]" * 5000 + "\n").encode()


class TestDeepNesting:

    def test_import_deep_yaml_is_400(self, flask_client):
        r = flask_client.post("/api/config/import", content_type="multipart/form-data",
                              data={"file": (io.BytesIO(DEEP), "c.yaml")})
        assert r.status_code == 400
        assert r.get_json()["ok"] is False

    def test_deep_file_on_disk_does_not_500(self, flask_client):
        app_mod.CONFIG_PATH.write_bytes(b"ems:\n  smtp_host: h\n" + DEEP)
        r = flask_client.get("/api/config")
        assert r.is_json and r.get_json()["ok"] is False
        # Kaydetme bozuk dosyayı yedekleyip onarabilmeli (eskiden 500, onarılamıyordu)
        r = flask_client.post("/api/config", json={"config": {"gmail": {"smtp_host": "g"}}})
        assert r.status_code == 200
        assert _saved()["gmail"]["smtp_host"] == "g"

    def test_deep_json_post_is_400(self, flask_client):
        value = "x"
        for _ in range(900):
            value = [value]
        r = flask_client.post("/api/config", json={"config": {"ems": {"deep": value}}})
        assert r.status_code == 400
        assert not list(app_mod.CONFIG_PATH.parent.glob("config.yaml*.tmp"))


# ═══════════════════════════════════════════════════════════════════
#  Content-Disposition büyük/küçük harf
# ═══════════════════════════════════════════════════════════════════

def _mixed(disposition: str, content_id: str = "") -> bytes:
    cid = f"Content-ID: <{content_id}>\r\n" if content_id else ""
    return (b"Subject: s\r\nMessage-ID: <d@x>\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
            b"--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
            b"--B\r\nContent-Type: image/png\r\n"
            + f"Content-Disposition: {disposition}\r\n{cid}".encode()
            + b"Content-Transfer-Encoding: base64\r\n\r\niVBORw0KGgo=\r\n--B--\r\n")


class TestContentDisposition:

    def _counts(self, raw):
        r = MailReceiver.__new__(MailReceiver)
        d = r._extract_details(message_from_bytes(raw), raw, "1")
        return len(d["attachments"]), len(d["inline_images"]), len(d["parts"])

    @pytest.mark.parametrize("disp", ["Attachment; filename=a.png", "ATTACHMENT; filename=a.png",
                                      " attachment ; filename=a.png"])
    def test_attachment_case_insensitive(self, disp):
        # Eski: "Attachment" ek sayılmıyordu → (0, 0, 3), yanlış FAIL
        assert self._counts(_mixed(disp)) == (1, 0, 2)

    @pytest.mark.parametrize("disp", ["Inline", "INLINE; filename=x.png"])
    def test_inline_case_insensitive(self, disp):
        assert self._counts(_mixed(disp, "img@x")) == (0, 1, 2)

    def test_filename_containing_attachment_is_still_inline(self):
        # Eski: "inline; filename=attachment.png" ek sayılıyordu
        assert self._counts(_mixed("inline; filename=attachment.png", "img@x")) == (0, 1, 2)


# ═══════════════════════════════════════════════════════════════════
#  LLM kararı kendi kontrolleriyle çelişemez
# ═══════════════════════════════════════════════════════════════════

class TestVerdictConsistency:

    def _parse(self, payload):
        return MailAnalyzer(api_key="k")._parse_response(json.dumps(payload), "attachment")

    def test_pass_with_failed_check_becomes_fail(self):
        # Eski: passed=true + başarısız kontrol + issues → PASS kalıyordu
        v = self._parse({"passed": True, "confidence": "HIGH", "summary": "x",
                         "checks": [{"name": "Ek", "passed": False, "detail": "yok"},
                                    {"name": "Konu", "passed": True, "detail": "ok"}],
                         "issues": ["Ek eksik"]})
        assert v["passed"] is False
        assert v["confidence"] == "LOW"
        assert any("Ek" in i and "PASS" in i for i in v["issues"])

    def test_pass_without_checks_is_low_confidence(self):
        v = self._parse({"passed": True, "confidence": "HIGH", "summary": "x", "checks": []})
        assert v["passed"] is True
        assert v["confidence"] == "LOW"
        assert v["issues"]

    def test_consistent_pass_untouched(self):
        v = self._parse({"passed": True, "confidence": "HIGH", "summary": "x",
                         "checks": [{"name": "Ek", "passed": True, "detail": "ok"}]})
        assert v["passed"] is True and v["confidence"] == "HIGH" and v["issues"] == []


# ═══════════════════════════════════════════════════════════════════
#  Gemini devre kesici
# ═══════════════════════════════════════════════════════════════════

def _gemini_resp(status, body=None):
    req = httpx.Request("POST", "https://gemini.test")
    if isinstance(body, (dict, list)):
        return httpx.Response(status, json=body, request=req)
    return httpx.Response(status, text=body or "", request=req)


class TestGeminiCircuitBreaker:

    def _run(self, responses, n=10, received_msg=None, combination_meta=None):
        a = MailAnalyzer(api_key="g", provider="gemini")
        with patch("analyzer.httpx.post", side_effect=responses) as post:
            results = [a.analyze("plain_text", {}, received_msg, combination_meta)
                       for _ in range(n)]
        return post.call_count, results

    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_auth_or_model_error_opens_circuit(self, status, received_msg, combination_meta):
        # Eski: kalıcı 403'te 10 analizin 10'u da gerçek HTTP çağrısı yapıyordu
        calls, results = self._run(lambda *a, **k: _gemini_resp(status, {"error": {}}),
                                   received_msg=received_msg, combination_meta=combination_meta)
        assert calls == 1
        assert all(r["passed"] is False for r in results)
        assert "yapılmadı" in results[-1]["summary"]

    def test_repeated_outage_opens_circuit(self, received_msg, combination_meta):
        calls, results = self._run(
            lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("t")),
            received_msg=received_msg, combination_meta=combination_meta)
        assert calls == 3
        assert all(r["passed"] is False for r in results)

    def test_success_resets_outage_counter(self, received_msg, combination_meta):
        ok = {"candidates": [{"content": {"parts": [{"text": json.dumps(
            {"passed": True, "confidence": "HIGH",
             "checks": [{"name": "c", "passed": True, "detail": ""}]})}]}}]}
        seq = [_gemini_resp(503), _gemini_resp(503), _gemini_resp(200, ok),
               _gemini_resp(503), _gemini_resp(503), _gemini_resp(200, ok)]
        calls, results = self._run(seq, n=6, received_msg=received_msg,
                                   combination_meta=combination_meta)
        assert calls == 6
        assert results[2]["passed"] is True and results[5]["passed"] is True

    @pytest.mark.parametrize("body,needle", [
        ({"promptFeedback": {"blockReason": "SAFETY"}}, "SAFETY"),
        ({"candidates": [{"finishReason": "MAX_TOKENS", "content": {}}]}, "MAX_TOKENS"),
    ])
    def test_empty_or_blocked_has_clear_message(self, body, needle, received_msg, combination_meta):
        # Eski: "Gemini API erişim hatası: 'candidates'" gibi ham KeyError metni
        _, results = self._run([_gemini_resp(200, body)], n=1,
                               received_msg=received_msg, combination_meta=combination_meta)
        assert results[0]["passed"] is False
        assert needle in results[0]["summary"]
        assert "'candidates'" not in results[0]["summary"]


# ═══════════════════════════════════════════════════════════════════
#  Geçici (4xx) SMTP AUTH hatası sunucuyu kilitlemez
# ═══════════════════════════════════════════════════════════════════

class TestTransientAuth:

    def test_4xx_auth_is_not_lockout(self):
        assert main_mod._is_auth_failure(smtplib.SMTPAuthenticationError(454, b"temp")) is False
        assert main_mod._is_auth_failure(smtplib.SMTPAuthenticationError(535, b"bad")) is True

    def test_454_does_not_block_remaining_scenarios(self, test_cfg):
        # Eski: ilk senaryodaki 454 sonrası kalan senaryolar "daha önce
        # reddedildi — yeniden denenmedi" diye denenmeden FAIL oluyordu.
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        sender.send_plain_text.side_effect = smtplib.SMTPAuthenticationError(454, b"temp fail")
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert res["analysis"]["passed"] is False
        assert "auth_failed_server" not in res

    def test_535_still_locks_out(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        sender.send_plain_text.side_effect = smtplib.SMTPAuthenticationError(535, b"bad creds")
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert res["auth_failed_server"] == "Gmail"


# ═══════════════════════════════════════════════════════════════════
#  Rapor yazılamazsa koşu başarılı bitmez
# ═══════════════════════════════════════════════════════════════════

class TestReportWrite:

    def _set_outputs(self, cli_env, html, csv):
        cfg = yaml.safe_load(cli_env.read_text(encoding="utf-8"))
        cfg["test"]["report_output"] = str(html)
        cfg["test"]["results_csv"] = str(csv)
        cli_env.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")

    def test_missing_parent_dirs_are_created(self, cli_env, tmp_path):
        # Eski: yalnızca reports/ oluşturuluyordu; özel bir yoldaki rapor hep yazılamıyordu
        html, csv = tmp_path / "a" / "b" / "r.html", tmp_path / "c" / "r.csv"
        self._set_outputs(cli_env, html, csv)
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "run_scenario", return_value={
                 "combination": "c", "scenario_type": "plain_text", "scenario_key": "plain_text",
                 "test_time": "t", "analysis": {"passed": True, "confidence": "HIGH",
                                                "checks": [], "summary": "ok", "issues": [],
                                                "recommendations": []}}):
            _run_cli(["--config", str(cli_env), "--combo", "0", "--scenario", "plain_text"])
        assert html.exists() and csv.exists()

    def test_unwritable_report_exits_with_error(self, cli_env, tmp_path, caplog):
        # Eski: "HTML rapor yazılamadı" loglanıp "1/1 PASS", çıkış 0
        blocker = tmp_path / "dosya"
        blocker.write_text("x")
        self._set_outputs(cli_env, blocker / "r.html", blocker / "r.csv")
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "run_scenario", return_value={"analysis": {"passed": True}}), \
             caplog.at_level("INFO"), pytest.raises(SystemExit) as exc:
            _run_cli(["--config", str(cli_env), "--combo", "0", "--scenario", "plain_text"])
        assert exc.value.code == main_mod.EXIT_ERROR
        assert "YAZILAMADI" in caplog.text


# ═══════════════════════════════════════════════════════════════════
#  S/MIME imzalama hatası yutulmaz
# ═══════════════════════════════════════════════════════════════════

class TestSendErrorDict:

    def test_smime_sign_error_fails_fast(self, test_cfg, tmp_path):
        # Eski: tam bekleme + tüm denemeler sonunda "teslim edilemedi — timeout"
        cert = tmp_path / "c.pem"; cert.write_text("cert")
        key = tmp_path / "k.pem"; key.write_text("key")
        cfg = {**test_cfg, "smime_cert_path": str(cert), "smime_key_path": str(key)}
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        sender.send_smime_signed.return_value = {
            "msg_id": "<s@t>", "sent_at": None, "scenario": "smime",
            "error": "unable to load signing key"}
        res = main_mod.run_scenario("smime", _combo(), 0, sender, receiver, analyzer, cfg)
        receiver.wait_for_message.assert_not_called()
        analyzer.analyze.assert_not_called()
        assert res["analysis"]["passed"] is False
        assert "signing key" in res["analysis"]["summary"]

    def test_missing_openssl_returns_error_and_cleans_temp(self, tmp_path, monkeypatch):
        # Eski: FileNotFoundError fırlıyor, imzasız geçici .eml dosyası kalıyordu
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        sender = MailSender({"smtp_host": "h", "smtp_port": 587, "username": "u",
                             "password": "p", "test_address": "a@b.c"})
        monkeypatch.setattr("sender.shutil.which", lambda name: "/usr/bin/openssl")
        with patch("subprocess.run", side_effect=FileNotFoundError("openssl")):
            meta = sender.send_smime_signed("d@e.f", "s", "body", "c.pem", "k.pem")
        assert "openssl" in meta["error"]
        assert meta["sent_at"] is None
        assert not list(tmp_path.glob("*.eml"))
