"""
test_login_disconnect_gemini.py — Yerel koşuda görülen iki sorunun regresyonları.

1) Gmail SMTP girişte yanıt vermeden bağlantıyı kesiyordu
   (SMTPServerDisconnected: Connection unexpectedly closed). Bu açık bir ret
   (535) olmadığı için hesabı koruyan devre kesici çalışmıyor, 18 kombinasyonun
   her senaryosu aynı hesaba yeniden giriş deniyor ve log'da açıklama yoktu.
2) Arayüzde Gemini seçilemiyordu; varsayılan model (gemini-2.0-flash) Google
   tarafından kapatılmıştı.
"""
from __future__ import annotations

import smtplib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import analyzer
import main as main_mod
import sender as sender_mod
from mail_transport import SMTPLoginDisconnected, explain_connection_error, smtp_login
from sender import MailSender

ROOT = Path(__file__).resolve().parent.parent
GMAIL = {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "username": "test@gmail.com",
         "password": "normal-sifre", "test_address": "test@gmail.com"}


class FakeSMTP:
    """Girişte davranışı ayarlanabilen sahte SMTP (EHLO'da LOGIN/PLAIN ilan eder)."""

    def __init__(self, on_login=None, on_auth=None, auth="LOGIN PLAIN XOAUTH2"):
        self.esmtp_features = {"auth": auth}
        self.on_login, self.on_auth = on_login, on_auth
        self.login_calls, self.auth_calls, self.closed = [], [], False

    def login(self, user, password):
        self.login_calls.append((user, password))
        if self.on_login:
            raise self.on_login

    def auth(self, mechanism, authobject, initial_response_ok=True):
        self.auth_calls.append((mechanism, initial_response_ok, self.user, self.password))
        if self.on_auth:
            raise self.on_auth

    def auth_login(self, challenge=None):
        return ""

    def close(self):
        self.closed = True


def disconnected():
    return smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


# ═══════════════════════════════════════════════════════════════════
#  smtp_login
# ═══════════════════════════════════════════════════════════════════

class TestSmtpLogin:

    def test_disconnect_becomes_login_disconnected(self):
        smtp = FakeSMTP(on_login=disconnected())
        with pytest.raises(SMTPLoginDisconnected) as exc:
            smtp_login(smtp, "u", "p", GMAIL)
        e = exc.value
        assert e.host == "smtp.gmail.com" and e.port == 587 and e.attempts == 1
        assert e.tried == ("PLAIN",), "smtplib PLAIN'i LOGIN'e tercih eder"
        assert "LOGIN" in e.mechanisms
        assert isinstance(e.__cause__, smtplib.SMTPServerDisconnected)

    def test_forced_mechanism_uses_auth(self):
        smtp = FakeSMTP()
        smtp_login(smtp, "u", "p", GMAIL, mechanism="LOGIN")
        assert smtp.auth_calls == [("LOGIN", False, "u", "p")] and smtp.login_calls == []

    def test_auth_rejection_passes_through(self):
        smtp = FakeSMTP(on_login=smtplib.SMTPAuthenticationError(535, b"5.7.8 BadCredentials"))
        with pytest.raises(smtplib.SMTPAuthenticationError):
            smtp_login(smtp, "u", "p", GMAIL)


# ═══════════════════════════════════════════════════════════════════
#  MailSender._connect — tek yeniden deneme
# ═══════════════════════════════════════════════════════════════════

def _sender(monkeypatch, *smtps, **cfg):
    made = list(smtps)
    calls = []

    def fake_connect(config, timeout=30):
        calls.append(config)
        return made.pop(0)
    monkeypatch.setattr(sender_mod, "smtp_connect", fake_connect)
    monkeypatch.setattr(sender_mod.time, "sleep", lambda *a: None)
    return MailSender({**GMAIL, **cfg}), calls


class TestSenderRetry:

    def test_transient_disconnect_recovers_with_auth_login(self, monkeypatch):
        first, second = FakeSMTP(on_login=disconnected()), FakeSMTP()
        s, calls = _sender(monkeypatch, first, second)
        assert s._connect() is second
        assert len(calls) == 2 and first.closed and not second.closed
        assert second.auth_calls[0][0] == "LOGIN", "ikinci deneme farklı yöntemle"

    def test_second_disconnect_raises_with_both_attempts(self, monkeypatch):
        first = FakeSMTP(on_login=disconnected())
        second = FakeSMTP(on_auth=disconnected())
        s, calls = _sender(monkeypatch, first, second)
        with pytest.raises(SMTPLoginDisconnected) as exc:
            s._connect()
        assert exc.value.attempts == 2 and exc.value.tried == ("PLAIN", "LOGIN")
        assert first.closed and second.closed and len(calls) == 2

    def test_auth_rejection_not_retried(self, monkeypatch):
        smtp = FakeSMTP(on_login=smtplib.SMTPAuthenticationError(535, b"denied"))
        s, calls = _sender(monkeypatch, smtp)
        with pytest.raises(smtplib.SMTPAuthenticationError):
            s._connect()
        assert len(calls) == 1 and smtp.closed

    def test_no_login_mechanism_retries_with_default(self, monkeypatch):
        first = FakeSMTP(on_login=disconnected(), auth="PLAIN")
        second = FakeSMTP(auth="PLAIN")
        s, _ = _sender(monkeypatch, first, second)
        s._connect()
        assert second.login_calls and not second.auth_calls

    def test_totp_fallback_skipped_on_dead_connection(self, monkeypatch):
        first = FakeSMTP(on_login=disconnected())
        second = FakeSMTP(on_auth=disconnected())
        s, _ = _sender(monkeypatch, first, second, auth_method="totp_password",
                       totp_secret="JBSWY3DPEHPK3PXP")
        with pytest.raises(SMTPLoginDisconnected):
            s._connect()
        assert len(first.login_calls) == 1, "kopan bağlantıda yalnız-şifre denemesi yapılmamalı"


# ═══════════════════════════════════════════════════════════════════
#  Açıklama
# ═══════════════════════════════════════════════════════════════════

class TestExplanation:

    def _err(self, **kw):
        return SMTPLoginDisconnected(kw.get("host", "smtp.gmail.com"), 587, ("LOGIN", "PLAIN"),
                                     2, ("PLAIN", "LOGIN"))

    def test_gmail_hint(self):
        hint = explain_connection_error(self._err(), GMAIL)
        assert "Uygulama Şifresi" in hint and "myaccount.google.com/apppasswords" in hint
        assert "2 deneme" in hint and "başka bir ağdan" in hint

    def test_generic_server_has_no_gmail_text(self):
        hint = explain_connection_error(self._err(host="mail.kurum.example"),
                                        {"smtp_host": "mail.kurum.example"})
        assert "Gmail" not in hint and "Kullanıcı adı/şifreyi" in hint

    def test_tls_verify_off_mentions_middlebox(self):
        hint = explain_connection_error(self._err(), {**GMAIL, "tls_verify": False})
        assert "tls_verify: false" in hint

    def test_found_through_exception_chain(self):
        try:
            try:
                raise self._err()
            except SMTPLoginDisconnected as e:
                raise RuntimeError("sarmalanmış") from e
        except RuntimeError as wrapped:
            assert "Uygulama Şifresi" in explain_connection_error(wrapped, GMAIL)

    def test_plain_disconnect_on_587_still_unexplained(self):
        # Girişte değil (ör. DATA sırasında) kopan bağlantı bu açıklamayı almamalı
        assert explain_connection_error(disconnected(), GMAIL) is None


# ═══════════════════════════════════════════════════════════════════
#  Orkestratör — kullanıcının log'undaki koşunun yeniden üretimi
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def cli_env(tmp_path, monkeypatch, full_config):
    monkeypatch.chdir(tmp_path)
    cfg = {**full_config}
    cfg["test"] = {**cfg["test"], "report_output": str(tmp_path / "r.html"),
                   "results_csv": str(tmp_path / "r.csv"),
                   "csv_input": str(ROOT / "mail_test_checklist.csv")}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(main_mod, "setup_logging", lambda c: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda *a, **k: None)
    return path


class TestOrchestrator:

    def test_is_auth_failure(self):
        assert main_mod._is_auth_failure(SMTPLoginDisconnected("h", 587))
        assert not main_mod._is_auth_failure(disconnected())

    def test_gmail_login_disconnect_tries_twice_for_whole_run(self, cli_env, caplog):
        """Eski: kombinasyon 0'ın 10 senaryosu × her biri 1 giriş = 10 başarısız giriş
        (tüm koşuda 18 × 10). Yeni: 2 deneme, sonra sunucu koşu boyunca denenmez."""
        made = []

        def fake_connect(config, timeout=30):
            made.append(FakeSMTP(on_login=disconnected(), on_auth=disconnected()))
            return made[-1]

        with patch.object(sender_mod, "smtp_connect", side_effect=fake_connect), \
                patch.object(main_mod, "MailReceiver"), patch.object(main_mod, "MailAnalyzer"), \
                patch.object(main_mod, "generate_html_report") as html, \
                patch.object(main_mod, "generate_csv_results"), \
                caplog.at_level("INFO"), pytest.raises(SystemExit):
            with patch("sys.argv", ["main.py", "--config", str(cli_env), "--combo", "0"]):
                main_mod.main()
        assert len(made) == 2, f"{len(made)} SMTP bağlantısı açıldı"
        results = html.call_args[0][0]
        first = next(r for r in results if r["analysis"]["passed"] is False)
        assert "girişinde sunucu bağlantıyı kesti" in first["analysis"]["summary"]
        assert "yeniden denenmedi" in results[-1]["analysis"]["summary"]
        assert "💡" in caplog.text and "Uygulama Şifresi" in caplog.text


# ═══════════════════════════════════════════════════════════════════
#  Arayüz bağlantı testi
# ═══════════════════════════════════════════════════════════════════

class TestConnectionEndpoint:

    def test_login_disconnect_explained(self, flask_client, tmp_path, monkeypatch):
        import app as app_mod
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"gmail": {**GMAIL, "imap_host": ""}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        with patch("mail_transport.smtp_connect", return_value=FakeSMTP(on_login=disconnected())):
            r = flask_client.post("/api/config/test-connection", json={"server": "gmail"})
        body = r.get_json()
        assert body["ok"] is False
        assert "Uygulama Şifresi" in str(body)


# ═══════════════════════════════════════════════════════════════════
#  Gemini
# ═══════════════════════════════════════════════════════════════════

class TestGeminiModel:

    @pytest.mark.parametrize("given,expected", [
        (None, "gemini-flash-latest"), ("", "gemini-flash-latest"),
        ("gemini-2.0-flash", "gemini-flash-latest"), ("gemini-2.0-flash-lite-001", "gemini-flash-latest"),
        ("models/gemini-1.5-flash-latest", "gemini-flash-latest"), ("gemini-pro", "gemini-flash-latest"),
        ("gemini-3.5-flash", "gemini-3.5-flash"), ("models/gemini-3.5-flash-lite", "gemini-3.5-flash-lite"),
        ("gemini-2.5-flash", "gemini-2.5-flash"), ("gemini-pro-latest", "gemini-pro-latest"),
    ])
    def test_resolve(self, given, expected):
        assert analyzer.resolve_gemini_model(given) == expected

    def test_retired_model_warns_and_requests_default(self, caplog):
        a = analyzer.MailAnalyzer("key", model="gemini-2.0-flash", provider="gemini")
        assert a.model == "gemini-flash-latest" and "kapatıldı" in caplog.text
        resp = MagicMock()
        resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
        with patch("analyzer.httpx.post", return_value=resp) as post:
            a._call_gemini("x")
        assert "/gemini-flash-latest:generateContent" in post.call_args[0][0]
        assert post.call_args.kwargs["headers"]["x-goog-api-key"] == "key"

    def test_example_config_default(self):
        cfg = yaml.safe_load((ROOT / "config.yaml.example").read_text(encoding="utf-8"))
        assert cfg["gemini"]["model"] == "gemini-flash-latest"


@pytest.fixture(scope="module")
def html():
    return (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


class TestGeminiUI:

    def test_fields_present(self, html):
        for needle in ('<select id="analysis_provider"', '<option value="gemini">',
                       '<input id="gemini_api_key" type="password"', '<input id="gemini_model"',
                       "aistudio.google.com/apikey"):
            assert needle in html, needle

    def test_load_save_and_secret_handling(self, html):
        assert "cfg.analysis = { provider: g('analysis_provider') || 'claude' };" in html
        assert "api_key: sec('gemini_api_key')" in html
        assert "'gemini_api_key'];" in html, "Sil düğmesi gemini anahtarını da kapsamalı"
        assert "if (out.gemini) delete out.gemini.api_key;" in html, "anahtar localStorage'a yazılmamalı"
        assert html.count("updateProviderUI()") >= 3

    def test_save_roundtrip_keeps_masked_key(self, flask_client, tmp_path, monkeypatch):
        import app as app_mod
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"gemini": {"api_key": "AIza-kayitli", "model": "eski"}}),
                       encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        r = flask_client.post("/api/config", json={"config": {
            "analysis": {"provider": "gemini"},
            "gemini": {"api_key": "", "model": "gemini-flash-latest"}}})
        assert r.get_json()["ok"] is True
        saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert saved["analysis"]["provider"] == "gemini"
        assert saved["gemini"] == {"api_key": "AIza-kayitli", "model": "gemini-flash-latest"}
        shown = flask_client.get("/api/config").get_json()["config"]["gemini"]["api_key"]
        assert shown != "AIza-kayitli", "anahtar tarayıcıya düz metin gitmemeli"
