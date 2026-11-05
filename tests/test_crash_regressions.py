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
