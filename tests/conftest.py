"""
conftest.py — Paylaşılan test fixture'ları.
Gerçek ağ bağlantısı olmadan tüm testler çalışır.
"""

import json
import sys
import email
import queue
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from email.mime.text import MIMEText
from email.utils import make_msgid, formatdate

# Proje kökünü sys.path'e ekle
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Sabitler / fixtures ──────────────────────────────────────────────

@pytest.fixture
def project_root():
    return PROJECT_ROOT


@pytest.fixture
def server_cfg():
    return {
        "smtp_host": "smtp.test.local",
        "smtp_port": 587,
        "smtp_use_tls": False,
        "imap_host": "imap.test.local",
        "imap_port": 993,
        "imap_use_ssl": True,
        "username": "user@test.local",
        "password": "secret",
        "test_address": "user@test.local",
    }


@pytest.fixture
def full_config(server_cfg, tmp_path):
    return {
        "ems": {**server_cfg, "label": "EMS"},
        "gmail": {**server_cfg, "smtp_host": "smtp.gmail.com", "label": "Gmail"},
        "outlook": {**server_cfg, "smtp_host": "smtp.office365.com", "label": "Outlook"},
        "anthropic": {"api_key": "sk-ant-test"},
        "test": {
            "wait_seconds": 0,
            "max_retries": 1,
            "retry_interval": 0,
            "subject_prefix": "[TEST]",
            "test_attachment_path": str(PROJECT_ROOT / "test_files/test_document.pdf"),
            "test_image_path": str(PROJECT_ROOT / "test_files/test_image.png"),
            "csv_input": str(PROJECT_ROOT / "mail_test_checklist.csv"),
            "report_output": str(tmp_path / "report.html"),
            "results_csv": str(tmp_path / "results.csv"),
        },
        "logging": {"level": "WARNING", "file": str(tmp_path / "test.log")},
    }


# ── SMTP mock ────────────────────────────────────────────────────────

@pytest.fixture
def mock_smtp():
    """smtplib.SMTP'yi patch'ler — gerçek bağlantı olmaz."""
    smtp_inst = MagicMock()
    smtp_inst.__enter__ = MagicMock(return_value=smtp_inst)
    smtp_inst.__exit__ = MagicMock(return_value=False)
    smtp_inst.sendmail = MagicMock(return_value={})
    with patch("smtplib.SMTP", return_value=smtp_inst):
        yield smtp_inst


# ── IMAP mock'ları ───────────────────────────────────────────────────

def _build_raw_email(subject="[TEST] Konu", body="Merhaba ğüşıöç", msg_id=None):
    """Minimal RFC 2822 mesajı bytes olarak üretir."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = "sender@test.local"
    msg["To"] = "receiver@test.local"
    msg["Date"] = formatdate(localtime=True)
    final_id = msg_id or make_msgid()
    msg["Message-ID"] = final_id
    return msg.as_bytes(), final_id


@pytest.fixture
def mock_imap_empty():
    """Boş gelen kutusu döndüren IMAP mock'u."""
    inst = MagicMock()
    inst.login.return_value = ("OK", [b"Logged in"])
    inst.select.return_value = ("OK", [b"1"])
    inst.search.return_value = ("OK", [b""])
    inst.logout.return_value = ("BYE", [b""])
    with patch("imaplib.IMAP4_SSL", return_value=inst):
        yield inst


@pytest.fixture
def mock_imap_with_message():
    """Eşleşen bir mesaj döndüren IMAP mock'u."""
    raw, msg_id = _build_raw_email()
    inst = MagicMock()
    inst.login.return_value = ("OK", [b"Logged in"])
    inst.select.return_value = ("OK", [b"1"])
    inst.search.return_value = ("OK", [b"1"])
    inst.fetch.return_value = ("OK", [(b"1 (RFC822 {500})", raw)])
    inst.logout.return_value = ("BYE", [b""])
    with patch("imaplib.IMAP4_SSL", return_value=inst):
        yield inst, msg_id


# ── Claude API mock'ları ─────────────────────────────────────────────

_PASS_RESULT = {
    "passed": True,
    "confidence": "HIGH",
    "checks": [
        {"name": "Mesaj Alımı", "passed": True, "detail": "Başarılı"},
        {"name": "Encoding", "passed": True, "detail": "UTF-8 doğru"},
    ],
    "summary": "Tüm kontroller başarılı.",
    "issues": [],
    "recommendations": [],
}


@pytest.fixture
def mock_claude_pass():
    """Başarılı analiz döndüren Claude API mock'u."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"content": [{"text": json.dumps(_PASS_RESULT)}]}
    with patch("requests.post", return_value=resp) as mock_post:
        yield mock_post


@pytest.fixture
def mock_claude_fail():
    """Başarısız analiz döndüren Claude API mock'u."""
    result = {**_PASS_RESULT, "passed": False, "summary": "İletim başarısız.",
              "checks": [{"name": "Mesaj Alımı", "passed": False, "detail": "Timeout"}],
              "issues": ["Mesaj alınamadı"]}
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"content": [{"text": json.dumps(result)}]}
    with patch("requests.post", return_value=resp) as mock_post:
        yield mock_post


# ── Alınan mesaj örneği ──────────────────────────────────────────────

@pytest.fixture
def received_msg():
    return {
        "imap_id": "1",
        "raw_size": 512,
        "raw_bytes": b"",
        "headers": {
            "message_id": "<abc@test.local>",
            "from": "sender@test.local",
            "to": "receiver@test.local",
            "subject": "[TEST] Plain Text testi",
            "date": "Mon, 27 Apr 2026 10:00:00 +0000",
            "content_type": "text/plain; charset=utf-8",
            "in_reply_to": "",
            "references": "",
            "mime_version": "1.0",
            "x_mailer": "",
            "content_transfer_encoding": "quoted-printable",
        },
        "parts": [
            {
                "content_type": "text/plain",
                "charset": "utf-8",
                "content_id": "",
                "disposition": "",
                "transfer_encoding": "qp",
                "text_preview": "Merhaba ğüşıöç",
            }
        ],
        "attachments": [],
        "inline_images": [],
        "is_multipart": False,
    }


@pytest.fixture
def combination_meta():
    return {
        "sender_server": "Gmail",
        "sender_client": "Android",
        "receiver_server": "EMS",
        "receiver_client": "iOS",
    }


# ── Flask test istemcisi ─────────────────────────────────────────────

@pytest.fixture
def flask_client(tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.yaml")
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    monkeypatch.setattr(app_module, "REPORTS_DIR", reports)
    app_module.app.config["TESTING"] = True
    app_module.run_state.update({
        "running": False,
        "process": None,
        "log_queue": queue.Queue(),
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
    })
    with app_module.app.test_client() as client:
        yield client
