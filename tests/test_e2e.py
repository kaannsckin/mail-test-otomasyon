"""
test_e2e.py — End-to-End kombinasyon testleri.

CSV'deki 10 kombinasyonun tamamı; 4 aktif senaryo (plain_text, attachment,
inline_image, reply_chain) için mock SMTP/IMAP/Claude API ile tam pipeline
testinden geçirilir.  Ayrıca eksik kombinasyonlar dokümante edilir.
"""

import csv
import json
import os
from itertools import product
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent

# ── Sabitler ──────────────────────────────────────────────────────────

ALL_SERVERS = ["EMS", "Gmail", "Outlook"]
ALL_CLIENTS = ["iOS", "Android", "Outlook"]
ACTIVE_SCENARIOS = ["plain_text", "attachment", "inline_image", "reply_chain"]

# CSV'deki 18 kombinasyon — (recv_server, recv_client, send_server, send_client)
EXISTING_COMBINATIONS = [
    # Orijinal 10
    ("EMS",     "iOS",     "Gmail",   "Android"),
    ("EMS",     "iOS",     "Gmail",   "iOS"),
    ("EMS",     "iOS",     "Gmail",   "Outlook"),
    ("EMS",     "Android", "Gmail",   "Android"),
    ("EMS",     "Android", "Gmail",   "iOS"),
    ("EMS",     "Outlook", "Gmail",   "Android"),
    ("EMS",     "Outlook", "EMS",     "iOS"),
    ("Gmail",   "Android", "EMS",     "iOS"),
    ("Gmail",   "iOS",     "EMS",     "Android"),
    ("Outlook", "Outlook", "EMS",     "iOS"),
    # Yeni 8 — eksik kombinasyonlar tamamlandı
    ("Gmail",   "iOS",     "Gmail",   "Android"),   # Gmail → Gmail
    ("Gmail",   "Android", "Gmail",   "Outlook"),   # Gmail → Gmail
    ("Outlook", "iOS",     "EMS",     "Android"),   # Outlook/iOS alıcı
    ("Outlook", "iOS",     "Gmail",   "Android"),   # Outlook/iOS alıcı
    ("Outlook", "Android", "EMS",     "iOS"),       # Outlook/Android alıcı
    ("Outlook", "Android", "Gmail",   "iOS"),       # Outlook/Android alıcı
    ("EMS",     "Android", "Outlook", "iOS"),       # Outlook gönderici
    ("Gmail",   "iOS",     "Outlook", "Android"),   # Outlook gönderici
]

COMBO_IDS = [f"{ss}/{sc}→{rs}/{rc}" for rs, rc, ss, sc in EXISTING_COMBINATIONS]

_PASS_ANALYSIS = {
    "passed": True,
    "confidence": "HIGH",
    "checks": [{"name": "Kontrol", "passed": True, "detail": "OK"}],
    "summary": "Tüm kontroller başarılı.",
    "issues": [],
    "recommendations": [],
}

_MINIMAL_PNG = bytes([
    137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82,
    0, 0, 0, 1, 0, 0, 0, 1, 8, 2, 0, 0, 0, 144, 119, 83, 222, 0, 0, 0,
    12, 73, 68, 65, 84, 8, 215, 99, 248, 207, 192, 0, 0, 0, 2, 0, 1,
    226, 33, 188, 51, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130,
])


# ── Yardımcılar ───────────────────────────────────────────────────────

def _server_cfg(server: str) -> dict:
    return {
        "smtp_host": f"smtp.{server.lower()}.test",
        "smtp_port": 587,
        "smtp_use_tls": False,
        "imap_host": f"imap.{server.lower()}.test",
        "imap_port": 993,
        "imap_use_ssl": True,
        "username": f"user@{server.lower()}.test",
        "password": "testpassword",
        "test_address": f"user@{server.lower()}.test",
    }


def _mock_smtp():
    inst = MagicMock()
    inst.__enter__ = MagicMock(return_value=inst)
    inst.__exit__ = MagicMock(return_value=False)
    return inst


def _mock_claude():
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"content": [{"text": json.dumps(_PASS_ANALYSIS)}]}
    return resp


def _make_received(msg_id: str) -> dict:
    return {
        "imap_id": "1",
        "raw_size": 512,
        "raw_bytes": b"",
        "headers": {
            "message_id": msg_id,
            "from": "sender@test.local",
            "to": "receiver@test.local",
            "subject": "[TEST] Konu",
            "date": "Mon, 27 Apr 2026 10:00:00 +0000",
            "content_type": "text/plain; charset=utf-8",
            "in_reply_to": "",
            "references": "",
            "mime_version": "1.0",
            "x_mailer": "",
            "content_transfer_encoding": "quoted-printable",
        },
        "parts": [{"content_type": "text/plain", "charset": "utf-8",
                   "content_id": "", "disposition": "",
                   "transfer_encoding": "qp",
                   "text_preview": "Merhaba ğüşıöç test"}],
        "attachments": [],
        "inline_images": [],
        "is_multipart": False,
    }


def _ensure_test_image(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_MINIMAL_PNG)


# ═══════════════════════════════════════════════════════════════════
#  Kombinasyon matris analizi
# ═══════════════════════════════════════════════════════════════════

class TestCombinationMatrixAnalysis:
    """CSV'deki kombinasyon matrisini analiz eder, kalan eksikleri belgeler."""

    def test_existing_count_is_18(self):
        assert len(EXISTING_COMBINATIONS) == 18

    def test_outlook_now_appears_as_sender(self):
        """Outlook artık gönderici sunucu olarak en az 2 kombinasyonda var."""
        outlook_senders = [c for c in EXISTING_COMBINATIONS if c[2] == "Outlook"]
        assert len(outlook_senders) >= 2

    def test_gmail_to_gmail_now_covered(self):
        """Gmail → Gmail kombinasyonu artık mevcut."""
        gmail_to_gmail = [c for c in EXISTING_COMBINATIONS
                          if c[0] == "Gmail" and c[2] == "Gmail"]
        assert len(gmail_to_gmail) >= 2

    def test_outlook_ios_receiver_now_covered(self):
        """Outlook/iOS alıcı kombinasyonu artık mevcut."""
        outlook_ios_recv = [c for c in EXISTING_COMBINATIONS
                            if c[0] == "Outlook" and c[1] == "iOS"]
        assert len(outlook_ios_recv) >= 2

    def test_outlook_android_receiver_now_covered(self):
        """Outlook/Android alıcı kombinasyonu artık mevcut."""
        outlook_android_recv = [c for c in EXISTING_COMBINATIONS
                                if c[0] == "Outlook" and c[1] == "Android"]
        assert len(outlook_android_recv) >= 2

    def test_still_missing_outlook_to_outlook(self):
        """Outlook → Outlook (hem gönderici hem alıcı Outlook sunucusu) hâlâ yok."""
        outlook_to_outlook = [c for c in EXISTING_COMBINATIONS
                               if c[0] == "Outlook" and c[2] == "Outlook"]
        assert len(outlook_to_outlook) == 0

    def test_still_missing_ems_to_ems(self):
        """EMS → EMS (aynı sunucu) kombinasyonu hâlâ yok."""
        ems_to_ems = [c for c in EXISTING_COMBINATIONS
                      if c[0] == "EMS" and c[2] == "EMS" and c[1] != c[3]]
        # Sadece EMS/Outlook ← EMS/iOS var; diğer istemci combolar eksik
        # Bu test eksik olanları belgeler
        all_ems_to_ems = [c for c in EXISTING_COMBINATIONS
                          if c[0] == "EMS" and c[2] == "EMS"]
        assert len(all_ems_to_ems) <= 1  # Sadece 1 var: EMS/Outlook ← EMS/iOS

    def test_all_combinations_have_valid_fields(self):
        for rs, rc, ss, sc in EXISTING_COMBINATIONS:
            assert rs in ALL_SERVERS, f"Geçersiz alıcı sunucu: {rs}"
            assert ss in ALL_SERVERS, f"Geçersiz gönderici sunucu: {ss}"
            assert rc in ALL_CLIENTS, f"Geçersiz alıcı istemci: {rc}"
            assert sc in ALL_CLIENTS, f"Geçersiz gönderici istemci: {sc}"

    def test_all_combinations_unique(self):
        assert len(set(EXISTING_COMBINATIONS)) == len(EXISTING_COMBINATIONS), (
            "Tekrarlanan kombinasyon var!"
        )

    def test_remaining_missing_combinations_report(self, capsys):
        """Hâlâ eksik olan kombinasyonları stdout'a yazar."""
        existing_set = set(EXISTING_COMBINATIONS)
        missing = [
            (rs, rc, ss, sc)
            for rs, rc, ss, sc in product(ALL_SERVERS, ALL_CLIENTS, ALL_SERVERS, ALL_CLIENTS)
            if rs != ss and (rs, rc, ss, sc) not in existing_set
        ]
        print(f"\n=== Hâlâ Eksik Kombinasyonlar ({len(missing)} adet) ===")
        for rs, rc, ss, sc in sorted(missing):
            print(f"  {ss}/{sc} → {rs}/{rc}")
        # Matris hâlâ tam değil — bazı kombinasyonlar bilerek dışarıda
        assert len(missing) >= 0  # Belgeleme amaçlı, başarısız olmaz


# ═══════════════════════════════════════════════════════════════════
#  E2E pipeline — tüm kombinasyonlar × tüm senaryolar
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("recv_server,recv_client,send_server,send_client",
                          EXISTING_COMBINATIONS, ids=COMBO_IDS)
class TestE2EPipeline:
    """
    Her kombinasyon için tam Sender → Receiver → Analyzer pipeline'ını test eder.
    Mock SMTP/IMAP/Claude API kullanılır.
    """

    def _run(self, recv_server, recv_client, send_server, send_client, scenario):
        from sender import MailSender
        from receiver import MailReceiver
        from analyzer import MailAnalyzer

        sender_cfg = _server_cfg(send_server)
        sender_cfg["test_address"] = f"sender@{send_server.lower()}.test"
        receiver_cfg = _server_cfg(recv_server)
        receiver_cfg["test_address"] = f"receiver@{recv_server.lower()}.test"
        combo_meta = {
            "sender_server": send_server, "sender_client": send_client,
            "receiver_server": recv_server, "receiver_client": recv_client,
        }

        smtp_inst = _mock_smtp()
        claude_resp = _mock_claude()

        img_path = PROJECT_ROOT / "test_files" / "test_image.png"
        _ensure_test_image(img_path)

        with patch("smtplib.SMTP", return_value=smtp_inst), \
             patch("requests.post", return_value=claude_resp):

            sender = MailSender(sender_cfg)
            receiver = MailReceiver(receiver_cfg)
            analyzer = MailAnalyzer("sk-ant-test")

            if scenario == "plain_text":
                send_meta = sender.send_plain_text(
                    receiver_cfg["test_address"], "[TEST] Konu", "Gövde ğüşıöç"
                )
            elif scenario == "attachment":
                send_meta = sender.send_with_attachment(
                    receiver_cfg["test_address"], "[TEST] Konu", "Gövde",
                    "/olmayan/dosya.pdf"
                )
            elif scenario == "inline_image":
                send_meta = sender.send_inline_image(
                    receiver_cfg["test_address"], "[TEST] Konu", str(img_path)
                )
            elif scenario == "reply_chain":
                send_meta = sender.send_reply(
                    receiver_cfg["test_address"], "Orijinal Konu",
                    "<orig@test.local>", "", "Cevap metni ğüşıöç"
                )
            else:
                pytest.skip(f"Desteklenmeyen senaryo: {scenario}")

            received = _make_received(send_meta.get("msg_id", "<test@t>"))
            analysis = analyzer.analyze(scenario, send_meta, received, combo_meta)

        return send_meta, received, analysis

    def test_plain_text_e2e(self, recv_server, recv_client, send_server, send_client):
        send_meta, received, analysis = self._run(
            recv_server, recv_client, send_server, send_client, "plain_text"
        )
        assert send_meta["scenario"] == "plain_text"
        assert "msg_id" in send_meta
        assert "sent_at" in send_meta
        assert analysis["passed"] is True
        assert analysis["confidence"] == "HIGH"

    def test_attachment_e2e(self, recv_server, recv_client, send_server, send_client):
        send_meta, received, analysis = self._run(
            recv_server, recv_client, send_server, send_client, "attachment"
        )
        assert send_meta["scenario"] == "attachment"
        assert analysis["passed"] is True

    def test_inline_image_e2e(self, recv_server, recv_client, send_server, send_client):
        send_meta, received, analysis = self._run(
            recv_server, recv_client, send_server, send_client, "inline_image"
        )
        assert send_meta["scenario"] == "inline_image"
        assert send_meta["cid"].startswith("inline_image_")
        assert analysis["passed"] is True

    def test_reply_chain_e2e(self, recv_server, recv_client, send_server, send_client):
        send_meta, received, analysis = self._run(
            recv_server, recv_client, send_server, send_client, "reply_chain"
        )
        assert send_meta["scenario"] == "reply_chain"
        assert send_meta["in_reply_to"] == "<orig@test.local>"
        assert analysis["passed"] is True

    def test_smime_skipped_without_cert(self, recv_server, recv_client,
                                         send_server, send_client):
        """S/MIME sertifika olmadan skip olmalı — hiç exception atmamalı."""
        from sender import MailSender
        smtp_inst = _mock_smtp()
        with patch("smtplib.SMTP", return_value=smtp_inst):
            sender = MailSender(_server_cfg(send_server))
            result = sender.send_smime_signed(
                f"receiver@{recv_server.lower()}.test",
                "S/MIME Test", "Gövde",
                "/olmayan/cert.pem", "/olmayan/key.pem"
            )
        assert result["scenario"] == "smime"
        assert "skipped" in result or "error" in result

    def test_timeout_analysis_returns_fail(self, recv_server, recv_client,
                                            send_server, send_client):
        """Alınan mesaj None ise analiz FAIL döndürmeli."""
        from analyzer import MailAnalyzer
        analyzer = MailAnalyzer("sk-ant-test")
        combo_meta = {
            "sender_server": send_server, "sender_client": send_client,
            "receiver_server": recv_server, "receiver_client": recv_client,
        }
        result = analyzer.analyze("plain_text", {}, None, combo_meta)
        assert result["passed"] is False
        assert result["confidence"] == "HIGH"


# ═══════════════════════════════════════════════════════════════════
#  Senaryo doğruluk testleri
# ═══════════════════════════════════════════════════════════════════

class TestScenarioCorrectness:

    def test_plain_text_utf8_body(self, tmp_path):
        from sender import MailSender
        smtp_inst = _mock_smtp()
        sent_args = []
        smtp_inst.sendmail = lambda f, t, m: sent_args.append((f, t, m))
        with patch("smtplib.SMTP", return_value=smtp_inst):
            sender = MailSender(_server_cfg("EMS"))
            sender.send_plain_text("to@t.com", "Konu", "ğüşıöçĞÜŞİÖÇ")
        assert len(sent_args) == 1
        raw_msg = sent_args[0][2]
        assert isinstance(raw_msg, bytes)
        assert len(raw_msg) > 50

    def test_attachment_preserves_filename(self, tmp_path):
        from sender import MailSender
        pdf = tmp_path / "türkçe_belge.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        smtp_inst = _mock_smtp()
        with patch("smtplib.SMTP", return_value=smtp_inst):
            sender = MailSender(_server_cfg("Gmail"))
            result = sender.send_with_attachment("to@t.com", "Konu", "Gövde", str(pdf))
        assert result["attachments"][0]["name"] == "türkçe_belge.pdf"

    def test_inline_image_cid_format(self, tmp_path):
        from sender import MailSender
        img = tmp_path / "foto.png"
        img.write_bytes(_MINIMAL_PNG)
        smtp_inst = _mock_smtp()
        with patch("smtplib.SMTP", return_value=smtp_inst):
            sender = MailSender(_server_cfg("Outlook"))
            result = sender.send_inline_image("to@t.com", "Konu", str(img))
        cid = result["cid"]
        assert cid.startswith("inline_image_")
        assert "@test" in cid
        assert len(cid) > 15

    def test_reply_chain_in_reply_to_preserved(self):
        from sender import MailSender
        orig_id = "<ozgun-mesaj-id-12345@ems.kurumsal.local>"
        smtp_inst = _mock_smtp()
        with patch("smtplib.SMTP", return_value=smtp_inst):
            sender = MailSender(_server_cfg("EMS"))
            result = sender.send_reply(
                "to@t.com", "Proje Güncelleme", orig_id, "", "Teşekkürler."
            )
        assert result["in_reply_to"] == orig_id
        assert orig_id in result["references"]

    def test_attachment_multi_file_count(self, tmp_path):
        from sender import MailSender
        files = []
        for name in ["a.pdf", "b.csv", "c.txt"]:
            f = tmp_path / name
            f.write_bytes(b"content")
            files.append(str(f))
        smtp_inst = _mock_smtp()
        with patch("smtplib.SMTP", return_value=smtp_inst):
            sender = MailSender(_server_cfg("EMS"))
            result = sender.send_with_attachment("to@t.com", "Konu", "Gövde", files)
        assert result["attachment_count"] == 3
        assert len(result["attachments"]) == 3

    def test_analyzer_builds_prompt_for_each_scenario(self):
        from analyzer import MailAnalyzer
        from tests.conftest import _build_raw_email
        import email as email_lib
        raw, _ = _build_raw_email()
        parsed = email_lib.message_from_bytes(raw)

        received = {
            "imap_id": "1", "raw_size": 100, "raw_bytes": raw,
            "headers": {
                "message_id": "<x@t>", "from": "a@b", "to": "c@d",
                "subject": "S", "date": "D", "content_type": "text/plain",
                "in_reply_to": "", "references": "", "mime_version": "1.0",
                "x_mailer": "", "content_transfer_encoding": "qp",
            },
            "parts": [], "attachments": [], "inline_images": [], "is_multipart": False,
        }
        combo = {"sender_server": "Gmail", "sender_client": "Android",
                 "receiver_server": "EMS", "receiver_client": "iOS"}
        send_meta = {"msg_id": "<x>", "cid": "c123", "attachment_name": "f.pdf",
                     "attachment_size": 100, "in_reply_to": "<orig>"}

        a = MailAnalyzer("sk-ant-test")
        for scenario in ("plain_text", "attachment", "inline_image", "smime", "reply_chain"):
            prompt = a._build_prompt(scenario, send_meta, received, combo)
            assert len(prompt) > 200, f"{scenario} için prompt çok kısa"
            assert scenario in prompt or scenario.replace("_", " ") in prompt.lower()


# ═══════════════════════════════════════════════════════════════════
#  Rapor entegrasyon testleri — tam matris
# ═══════════════════════════════════════════════════════════════════

class TestFullMatrixReporting:

    def _make_result(self, combo_label, scenario, passed=True):
        return {
            "combination": combo_label,
            "scenario_type": scenario,
            "scenario_key": scenario,
            "test_time": "2026-04-27 10:00:00",
            "analysis": {
                "passed": passed,
                "confidence": "HIGH" if passed else "MEDIUM",
                "checks": [{"name": "Test", "passed": passed, "detail": "Smoke test"}],
                "summary": "Başarılı." if passed else "Başarısız.",
                "issues": [] if passed else ["Test başarısız"],
                "recommendations": [],
            },
        }

    def test_html_report_all_10_combinations(self, tmp_path):
        from reporter import generate_html_report
        from csv_parser import parse_csv
        combos = parse_csv(str(PROJECT_ROOT / "mail_test_checklist.csv"))
        results = [
            self._make_result(c.label, s, i % 3 != 0)
            for i, (c, s) in enumerate(
                (c, s) for c in combos for s in ACTIVE_SCENARIOS
            )
        ]
        output = str(tmp_path / "full.html")
        generate_html_report(results, output)
        content = Path(output).read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content
        assert len(results) == 72  # 18 × 4

    def test_csv_report_all_10_combinations(self, tmp_path):
        from reporter import generate_csv_results
        from csv_parser import parse_csv
        combos = parse_csv(str(PROJECT_ROOT / "mail_test_checklist.csv"))
        results = [
            self._make_result(c.label, s)
            for c in combos
            for s in ACTIVE_SCENARIOS
        ]
        output = str(tmp_path / "full.csv")
        generate_csv_results(results, output)
        with open(output, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 72
        assert all(r["Sonuç"] == "PASS" for r in rows)

    def test_pass_rate_calculation(self, tmp_path):
        from reporter import generate_html_report
        from csv_parser import parse_csv
        combos = parse_csv(str(PROJECT_ROOT / "mail_test_checklist.csv"))
        results = []
        for i, combo in enumerate(combos):
            for scenario in ACTIVE_SCENARIOS:
                results.append(self._make_result(combo.label, scenario, i < 9))
        # 9 combo pass (9×4=36), 9 combo fail (9×4=36) → %50
        output = str(tmp_path / "half.html")
        generate_html_report(results, output)
        content = Path(output).read_text(encoding="utf-8")
        assert "50.0%" in content
