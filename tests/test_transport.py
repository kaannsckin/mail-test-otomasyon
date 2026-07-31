"""
test_transport.py — sender.py / receiver.py'de test edilmemiş yollar.

Kapsam: TOTP giriş geri düşüşleri (şifre+kod reddedilirse yalnız şifre),
SSL'siz IMAP, IMAP polling yeniden deneme/hata, bozuk part çözümleme ve
S/MIME imzalama akışı (openssl CLI ile gerçek imza).
"""

import imaplib
import smtplib
import subprocess
import sys
import types
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from receiver import MailReceiver
from sender import MailSender

VALID_SECRET = "JBSWY3DPEHPK3PXP"


# ═══════════════════════════════════════════════════════════════════
#  SMTP giriş yolları
# ═══════════════════════════════════════════════════════════════════

class TestSenderLogin:

    def test_password_totp_rejected_falls_back_to_password(self, server_cfg):
        cfg = {**server_cfg, "auth_method": "totp_password", "totp_secret": VALID_SECRET}
        smtp = MagicMock()
        smtp.login.side_effect = [smtplib.SMTPAuthenticationError(535, b"reddedildi"), None]
        MailSender(cfg)._login(smtp)
        assert smtp.login.call_count == 2
        # İlk deneme şifre+kod, ikinci deneme yalnız şifre
        assert smtp.login.call_args_list[0][0][1].startswith("secret")
        assert smtp.login.call_args_list[1][0][1] == "secret"

    def test_otp_only_does_not_fall_back(self, server_cfg):
        cfg = {**server_cfg, "auth_method": "otp_only", "totp_secret": VALID_SECRET}
        smtp = MagicMock()
        MailSender(cfg)._login(smtp)
        assert smtp.login.call_count == 1
        assert smtp.login.call_args[0][1] != "secret"   # yalnızca kod

    def test_invalid_totp_secret_uses_password_only(self, server_cfg):
        cfg = {**server_cfg, "auth_method": "totp_password", "totp_secret": "GECERSIZ!!!"}
        smtp = MagicMock()
        MailSender(cfg)._login(smtp)
        smtp.login.assert_called_once_with("user@test.local", "secret")

    def test_interactive_challenge_used_when_no_secret(self, server_cfg, monkeypatch):
        monkeypatch.setenv("MFA_INTERACTIVE", "1")
        cfg = {**server_cfg, "auth_method": "totp_password", "totp_secret": "", "label": "EMS"}
        smtp = MagicMock()
        with patch("sender.mfa_manager.mfa_challenge", return_value="123456") as ch:
            MailSender(cfg)._login(smtp)
        ch.assert_called_once()
        assert smtp.login.call_args[0][1] == "secret123456"

    def test_cancelled_challenge_falls_back_to_password(self, server_cfg, monkeypatch):
        monkeypatch.setenv("MFA_INTERACTIVE", "1")
        cfg = {**server_cfg, "auth_method": "totp_password", "totp_secret": ""}
        smtp = MagicMock()
        with patch("sender.mfa_manager.mfa_challenge", return_value=None):
            MailSender(cfg)._login(smtp)
        smtp.login.assert_called_once_with("user@test.local", "secret")

    def test_connect_starts_tls_when_configured(self, server_cfg):
        cfg = {**server_cfg, "smtp_use_tls": True}
        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp):
            MailSender(cfg)._connect()
        smtp.starttls.assert_called_once()
        assert smtp.ehlo.call_count == 2

    def test_connect_skips_tls_when_disabled(self, server_cfg):
        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp):
            MailSender({**server_cfg, "smtp_use_tls": False})._connect()
        smtp.starttls.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
#  IMAP giriş / bağlantı yolları
# ═══════════════════════════════════════════════════════════════════

class TestReceiverLogin:

    def test_password_totp_rejected_falls_back_to_password(self, server_cfg):
        cfg = {**server_cfg, "auth_method": "totp_password", "totp_secret": VALID_SECRET}
        imap = MagicMock()
        imap.login.side_effect = [imaplib.IMAP4.error("reddedildi"), None]
        MailReceiver(cfg)._login(imap)
        assert imap.login.call_count == 2
        assert imap.login.call_args_list[1][0][1] == "secret"

    def test_otp_only_sends_code_alone(self, server_cfg):
        cfg = {**server_cfg, "auth_method": "otp_only", "totp_secret": VALID_SECRET}
        imap = MagicMock()
        MailReceiver(cfg)._login(imap)
        imap.login.assert_called_once()
        assert imap.login.call_args[0][1] != "secret"

    def test_interactive_challenge_used_when_no_secret(self, server_cfg, monkeypatch):
        monkeypatch.setenv("MFA_INTERACTIVE", "1")
        cfg = {**server_cfg, "auth_method": "totp_password", "totp_secret": "", "label": "EMS"}
        imap = MagicMock()
        with patch("receiver.mfa_manager.mfa_challenge", return_value="654321"):
            MailReceiver(cfg)._login(imap)
        assert imap.login.call_args[0][1] == "secret654321"

    def test_non_ssl_uses_plain_imap4(self, server_cfg):
        cfg = {**server_cfg, "imap_use_ssl": False}
        imap = MagicMock()
        with patch("imaplib.IMAP4", return_value=imap) as plain, \
             patch("imaplib.IMAP4_SSL") as ssl_cls:
            MailReceiver(cfg)._connect()
        plain.assert_called_once_with("imap.test.local", 993)
        ssl_cls.assert_not_called()
        imap.select.assert_called_once_with("INBOX")

    def test_ssl_connection_uses_default_context(self, server_cfg):
        imap = MagicMock()
        with patch("imaplib.IMAP4_SSL", return_value=imap) as cls:
            MailReceiver(server_cfg)._connect()
        assert cls.call_args.kwargs["ssl_context"] is not None


# ═══════════════════════════════════════════════════════════════════
#  IMAP polling davranışı
# ═══════════════════════════════════════════════════════════════════

class TestWaitForMessage:

    def test_imap_error_retries_then_gives_up(self, server_cfg):
        with patch("imaplib.IMAP4_SSL", side_effect=OSError("bağlantı reddedildi")), \
             patch("receiver.time.sleep"):
            result = MailReceiver(server_cfg).wait_for_message(
                "<x@t>", "[TEST]", wait_seconds=0, max_retries=3, retry_interval=0)
        assert result is None

    def test_recovers_after_transient_error(self, server_cfg, mock_imap_with_message):
        inst, msg_id = mock_imap_with_message
        calls = {"n": 0}
        real_search = inst.search

        def flaky_search(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("geçici hata")
            return real_search(*a, **k)

        inst.search = flaky_search
        with patch("receiver.time.sleep"):
            result = MailReceiver(server_cfg).wait_for_message(
                msg_id, "[TEST]", wait_seconds=0, max_retries=3, retry_interval=0)
        assert result is not None
        assert result["headers"]["message_id"] == msg_id

    def test_non_matching_message_id_ignored(self, server_cfg, mock_imap_with_message):
        with patch("receiver.time.sleep"):
            result = MailReceiver(server_cfg).wait_for_message(
                "<baska@t>", "[TEST]", wait_seconds=0, max_retries=1, retry_interval=0)
        assert result is None

    def test_logs_out_after_finding_message(self, server_cfg, mock_imap_with_message):
        inst, msg_id = mock_imap_with_message
        with patch("receiver.time.sleep"):
            MailReceiver(server_cfg).wait_for_message(
                msg_id, "[TEST]", wait_seconds=0, max_retries=1, retry_interval=0)
        inst.logout.assert_called()

    def test_initial_wait_is_honored(self, server_cfg, mock_imap_empty):
        with patch("receiver.time.sleep") as slp:
            MailReceiver(server_cfg).wait_for_message(
                "<x@t>", "[TEST]", wait_seconds=9, max_retries=1, retry_interval=0)
        assert slp.call_args_list[0][0][0] == 9


# ═══════════════════════════════════════════════════════════════════
#  MIME part çözümleme dayanıklılığı
# ═══════════════════════════════════════════════════════════════════

class TestWalkPartsRobustness:

    def test_undecodable_payload_yields_empty_preview(self, server_cfg):
        msg = MIMEText("gövde", "plain", "utf-8")
        result = {"parts": [], "attachments": [], "inline_images": []}
        with patch.object(type(msg), "get_payload", side_effect=LookupError("bilinmeyen charset")):
            MailReceiver(server_cfg)._walk_parts(msg, result)
        assert result["parts"][0]["text_preview"] == ""

    def test_wrong_charset_replaced_not_raised(self, server_cfg):
        msg = MIMEText("ğüşıöç", "plain", "utf-8")
        msg.set_param("charset", "ascii")
        result = {"parts": [], "attachments": [], "inline_images": []}
        MailReceiver(server_cfg)._walk_parts(msg, result)
        assert result["parts"]          # hata atmadan bir part üretmeli

    def test_text_preview_truncated_to_500(self, server_cfg):
        msg = MIMEText("x" * 2000, "plain", "utf-8")
        result = {"parts": [], "attachments": [], "inline_images": []}
        MailReceiver(server_cfg)._walk_parts(msg, result)
        assert len(result["parts"][0]["text_preview"]) == 500

    def test_attachment_without_filename(self, server_cfg):
        msg = MIMEMultipart()
        part = MIMEText("veri", "plain", "utf-8")
        part.add_header("Content-Disposition", "attachment")
        msg.attach(part)
        result = {"parts": [], "attachments": [], "inline_images": []}
        MailReceiver(server_cfg)._walk_parts(msg, result)
        assert result["attachments"][0]["filename"] == ""


# ═══════════════════════════════════════════════════════════════════
#  S/MIME imzalama
# ═══════════════════════════════════════════════════════════════════

def _openssl_available() -> bool:
    try:
        return subprocess.run(["openssl", "version"], capture_output=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.fixture
def fake_pyopenssl(monkeypatch):
    """pyOpenSSL opsiyonel bağımlılık — kurulu değilse imzalama dalı hiç
    çalışmaz. Testte içe aktarmayı taklit ederek dalı ölçülebilir kılıyoruz."""
    if "OpenSSL" not in sys.modules:
        mod = types.ModuleType("OpenSSL")
        mod.crypto = types.SimpleNamespace()
        monkeypatch.setitem(sys.modules, "OpenSSL", mod)
    return True


@pytest.fixture
def self_signed_cert(tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=mail-otomasyon-test"],
        capture_output=True, check=True,
    )
    return cert, key


class TestSmimeSigning:

    def test_skipped_when_pyopenssl_missing(self, server_cfg, monkeypatch):
        monkeypatch.setitem(sys.modules, "OpenSSL", None)
        result = MailSender(server_cfg).send_smime_signed(
            "to@t.com", "Konu", "Gövde", "/yok/c.pem", "/yok/k.pem")
        assert result["skipped"] is True
        assert "pyOpenSSL" in result["skip_reason"]

    @pytest.mark.skipif(not _openssl_available(), reason="openssl CLI yok")
    def test_bad_cert_returns_error_not_raises(self, server_cfg, tmp_path, fake_pyopenssl):
        bad = tmp_path / "bozuk.pem"
        bad.write_text("BU BIR SERTIFIKA DEGIL")
        result = MailSender(server_cfg).send_smime_signed(
            "to@t.com", "Konu", "Gövde", str(bad), str(bad))
        assert "error" in result
        assert result["scenario"] == "smime"

    @pytest.mark.skipif(not _openssl_available(), reason="openssl CLI yok")
    def test_signed_message_sent_with_envelope_headers(self, server_cfg, mock_smtp,
                                                       self_signed_cert, fake_pyopenssl):
        cert, key = self_signed_cert
        sender = MailSender(server_cfg)
        result = sender.send_smime_signed(
            "to@t.com", "İmzalı Konu", "Gövde ğüşıöç", str(cert), str(key))

        assert result["signed"] is True
        assert result["msg_id"]
        raw = mock_smtp.sendmail.call_args[0][2]
        # Zarf header'ları imzalı gövdeye eklenmiş olmalı — yoksa alıcı eşleştiremez
        assert b"To: to@t.com" in raw
        assert result["msg_id"].encode() in raw
        assert b"pkcs7-signature" in raw.lower() or b"signed-data" in raw.lower()

    @pytest.mark.skipif(not _openssl_available(), reason="openssl CLI yok")
    def test_temp_files_cleaned_up(self, server_cfg, mock_smtp, self_signed_cert,
                                   fake_pyopenssl, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        cert, key = self_signed_cert
        before = set(tmp_path.iterdir())
        MailSender(server_cfg).send_smime_signed(
            "to@t.com", "Konu", "Gövde", str(cert), str(key))
        leftovers = set(tmp_path.iterdir()) - before
        assert not [p for p in leftovers if p.suffix in (".eml", ".signed")]
