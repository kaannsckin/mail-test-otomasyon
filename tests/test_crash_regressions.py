"""
test_crash_regressions.py — İki ajanla yapılan crash testinde (web/API ve
gerçek SMTP/IMAP sunucusuyla uçtan uca hat) bulunan hataların regresyon testleri.

Her test, gerçekte yeniden üretilmiş bir hatayı sabitler; yorumlarda eski
(hatalı) davranış yazılıdır.
"""

import imaplib
import io
import json
import smtplib
import socket
import ssl
import sys
import threading
import time
from email import message_from_bytes
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_mod
from analyzer import FALLBACK_BETA, VERDICT_SCHEMA, MailAnalyzer
from mail_transport import (explain_connection_error, imap_connect, smtp_connect,
                            smtp_mode, ssl_context_for)
from receiver import MailReceiver
from sender import MailSender


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch):
    for name in ("UI_PASSWORD", "UI_USERNAME", "ALLOW_NO_AUTH"):
        monkeypatch.delenv(name, raising=False)


# ═══════════════════════════════════════════════════════════════════
#  Web güvenlik kapısı
# ═══════════════════════════════════════════════════════════════════

class TestSecurityGate:

    def test_no_password_rejects_remote_clients(self, flask_client):
        # Eski: HOST=0.0.0.0 / PaaS'ta parolasız arayüz herkese açıktı
        # (config export → düz metin şifreler).
        r = flask_client.get("/api/config", environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
        assert r.status_code == 403
        assert "UI_PASSWORD" in r.get_data(as_text=True)

    def test_no_password_rejects_foreign_host_header(self, flask_client):
        # DNS rebinding: istek loopback'ten gelir ama Host saldırgan alan adıdır.
        r = flask_client.get("/api/config/export", base_url="http://evil.example:5000")
        assert r.status_code == 403

    def test_no_password_allows_localhost(self, flask_client):
        assert flask_client.get("/api/config").status_code == 200
        r = flask_client.get("/api/config", base_url="http://127.0.0.1:5000")
        assert r.status_code == 200

    def test_ipv6_loopback_allowed(self, flask_client):
        r = flask_client.get("/api/config", base_url="http://[::1]:5000",
                             environ_overrides={"REMOTE_ADDR": "::1"})
        assert r.status_code == 200

    def test_allow_no_auth_opt_out(self, flask_client, monkeypatch):
        monkeypatch.setenv("ALLOW_NO_AUTH", "1")
        r = flask_client.get("/api/config", environ_overrides={"REMOTE_ADDR": "10.0.0.9"},
                             base_url="http://10.0.0.2:5000")
        assert r.status_code == 200

    def test_health_always_open(self, flask_client, monkeypatch):
        monkeypatch.setenv("UI_PASSWORD", "x")
        r = flask_client.get("/api/health", environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
        assert r.status_code == 200

    def test_unicode_password_does_not_500(self, flask_client, monkeypatch):
        # Eski: hmac.compare_digest(str) ASCII dışı karakterde TypeError → 500
        monkeypatch.setenv("UI_PASSWORD", "güçlüŞifre")
        ok = flask_client.get("/api/config", headers={
            "Authorization": "Basic " + __import__("base64").b64encode(
                "admin:güçlüŞifre".encode("utf-8")).decode()})
        assert ok.status_code == 200
        bad = flask_client.get("/api/config", headers={
            "Authorization": "Basic " + __import__("base64").b64encode(
                "admin:şifre".encode("utf-8")).decode()})
        assert bad.status_code == 401

    def test_password_mode_allows_remote_with_credentials(self, flask_client, monkeypatch):
        monkeypatch.setenv("UI_PASSWORD", "pw")
        import base64
        r = flask_client.get("/api/config", base_url="https://app.example.com",
                             environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
                             headers={"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()})
        assert r.status_code == 200

    @pytest.mark.parametrize("headers", [
        {"Origin": "http://attacker.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost:5999"},         # aynı site, farklı port
        {"Sec-Fetch-Site": "cross-site"},
    ])
    def test_cross_site_post_rejected(self, flask_client, headers):
        # Eski: başka bir sayfa no-cors form POST'u ile config.yaml'ı ezebiliyordu.
        r = flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "x"}}},
                              headers=headers)
        assert r.status_code == 403
        assert not app_mod.CONFIG_PATH.exists()

    def test_same_origin_post_allowed(self, flask_client):
        r = flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "x"}}},
                              headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200

    def test_cross_site_get_still_allowed(self, flask_client):
        # Okuma istekleri CORS ile zaten korunur; GET engellenmez.
        r = flask_client.get("/api/health", headers={"Origin": "http://attacker.example"})
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════
#  Config dayanıklılığı
# ═══════════════════════════════════════════════════════════════════

class TestConfigHardening:

    def _import(self, client, text: bytes, name="c.yaml"):
        return client.post("/api/config/import",
                           data={"file": (io.BytesIO(text), name)},
                           content_type="multipart/form-data")

    def test_yaml_alias_bomb_rejected(self, flask_client):
        # Eski: 413 baytlık dosya her GET'te 572 MB'a açılıyordu.
        bomb = b"a: &a ['x','x','x','x','x','x','x','x','x']\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
        r = self._import(flask_client, bomb)
        assert r.status_code == 400
        assert not app_mod.CONFIG_PATH.exists()

    def test_non_utf8_and_syntax_errors_are_400(self, flask_client):
        assert self._import(flask_client, b"\xff\xfe\x00bozuk").status_code == 400
        assert self._import(flask_client, b"ems: [unclosed").status_code == 400

    def test_empty_section_import_then_save_works(self, flask_client):
        # Eski: 'outlook:' boş bırakılınca sonraki her kayıt None.get ile 500
        assert self._import(flask_client, b"outlook:\nems:\n  smtp_host: h\n").status_code == 200
        r = flask_client.post("/api/config", json={"config": {"outlook": {"password": ""}}})
        assert r.status_code == 200

    def test_non_dict_config_rejected(self, flask_client):
        # Eski: {"config":"x"} kaydediliyor, sonraki her istek 500 veriyordu
        r = flask_client.post("/api/config", json={"config": "x"})
        assert r.status_code == 400
        assert not app_mod.CONFIG_PATH.exists()

    def test_save_keeps_keys_the_form_does_not_know(self, flask_client):
        # Eski: form kaydı calendar_*, smime_*, smtp_use_ssl gibi anahtarları siliyordu
        app_mod.CONFIG_PATH.write_text(yaml.dump({
            "ems": {"smtp_host": "h", "password": "gizli", "smtp_use_ssl": True,
                    "tls_ca_file": "certs/ca.pem"},
            "test": {"calendar_location": "Toplantı Odası", "smime_cert_path": "c.pem"},
            "logging": {"level": "DEBUG"},
        }, allow_unicode=True), encoding="utf-8")
        r = flask_client.post("/api/config", json={"config": {
            "ems": {"smtp_host": "yeni", "password": ""},
            "test": {"wait_seconds": 20},
        }})
        assert r.status_code == 200
        saved = yaml.safe_load(app_mod.CONFIG_PATH.read_text(encoding="utf-8"))
        assert saved["ems"]["smtp_host"] == "yeni"
        assert saved["ems"]["password"] == "gizli"
        assert saved["ems"]["smtp_use_ssl"] is True
        assert saved["ems"]["tls_ca_file"] == "certs/ca.pem"
        assert saved["test"]["calendar_location"] == "Toplantı Odası"
        assert saved["test"]["wait_seconds"] == 20
        assert saved["logging"]["level"] == "DEBUG"

    def test_corrupt_config_reported_not_500_html(self, flask_client):
        app_mod.CONFIG_PATH.write_text("- liste\n- kök\n", encoding="utf-8")
        r = flask_client.get("/api/config")
        assert r.is_json and r.get_json()["ok"] is False


# ═══════════════════════════════════════════════════════════════════
#  Bozuk girdilerde 500 yok
# ═══════════════════════════════════════════════════════════════════

class TestNo500OnBadInput:

    def test_logs_offset_not_a_number(self, flask_client):
        r = flask_client.get("/api/run/logs?offset=abc")
        assert r.status_code == 200

    @pytest.mark.parametrize("code", [123456, None, ["1"], {"a": 1}, True])
    def test_mfa_submit_odd_types(self, flask_client, code):
        r = flask_client.post("/api/mfa/submit", json={"code": code})
        assert r.status_code == 200 and r.is_json

    def test_mfa_submit_without_pending_request(self, flask_client):
        r = flask_client.post("/api/mfa/submit", json={"code": "123456"}).get_json()
        assert r["ok"] is False and "Bekleyen" in r["error"]

    def test_totp_preview_non_string(self, flask_client):
        assert flask_client.post("/api/mfa/totp-preview", json={"secret": 5}).status_code == 200

    def test_run_start_rejects_bad_types(self, flask_client):
        # Eski: scenario liste gelince 500 — ama arka plan thread'i çoktan başlamıştı
        with patch.object(app_mod.subprocess, "Popen") as popen:
            assert flask_client.post("/api/run/start", json={"scenario": ["a"]}).status_code == 400
            assert flask_client.post("/api/run/start", json={"scenario": "yok"}).status_code == 400
            assert flask_client.post("/api/run/start", json={"combo": "x"}).status_code == 400
            assert flask_client.post("/api/run/start", json={"combo": -1}).status_code == 400
        popen.assert_not_called()

    def test_oversized_body_413_json(self, flask_client):
        r = flask_client.post("/api/config", data=b"x" * (3 * 1024 * 1024),
                              content_type="application/json")
        assert r.status_code == 413 and r.is_json


# ═══════════════════════════════════════════════════════════════════
#  Koşu yaşam döngüsü
# ═══════════════════════════════════════════════════════════════════

def _blocking_proc():
    """stdout'u release edilene kadar bloklanan sahte süreç."""
    gate = threading.Event()
    proc = MagicMock()
    proc.pid = 999999

    def lines():
        gate.wait(5)
        return
        yield  # pragma: no cover

    proc.stdout = lines()
    proc.returncode = None

    def terminate():
        proc.returncode = -15
        gate.set()
    proc.terminate.side_effect = terminate
    proc.wait.side_effect = lambda timeout=None: (gate.wait(5), proc.returncode)[1]
    return proc, gate


class TestRunLifecycle:

    @pytest.fixture(autouse=True)
    def _logs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_mod, "LOGS_DIR", tmp_path / "logs")
        (tmp_path / "logs").mkdir()
        monkeypatch.setattr(app_mod, "RUN_PID_FILE", tmp_path / "logs" / "run.pid")
        yield
        app_mod.run_state.update({"running": False, "process": None})

    def test_concurrent_starts_spawn_one_process(self, flask_client):
        # Eski: kilitsiz run_state — paralel başlatmalarda 3 main.py süreci
        proc, gate = _blocking_proc()
        codes = []
        barrier = threading.Barrier(6)

        def hit():
            client = app_mod.app.test_client()   # test istemcisi thread'ler arası paylaşılamaz
            barrier.wait()
            codes.append(client.post("/api/run/start", json={}).status_code)

        with patch.object(app_mod.subprocess, "Popen", return_value=proc) as popen:
            threads = [threading.Thread(target=hit) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert popen.call_count == 1
            assert sorted(codes) == [200] + [400] * 5
            gate.set()

    def test_child_runs_in_data_dir_with_utf8(self, flask_client):
        # Eski: cwd=BASE_DIR → DATA_DIR ayarlıyken raporlar arayüzde görünmüyordu
        proc, gate = _blocking_proc()
        gate.set()
        with patch.object(app_mod.subprocess, "Popen", return_value=proc) as popen:
            flask_client.post("/api/run/start", json={"combo": [5, 2, 5]})
        kwargs = popen.call_args.kwargs
        assert kwargs["cwd"] == str(app_mod.DATA_DIR)
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        assert kwargs["errors"] == "replace"
        assert "--combo" in popen.call_args[0][0]
        assert popen.call_args[0][0][popen.call_args[0][0].index("--combo") + 1] == "5,2"

    def test_stop_marks_stopped_and_allows_new_run(self, flask_client):
        proc, _ = _blocking_proc()
        with patch.object(app_mod.subprocess, "Popen", return_value=proc):
            assert flask_client.post("/api/run/start", json={}).status_code == 200
            assert flask_client.post("/api/run/stop").get_json()["ok"] is True
            status = flask_client.get("/api/run/status").get_json()
            assert status["running"] is False and status["stopped"] is True
            proc2, gate2 = _blocking_proc()
            with patch.object(app_mod.subprocess, "Popen", return_value=proc2):
                assert flask_client.post("/api/run/start", json={}).status_code == 200
                gate2.set()


# ═══════════════════════════════════════════════════════════════════
#  Ağ teşhisi
# ═══════════════════════════════════════════════════════════════════

class TestDiagnostics:

    def test_dns_failure_not_blamed_on_platform(self, flask_client, monkeypatch):
        # Eski: DNS çözülemeyince "platform SMTP portlarını engelliyor" deniyordu
        def fake(addr, timeout=8):
            if addr[1] == 443:
                m = MagicMock()
                m.__enter__ = MagicMock(return_value=m)
                m.__exit__ = MagicMock(return_value=False)
                return m
            raise socket.gaierror(-2, "Name or service not known")
        monkeypatch.setattr(app_mod.socket, "create_connection", fake)
        data = flask_client.get("/api/diagnostics/network").get_json()
        assert "DNS" in data["verdict"]
        assert all(t["reason"] == "dns" for t in data["targets"] if not t["reachable"])

    def test_saved_servers_are_probed(self, flask_client, monkeypatch):
        app_mod.CONFIG_PATH.write_text(yaml.dump({"ems": {
            "smtp_host": "ems.kurum.local", "smtp_port": 25, "label": "EMS"}}), encoding="utf-8")
        seen = []

        def fake(addr, timeout=8):
            seen.append(addr)
            raise TimeoutError("timed out")
        monkeypatch.setattr(app_mod.socket, "create_connection", fake)
        data = flask_client.get("/api/diagnostics/network").get_json()
        assert ("ems.kurum.local", 25) in seen
        assert any(t["reason"] == "timeout" for t in data["targets"])


# ═══════════════════════════════════════════════════════════════════
#  Analiz (LLM) katmanı
# ═══════════════════════════════════════════════════════════════════

def _resp(text, stop_reason="end_turn"):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = [block]
    return resp


def _analyzer_with(client):
    with patch("analyzer.anthropic.Anthropic", return_value=client):
        return MailAnalyzer("sk-ant-test")


class TestAnalyzerHardening:

    def test_attachment_bytes_do_not_crash_prompt(self, received_msg, combination_meta):
        # Eski: payload_sample (bytes) → json.dumps TypeError → TÜM ekli
        # senaryolar sonuçtan sessizce düşüyordu.
        msg = {**received_msg, "attachments": [{"filename": "a.pdf", "payload_sample": b"%PDF-1.4\x00"}]}
        prompt = MailAnalyzer("k")._build_prompt("attachment", {"msg_id": "<x>", "raw": b"\x00"},
                                                 msg, combination_meta)
        assert "a.pdf" in prompt and "bayt ikili veri" in prompt

    @pytest.mark.parametrize("payload, expected", [
        ({"passed": "false"}, False),       # eski: metin "false" → PASS
        ({"passed": "true"}, True),
        ({"summary": "passed yok"}, False),  # eski: SKIP sayılıp paydadan düşüyordu
        ({"passed": 1}, False),
    ])
    def test_verdict_passed_is_validated(self, payload, expected):
        v = MailAnalyzer("k")._parse_response(json.dumps(payload), "plain_text")
        assert v["passed"] is expected
        if expected is False and "passed" not in payload or payload.get("passed") == 1:
            assert v["confidence"] == "LOW"

    def test_checks_as_string_normalized(self):
        # Eski: checks metin gelince reporter çöküyor, HİÇ rapor yazılmıyordu
        v = MailAnalyzer("k")._parse_response(
            json.dumps({"passed": True, "checks": "hepsi tamam", "issues": "yok"}), "plain_text")
        assert v["checks"] == [] and v["issues"][0] == "yok"
        assert v["confidence"] == "LOW"

    def test_json_followed_by_braces_text(self):
        # Eski: açgözlü regex '{.*}' JSON sonrası '}' içeren metinde bozuluyordu
        text = '{"passed": true, "confidence": "HIGH", "checks": []} Not: {ek} açıklama'
        assert MailAnalyzer("k")._parse_response(text, "plain_text")["passed"] is True

    def test_markdown_fenced_json(self):
        text = '```json\n{"passed": false, "confidence": "MEDIUM"}\n```'
        v = MailAnalyzer("k")._parse_response(text, "plain_text")
        assert v["passed"] is False and v["confidence"] == "MEDIUM"

    def test_request_uses_structured_output_and_fallback(self, received_msg, combination_meta):
        client = MagicMock()
        client.beta.messages.create.return_value = _resp(json.dumps({"passed": True}))
        a = _analyzer_with(client)
        a.analyze("plain_text", {"msg_id": "<x>"}, received_msg, combination_meta)
        kw = client.beta.messages.create.call_args.kwargs
        assert kw["model"] == "claude-opus-5-5"
        assert kw["max_tokens"] >= 16000            # thinking bütçesi için
        assert kw["output_config"]["format"]["schema"] == VERDICT_SCHEMA
        assert kw["output_config"]["effort"] == "medium"
        assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"

    def test_old_model_400_falls_back_to_plain_request_once(self, received_msg, combination_meta):
        client = MagicMock()
        client.beta.messages.create.side_effect = anthropic.BadRequestError(
            "output_config not supported",
            response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None)
        client.messages.create.return_value = _resp('{"passed": true, "confidence": "HIGH"}')
        a = _analyzer_with(client)
        for _ in range(2):
            v = a.analyze("plain_text", {"msg_id": "<x>"}, received_msg, combination_meta)
            assert v["passed"] is True
        assert client.beta.messages.create.call_count == 1   # yapışkan: tekrar denenmez
        assert client.messages.create.call_count == 2

    def test_max_tokens_cutoff_is_fail(self, received_msg, combination_meta):
        client = MagicMock()
        client.beta.messages.create.return_value = _resp('{"passed": tr', stop_reason="max_tokens")
        v = _analyzer_with(client).analyze("plain_text", {}, received_msg, combination_meta)
        assert v["passed"] is False and "kesildi" in v["summary"]

    def test_unexpected_sdk_error_is_fail_not_crash(self, received_msg, combination_meta):
        # Eski: bozuk yanıt gövdesi (JSONDecodeError) senaryoyu düşürüyordu
        client = MagicMock()
        client.beta.messages.create.side_effect = json.JSONDecodeError("x", "y", 0)
        v = _analyzer_with(client).analyze("plain_text", {}, received_msg, combination_meta)
        assert v["passed"] is False

    def test_model_not_found_message(self, received_msg, combination_meta):
        client = MagicMock()
        client.beta.messages.create.side_effect = anthropic.NotFoundError(
            "model", response=httpx.Response(404, request=httpx.Request("POST", "https://x")), body=None)
        v = _analyzer_with(client).analyze("plain_text", {}, received_msg, combination_meta)
        assert "Model bulunamadı" in v["summary"]


# ═══════════════════════════════════════════════════════════════════
#  TLS / bağlantı katmanı
# ═══════════════════════════════════════════════════════════════════

class TestTransport:

    def test_port_465_uses_implicit_ssl(self):
        # Eski: 465'e düz SMTP ile bağlanıp 30 sn bekleniyordu
        assert smtp_mode({"smtp_port": 465, "smtp_use_tls": True}) == "ssl"
        assert smtp_mode({"smtp_port": 465, "smtp_use_ssl": False}) == "starttls"
        assert smtp_mode({"smtp_port": 587}) == "starttls"
        assert smtp_mode({"smtp_port": 25, "smtp_use_tls": False}) == "plain"

    def test_smtp_ssl_path(self):
        with patch("smtplib.SMTP_SSL") as cls:
            smtp_connect({"smtp_host": "h", "smtp_port": 465})
        assert isinstance(cls.call_args.kwargs["context"], ssl.SSLContext)

    def test_starttls_missing_extension_explained(self):
        smtp = MagicMock()
        smtp.has_extn.return_value = False
        with patch("smtplib.SMTP", return_value=smtp), pytest.raises(smtplib.SMTPNotSupportedError):
            smtp_connect({"smtp_host": "h", "smtp_port": 587})

    def test_tls_verify_false(self):
        ctx = ssl_context_for({"tls_verify": False})
        assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False
        assert ssl_context_for({"tls_verify": "false"}).verify_mode == ssl.CERT_NONE

    def test_ca_file_missing_is_clear_error(self):
        with pytest.raises(FileNotFoundError, match="tls_ca_file"):
            ssl_context_for({"tls_ca_file": "yok/kurum-ca.pem"})

    def test_ca_file_used(self, tmp_path):
        ca = tmp_path / "ca.pem"
        ca.write_text("x")
        with patch("mail_transport.ssl.create_default_context") as cdc:
            ssl_context_for({"tls_ca_file": str(ca)})
        assert cdc.call_args.kwargs["cafile"] == str(ca)

    def test_imap_has_timeout_and_optional_starttls(self):
        # Eski: IMAP'te timeout yoktu — yanlış TLS modunda süreç sonsuza dek asılıyordu
        imap = MagicMock()
        with patch("imaplib.IMAP4", return_value=imap) as cls:
            imap_connect({"imap_host": "h", "imap_port": 143, "imap_use_ssl": False,
                          "imap_use_starttls": True})
        assert cls.call_args.kwargs["timeout"] == 30
        imap.starttls.assert_called_once()

    @pytest.mark.parametrize("exc, needle", [
        (ssl.SSLCertVerificationError("certificate verify failed: self-signed"), "tls_ca_file"),
        (ssl.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number"), "TLS modu"),
        (socket.gaierror(-2, "Name or service not known"), "DNS"),
        (smtplib.SMTPAuthenticationError(535, b"bad"), "reddedildi"),
        (ConnectionRefusedError(111, "refused"), "reddedildi"),
        (TimeoutError("timed out"), "zaman aşımı"),
    ])
    def test_errors_explained(self, exc, needle):
        assert needle in (explain_connection_error(exc, {"smtp_port": 587}) or "")

    def test_gmail_auth_hint(self):
        hint = explain_connection_error(smtplib.SMTPAuthenticationError(535, b"x"),
                                        {"smtp_host": "smtp.gmail.com"})
        assert "App Password" in hint


# ═══════════════════════════════════════════════════════════════════
#  IMAP alıcı
# ═══════════════════════════════════════════════════════════════════

SERVER = {"imap_host": "h", "imap_port": 993, "username": "u", "password": 123456,
          "test_address": "u@h"}


def _raw(msg_id="<hedef@test>", subject="[AUTO-TEST] x"):
    return (f"Message-ID: {msg_id}\r\nSubject: {subject}\r\nFrom: a@b\r\n\r\ngövde\r\n").encode()


class TestReceiverHardening:

    def test_numeric_yaml_password_cast_to_str(self):
        # Eski: 'password: 123456' int geliyordu → imaplib AttributeError
        assert MailReceiver(SERVER).password == "123456"
        assert MailSender({**SERVER, "smtp_host": "h", "smtp_port": 587}).password == "123456"

    def test_server_side_message_id_search_and_peek(self):
        # Eski: yalnızca son 20 '[AUTO-TEST]' mesaja bakılıyor, RFC822 ile
        # tamamı indiriliyor ve mesajlar okundu işaretleniyordu.
        imap = MagicMock()
        imap.select.return_value = ("OK", [b"1"])
        imap.search.return_value = ("OK", [b"2057"])
        imap.fetch.return_value = ("OK", [(b"2057 (BODY[] {80}", _raw())])
        with patch("imaplib.IMAP4_SSL", return_value=imap), patch("receiver.time.sleep"):
            res = MailReceiver(SERVER).wait_for_message("<hedef@test>", "[OTOMASYON-TESTİ]",
                                                        wait_seconds=0, max_retries=1)
        assert res is not None
        assert imap.search.call_args[0][1:3] == ("HEADER", "Message-ID")
        assert imap.fetch.call_args[0][1] == "(BODY.PEEK[])"

    def test_fallback_scan_when_header_search_empty(self):
        imap = MagicMock()
        imap.select.return_value = ("OK", [b"1"])
        imap.search.side_effect = [("OK", [b""]), ("OK", [b"1 2 3"])]
        imap.fetch.side_effect = [
            ("OK", [(b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {20}", b"Message-ID: <baska@x>\r\n\r\n"), b")",
                    (b"3 (BODY[HEADER.FIELDS (MESSAGE-ID)] {20}", b"Message-ID: <hedef@test>\r\n\r\n"), b")"]),
            ("OK", [(b"3 (BODY[] {80}", _raw())]),
        ]
        with patch("imaplib.IMAP4_SSL", return_value=imap), patch("receiver.time.sleep"):
            res = MailReceiver(SERVER).wait_for_message("<hedef@test>", "x", wait_seconds=0, max_retries=1)
        assert res is not None and res["imap_id"] == "3"
        assert imap.search.call_args_list[1][0][1] == "SINCE"

    def test_auth_error_stops_immediately_and_is_reported(self):
        # Eski: yanlış şifreyle 30 bağlantı × 2 giriş (hesap kilitleme) ve
        # sonuç 'teslim edilemedi — timeout' diye raporlanıyordu.
        imap = MagicMock()
        imap.login.side_effect = imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        with patch("imaplib.IMAP4_SSL", return_value=imap) as cls, patch("receiver.time.sleep"):
            r = MailReceiver(SERVER)
            assert r.wait_for_message("<x@y>", "p", wait_seconds=0, max_retries=5) is None
        assert cls.call_count == 1
        assert r.is_auth_error(r.last_error)

    def test_connection_error_kept_as_last_error(self):
        with patch("imaplib.IMAP4_SSL", side_effect=ssl.SSLCertVerificationError("verify failed")), \
             patch("receiver.time.sleep"):
            r = MailReceiver(SERVER)
            r.wait_for_message("<x@y>", "p", wait_seconds=0, max_retries=2)
        assert isinstance(r.last_error, ssl.SSLError)

    def test_attachment_head_is_json_safe_and_rfc822_has_inner_headers(self):
        from email.mime.application import MIMEApplication
        from email.mime.message import MIMEMessage
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        outer = MIMEMultipart()
        outer["Message-ID"] = "<o@t>"
        pdf = MIMEApplication(b"%PDF-1.4 icerik", "pdf")
        pdf.add_header("Content-Disposition", "attachment", filename="a.pdf")
        outer.attach(pdf)
        inner = MIMEText("orijinal", "plain", "utf-8")
        inner["Subject"] = "Orijinal Konu"
        inner["From"] = "ilk@x"
        fwd = MIMEMessage(inner)
        fwd.add_header("Content-Disposition", "attachment", filename="iletilen.eml")
        outer.attach(fwd)
        raw = outer.as_bytes()
        details = MailReceiver(SERVER)._extract_details(message_from_bytes(raw), raw, "1")
        json.dumps({k: v for k, v in details.items() if k != "raw_bytes"})  # TypeError olmamalı
        pdf_info = next(a for a in details["attachments"] if a.get("filename") == "a.pdf")
        assert pdf_info["payload_head_hex"].startswith(b"%PDF".hex())
        rfc = next(a for a in details["attachments"] if a["content_type"] == "message/rfc822")
        assert rfc["size"] > 0 and rfc["encapsulated_headers"]["Subject"] == "Orijinal Konu"


# ═══════════════════════════════════════════════════════════════════
#  SMTP gönderici
# ═══════════════════════════════════════════════════════════════════

class TestSenderHardening:

    def test_non_ascii_attachment_filename_rfc2231(self, tmp_path):
        # Eski: tüm Content-Disposition tek encoded-word'e dönüşüyor, istemciler
        # (ve aracın kendi alıcısı) eki göremiyordu.
        f = tmp_path / "Rapor_Şubat_ğüş.pdf"
        f.write_bytes(b"%PDF-1.4")
        sender = MailSender({"smtp_host": "h", "smtp_port": 587, "username": "u",
                             "password": "p", "test_address": "u@h"})
        captured = {}
        sender._send = lambda msg, to: captured.setdefault("msg", msg) and 0.0
        sender.send_with_attachment("a@b", "konu", "gövde", str(f))
        raw = captured["msg"].as_bytes()
        assert b"filename*=utf-8''Rapor_" in raw
        parsed = message_from_bytes(raw)
        att = [p for p in parsed.walk() if p.get_content_disposition() == "attachment"]
        assert att and att[0].get_filename() == "Rapor_Şubat_ğüş.pdf"


# ═══════════════════════════════════════════════════════════════════
#  CSV okuyucu
# ═══════════════════════════════════════════════════════════════════

class TestCsvEncodings:

    def test_excel_tr_cp1254_semicolon_matches_utf8(self, tmp_path):
        # Eski: Excel-TR kaydı (cp1254 + ';') UnicodeDecodeError ya da
        # "18 kombinasyon, 0 senaryo" veriyordu.
        from csv_parser import parse_csv
        src = Path(__file__).parent.parent / "mail_test_checklist.csv"
        import csv as _csv
        rows = list(_csv.reader(src.read_text(encoding="utf-8-sig").splitlines()))
        out = io.StringIO()
        _csv.writer(out, delimiter=";").writerows(rows)
        excel = tmp_path / "excel_tr.csv"
        excel.write_bytes(out.getvalue().encode("cp1254", errors="replace"))
        a, b = parse_csv(str(src)), parse_csv(str(excel))
        assert len(a) == len(b) > 0
        assert [len(c.scenarios) for c in a] == [len(c.scenarios) for c in b]


# ═══════════════════════════════════════════════════════════════════
#  2. tur crash test bulguları
# ═══════════════════════════════════════════════════════════════════

class TestRound2:

    @pytest.mark.parametrize("header", [
        {"X-Forwarded-For": "203.0.113.7"}, {"X-Real-IP": "203.0.113.7"},
        {"Forwarded": "for=203.0.113.7"}, {"X-Forwarded-Host": "public.example"},
    ])
    def test_host_rewriting_proxy_cannot_bypass_no_password_mode(self, flask_client, header):
        # nginx 'proxy_pass http://127.0.0.1:PORT' istek loopback'ten ve
        # 'Host: localhost' ile gelir; eskiden export düz metin şifre veriyordu.
        r = flask_client.get("/api/config/export", headers=header)
        assert r.status_code == 403

    def test_password_mode_behind_host_rewriting_proxy_can_save(self, flask_client, monkeypatch):
        # Eski: Origin (genel adres) ≠ yeniden yazılmış Host → her kayıt 403
        import base64
        monkeypatch.setenv("UI_PASSWORD", "pw")
        r = flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "h"}}},
                              headers={"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode(),
                                       "Origin": "https://mail.example.com",
                                       "X-Forwarded-Host": "mail.example.com"})
        assert r.status_code == 200

    def test_ipv4_mapped_loopback_is_local(self):
        assert app_mod._is_loopback_addr("::ffff:127.0.0.1") is True
        assert app_mod._is_loopback_addr("::ffff:10.0.0.1") is False

    @pytest.mark.parametrize("change", [
        {"smtp_port": 2525},                         # aynı host, başka (şifresiz) port
        {"smtp_use_tls": False},
        {"tls_verify": False},
        {"tls_ca_file": "saldirgan-ca.pem"},
        {"imap_port": 143, "imap_use_ssl": False},
    ])
    def test_saved_password_not_reused_when_transport_changes(self, flask_client, change):
        base = {"smtp_host": "smtp.t.local", "smtp_port": 587, "smtp_use_tls": True,
                "imap_host": "imap.t.local", "imap_port": 993, "username": "u@t.local",
                "password": "kayitli-sifre"}
        app_mod.CONFIG_PATH.write_text(yaml.dump({"ems": base}), encoding="utf-8")
        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp), patch("imaplib.IMAP4_SSL"), \
             patch("imaplib.IMAP4"):
            r = flask_client.post("/api/config/test-connection", json={
                "server": "ems", "server_config": {**base, **change, "password": ""}})
        assert r.get_json()["ok"] is False
        for call in smtp.login.call_args_list:
            assert "kayitli-sifre" not in call[0][1]

    def test_unchanged_target_still_reuses_saved_password(self, flask_client):
        base = {"smtp_host": "smtp.t.local", "smtp_port": 587, "smtp_use_tls": False,
                "username": "u@t.local", "password": "kayitli-sifre"}
        app_mod.CONFIG_PATH.write_text(yaml.dump({"ems": base}), encoding="utf-8")
        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp):
            r = flask_client.post("/api/config/test-connection", json={
                "server": "ems", "server_config": {**base, "smtp_port": "587", "password": ""}})
        assert r.get_json()["ok"] is True
        smtp.login.assert_called_once_with("u@t.local", "kayitli-sifre")

    def test_secret_can_be_cleared_with_null(self, flask_client):
        app_mod.CONFIG_PATH.write_text(yaml.dump({
            "ems": {"password": "eski", "totp_secret": "JBSWY3DPEHPK3PXP"},
            "anthropic": {"api_key": "sk-ant-eski"}}), encoding="utf-8")
        flask_client.post("/api/config", json={"config": {
            "ems": {"password": "", "totp_secret": None}, "anthropic": {"api_key": None}}})
        saved = yaml.safe_load(app_mod.CONFIG_PATH.read_text(encoding="utf-8"))
        assert saved["ems"]["password"] == "eski"          # boş → korunur
        assert saved["ems"]["totp_secret"] == ""           # null → silinir
        assert saved["anthropic"]["api_key"] == ""

    def test_stop_clears_mfa_bridge(self, flask_client):
        proc = MagicMock()
        app_mod.run_state.update({"running": True, "process": proc, "run_id": "r"})
        try:
            with patch.object(app_mod.mfa_manager, "clear_bridge") as clear:
                assert flask_client.post("/api/run/stop").get_json()["ok"] is True
            clear.assert_called()
        finally:
            app_mod.run_state.update({"running": False, "process": None})


class TestRound2MailPipeline:

    def _conn_err(self):
        return anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))

    def test_circuit_breaker_stops_calling_dead_llm(self, received_msg, combination_meta):
        # Eski: ölü uç noktada senaryo başına ~6 dk → tam matris ~20 saat
        client = MagicMock()
        client.beta.messages.create.side_effect = self._conn_err()
        a = _analyzer_with(client)
        results = [a.analyze("plain_text", {}, received_msg, combination_meta) for _ in range(6)]
        assert client.beta.messages.create.call_count == 3
        assert all(r["passed"] is False for r in results)
        assert "art arda 3" in results[-1]["summary"]

    def test_success_resets_breaker(self, received_msg, combination_meta):
        client = MagicMock()
        ok = _resp('{"passed": true}')
        client.beta.messages.create.side_effect = [self._conn_err(), self._conn_err(), ok,
                                                   self._conn_err(), self._conn_err(), ok]
        a = _analyzer_with(client)
        for _ in range(6):
            a.analyze("plain_text", {}, received_msg, combination_meta)
        assert client.beta.messages.create.call_count == 6

    def test_invalid_api_key_opens_breaker_immediately(self, received_msg, combination_meta):
        client = MagicMock()
        client.beta.messages.create.side_effect = anthropic.AuthenticationError(
            "bad key", response=httpx.Response(401, request=httpx.Request("POST", "https://x")), body=None)
        a = _analyzer_with(client)
        for _ in range(4):
            v = a.analyze("plain_text", {}, received_msg, combination_meta)
        assert client.beta.messages.create.call_count == 1
        assert "API key" in v["summary"]

    def test_one_off_400_does_not_disable_structured_output(self, received_msg, combination_meta):
        # Eski: herhangi bir 400 (ör. aşırı uzun prompt) koşunun geri kalanında
        # yapılandırılmış çıktıyı kapatıyordu.
        err = anthropic.BadRequestError(
            "prompt is too long", response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
            body=None)
        client = MagicMock()
        client.beta.messages.create.side_effect = [err, _resp('{"passed": true}')]
        client.messages.create.side_effect = err
        a = _analyzer_with(client)
        assert a.analyze("plain_text", {}, received_msg, combination_meta)["passed"] is False
        assert a.analyze("plain_text", {}, received_msg, combination_meta)["passed"] is True
        assert client.beta.messages.create.call_count == 2

    @pytest.mark.parametrize("text", ['[{"passed": true}]', "true", '"metin"', "[1, 2]"])
    def test_non_object_json_is_parse_failure_not_crash(self, text):
        v = MailAnalyzer("k")._parse_response(text, "plain_text")
        assert v["passed"] is False and v["confidence"] == "LOW"

    def test_excel_tr_question_mark_arrows(self):
        # cp1254 '→' taşıyamaz → 'Alan ? EMS'; eskiden tüm sunucular "Bilinmeyen sunucu: ?"
        from csv_parser import _parse_combination_header
        info = _parse_combination_header("🔀 Kombinasyon: Alan ? EMS / iOS  Gönderen ? Gmail / Android")
        assert info["receiver_server"] == "EMS" and info["sender_server"] == "Gmail"
        info = _parse_combination_header("Kombinasyon: Alan -> EMS / iOS  Gönderen -> Outlook / Outlook")
        assert info["sender_server"] == "Outlook"

    def test_partial_report_is_marked(self, tmp_path):
        from reporter import generate_html_report
        out = tmp_path / "r.html"
        results = [{"combination": "A", "scenario_type": "plain_text",
                    "analysis": {"passed": True, "checks": [], "summary": "ok"}}]
        generate_html_report(results, str(out), note="Koşu DURDURULDU — kısmi")
        assert "DURDURULDU" in out.read_text(encoding="utf-8")
        generate_html_report(results, str(out))
        assert "DURDURULDU" not in out.read_text(encoding="utf-8")

    def test_auth_hints_are_specific(self, monkeypatch):
        monkeypatch.delenv("MFA_INTERACTIVE", raising=False)
        hint = explain_connection_error(smtplib.SMTPAuthenticationError(535, b"x"),
                                        {"auth_method": "totp_password", "totp_secret": ""})
        assert "totp_secret boş" in hint
        hint = explain_connection_error(
            smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server."), {})
        assert "smtp_use_tls" in hint

    def test_timeout_summary_leads_with_specific_hint(self):
        import main as main_mod
        from csv_parser import TestCombination
        combo = TestCombination(label="X", receiver_server="EMS", receiver_client="iOS",
                                sender_server="Gmail", sender_client="Android")
        sender = MagicMock()
        sender.config = {"smtp_host": "h", "smtp_port": 2465}
        sender.send_plain_text.side_effect = TimeoutError("timed out")
        receiver = MagicMock()
        receiver.config = {"test_address": "a@b"}
        res = main_mod.run_scenario("plain_text", combo, 0, sender, receiver, MagicMock(),
                                    {"wait_seconds": 0, "max_retries": 1, "retry_interval": 0})
        summary = res["analysis"]["summary"]
        assert summary.index("smtp_use_ssl") < summary.index("giden SMTP portuna")


# ═══════════════════════════════════════════════════════════════════
#  3. tur crash test — yüksek öncelikli bulgular
# ═══════════════════════════════════════════════════════════════════

class TestConcurrentConfigSave:
    """Eski: kilitsiz oku-birleştir-yaz + ortak config.yaml.tmp → 40 paralel
    kayıtta 10 adet 500, 40 bölümden 1'i kaldı, mevcut ems/şifre silindi."""

    def test_parallel_saves_keep_every_section(self, flask_client):
        app_mod.CONFIG_PATH.write_text(
            yaml.dump({"ems": {"smtp_host": "h", "password": "REALPW"}}), encoding="utf-8")
        codes, errors = [], []

        def save(i):
            try:
                with app_mod.app.test_client() as c:
                    r = c.post("/api/config", json={"config": {f"sec{i}": {"k": i}}})
                    codes.append(r.status_code)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=save, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert codes == [200] * 40
        saved = yaml.safe_load(app_mod.CONFIG_PATH.read_text(encoding="utf-8"))
        assert {f"sec{i}" for i in range(40)} <= set(saved)
        assert saved["ems"]["password"] == "REALPW"
        assert not list(app_mod.CONFIG_PATH.parent.glob("config.yaml*.tmp"))

    def test_failed_write_leaves_no_temp_file(self, flask_client, monkeypatch):
        def boom(data, f):
            raise OSError("disk dolu")
        monkeypatch.setattr(app_mod, "_yaml_dump", boom)
        r = flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "h"}}})
        assert r.status_code == 500
        assert r.get_json()["ok"] is False
        assert not list(app_mod.CONFIG_PATH.parent.glob("config.yaml*.tmp"))


class _WireCapture:
    """Ham DATA satırlarını kaydeden minimal SMTP sunucusu."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.data_lines = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.sock.accept()
        f = conn.makefile("rb")
        conn.sendall(b"220 test\r\n")
        in_data = False
        while True:
            line = f.readline()
            if not line:
                break
            if in_data:
                self.data_lines.append(line)
                if line.rstrip(b"\r\n") == b".":
                    in_data = False
                    conn.sendall(b"250 ok\r\n")
                continue
            cmd = line.upper()
            if cmd.startswith(b"DATA"):
                in_data = True
                conn.sendall(b"354 go\r\n")
            elif cmd.startswith(b"QUIT"):
                conn.sendall(b"221 bye\r\n")
                break
            elif cmd.startswith(b"EHLO"):
                conn.sendall(b"250-test\r\n250 AUTH PLAIN LOGIN\r\n")
            elif cmd.startswith(b"AUTH"):
                conn.sendall(b"235 ok\r\n")
            else:
                conn.sendall(b"250 ok\r\n")
        conn.close()
        self.sock.close()


class TestSmtpWireLineEndings:
    """Eski: msg.as_bytes() yalın LF üretiyordu; smtplib bytes'ı normalize
    etmediği için katı RFC 5321 sunucuları (aiosmtpd, Exim >= 4.97) ~1000
    baytı aşan mesajı "500 Line too long" ile reddetti."""

    @pytest.mark.parametrize("scenario", ["plain_text", "html", "attachment"])
    def test_data_uses_crlf_only(self, scenario, tmp_path):
        cap = _WireCapture()
        cfg = {"smtp_host": "127.0.0.1", "smtp_port": cap.port, "smtp_use_tls": False,
               "username": "u@test.local", "password": "p", "test_address": "a@test.local"}
        sender = MailSender(cfg)
        body = "satır ğüşıöç\n" * 400   # > 1000 bayt, çok satırlı
        if scenario == "plain_text":
            meta = sender.send_plain_text("b@test.local", "[T] crlf", body)
        elif scenario == "html":
            meta = sender.send_html_message("b@test.local", "[T] crlf", f"<p>{body}</p>", body)
        else:
            pdf = tmp_path / "ek.pdf"
            pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 5000)
            meta = sender.send_with_attachment("b@test.local", "[T] crlf", body, [str(pdf)])
        cap.thread.join(timeout=10)
        assert meta.get("sent_at")
        assert cap.data_lines, "DATA alınmadı"
        bad = [l for l in cap.data_lines if not l.endswith(b"\r\n") or l.endswith(b"\r\r\n")]
        assert bad == []
        assert all(len(l) <= 1000 for l in cap.data_lines)


class TestReceiverOddHeaders:
    """Eski: geçerli/yarı geçerli header'lar mesaj ayrıştırmayı çökertiyor,
    teslim edilmiş mesaj "IMAP kontrol edilemedi" FAIL'ine dönüşüyordu."""

    def _details(self, raw: bytes) -> dict:
        r = MailReceiver.__new__(MailReceiver)
        return r._extract_details(message_from_bytes(raw), raw, "1")

    def test_raw_8bit_utf8_from_and_subject(self):
        # Eski: From → AttributeError('Header' .split), Subject → LookupError('unknown-8bit')
        raw = ("From: Ünal Çelik <a@b.c>\r\nTo: Şule <d@e.f>\r\nSubject: Çağrı şöyle\r\n"
               "Message-ID: <1@x>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
               "merhaba\r\n").encode("utf-8")
        d = self._details(raw)
        assert d["headers"]["message_id"] == "<1@x>"
        assert "Çağrı" in d["headers"]["subject"]
        assert "Ünal" in d["headers"]["from"] and "a@b.c" in d["headers"]["from"]

    @pytest.mark.parametrize("value", ["=?foo?q?x?=", "=?unknown-8bit?b?w4c=?=",
                                       "Önce =?x-yok?Q?abc?= sonra"])
    def test_unknown_charset_encoded_word(self, value):
        out = MailReceiver._decode_header(value)
        assert isinstance(out, str) and out

    def test_unknown_charset_in_attachment_and_inner_message(self):
        raw = (b"Subject: =?foo?q?dis?=\r\nMessage-ID: <2@x>\r\nMIME-Version: 1.0\r\n"
               b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
               b"--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
               b"--B\r\nContent-Type: application/pdf\r\n"
               b"Content-Disposition: attachment; filename=\"=?foo?q?ek?=.pdf\"\r\n"
               b"Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n"
               b"--B\r\nContent-Type: message/rfc822\r\n\r\n"
               b"Subject: =?bar?q?ic?=\r\nFrom: \xc3\x9cnal <x@y.z>\r\n\r\nic govde\r\n"
               b"--B--\r\n")
        d = self._details(raw)
        assert d["headers"]["message_id"] == "<2@x>"
        assert any(a["filename"].endswith(".pdf") for a in d["attachments"])
        fwd = [a for a in d["attachments"] if a["content_type"] == "message/rfc822"]
        assert fwd and "Ünal" in fwd[0]["encapsulated_headers"]["From"]

    def test_8bit_message_found_by_wait_for_message(self):
        raw = ("From: Ünal <a@b.c>\r\nSubject: [TEST] Çağrı\r\nMessage-ID: <3@x>\r\n"
               "Content-Type: text/plain; charset=utf-8\r\n\r\nhi\r\n").encode("utf-8")
        r = MailReceiver({"imap_host": "h", "imap_port": 993, "username": "u",
                          "password": "p", "test_address": "u@h"})
        with patch.object(r, "_connect", return_value=MagicMock()), \
             patch.object(r, "_candidates_by_header", return_value=[b"1"]), \
             patch.object(r, "_fetch_full", return_value=raw):
            found = r.wait_for_message("<3@x>", "[TEST]", wait_seconds=0,
                                       max_retries=1, retry_interval=0)
        assert r.last_error is None
        assert found is not None
        assert found["headers"]["message_id"] == "<3@x>"
        assert "Çağrı" in found["headers"]["subject"]
