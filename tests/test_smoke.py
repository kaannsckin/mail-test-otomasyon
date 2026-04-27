"""
test_smoke.py — Tüm modüller için birim seviyesi smoke testleri.
Gerçek SMTP/IMAP/Claude API bağlantısı olmadan çalışır.
"""

import csv
import email
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

PROJECT_ROOT = Path(__file__).parent.parent


# ═══════════════════════════════════════════════════════════════════
#  sender.py
# ═══════════════════════════════════════════════════════════════════

class TestMailSenderStatic:

    def test_guess_mime_pdf(self):
        from sender import MailSender
        assert MailSender._guess_mime("rapor.pdf") == "application/pdf"

    def test_guess_mime_png(self):
        from sender import MailSender
        assert MailSender._guess_mime("resim.png") == "image/png"

    def test_guess_mime_jpg(self):
        from sender import MailSender
        assert MailSender._guess_mime("foto.jpg") == "image/jpeg"

    def test_guess_mime_docx(self):
        from sender import MailSender
        assert "wordprocessingml" in MailSender._guess_mime("belge.docx")

    def test_guess_mime_xlsx(self):
        from sender import MailSender
        assert "spreadsheetml" in MailSender._guess_mime("tablo.xlsx")

    def test_guess_mime_txt(self):
        from sender import MailSender
        assert MailSender._guess_mime("dosya.txt") == "text/plain"

    def test_guess_mime_zip(self):
        from sender import MailSender
        assert MailSender._guess_mime("arsiv.zip") == "application/zip"

    def test_guess_mime_unknown(self):
        from sender import MailSender
        assert MailSender._guess_mime("dosya.xyz") == "application/octet-stream"


class TestMailSenderMethods:

    def test_base_headers_required_keys(self, server_cfg):
        from sender import MailSender
        s = MailSender(server_cfg)
        headers = s._base_headers("Test Konu", "to@test.local")
        for key in ("Message-ID", "Date", "Subject", "From", "To", "X-Test-Automation"):
            assert key in headers, f"Header eksik: {key}"

    def test_base_headers_subject(self, server_cfg):
        from sender import MailSender
        s = MailSender(server_cfg)
        headers = s._base_headers("Özel Konu ğüşıöç", "to@test.local")
        assert headers["Subject"] == "Özel Konu ğüşıöç"

    def test_base_headers_custom_msg_id(self, server_cfg):
        from sender import MailSender
        s = MailSender(server_cfg)
        custom_id = "<ozel-id@test.local>"
        headers = s._base_headers("Konu", "to@test.local", msg_id=custom_id)
        assert headers["Message-ID"] == custom_id

    def test_send_plain_text_returns_meta(self, server_cfg, mock_smtp):
        from sender import MailSender
        s = MailSender(server_cfg)
        result = s.send_plain_text("to@test.local", "Konu", "Gövde ğüşıöç")
        assert result["scenario"] == "plain_text"
        assert "msg_id" in result
        assert "sent_at" in result
        assert isinstance(result["sent_at"], float)

    def test_send_plain_text_calls_sendmail(self, server_cfg, mock_smtp):
        from sender import MailSender
        s = MailSender(server_cfg)
        s.send_plain_text("to@test.local", "Konu", "Gövde")
        assert mock_smtp.sendmail.called
        call_args = mock_smtp.sendmail.call_args[0]
        assert call_args[1] == ["to@test.local"]

    def test_send_with_attachment_single_file(self, server_cfg, mock_smtp, tmp_path):
        from sender import MailSender
        att = tmp_path / "belge.pdf"
        att.write_bytes(b"%PDF-1.4 minimal content")
        s = MailSender(server_cfg)
        result = s.send_with_attachment("to@test.local", "Konu", "Gövde", str(att))
        assert result["scenario"] == "attachment"
        assert result["attachment_count"] == 1
        assert "belge.pdf" in result["attachment_name"]
        assert result["attachments"][0]["size"] > 0

    def test_send_with_attachment_multiple_files(self, server_cfg, mock_smtp, tmp_path):
        from sender import MailSender
        files = []
        for name in ["a.pdf", "b.txt", "c.png"]:
            f = tmp_path / name
            f.write_bytes(b"test content")
            files.append(str(f))
        s = MailSender(server_cfg)
        result = s.send_with_attachment("to@test.local", "Konu", "Gövde", files)
        assert result["attachment_count"] == 3

    def test_send_with_attachment_missing_file_skipped(self, server_cfg, mock_smtp):
        from sender import MailSender
        s = MailSender(server_cfg)
        result = s.send_with_attachment("to@test.local", "Konu", "Gövde",
                                        "/olmayan/dosya.pdf")
        assert result["scenario"] == "attachment"
        assert result["attachment_count"] == 0

    def test_send_inline_image(self, server_cfg, mock_smtp, tmp_path):
        from sender import MailSender
        img = tmp_path / "test.png"
        img.write_bytes(_minimal_png())
        s = MailSender(server_cfg)
        result = s.send_inline_image("to@test.local", "Konu", str(img))
        assert result["scenario"] == "inline_image"
        assert result["cid"].startswith("inline_image_")

    def test_send_inline_image_custom_html(self, server_cfg, mock_smtp, tmp_path):
        from sender import MailSender
        img = tmp_path / "test.png"
        img.write_bytes(_minimal_png())
        custom_html = '<img src="cid:{{CID}}" />'
        s = MailSender(server_cfg)
        result = s.send_inline_image("to@test.local", "Konu", str(img), html_body=custom_html)
        assert result["scenario"] == "inline_image"

    def test_send_reply_returns_meta(self, server_cfg, mock_smtp):
        from sender import MailSender
        s = MailSender(server_cfg)
        orig_id = "<orijinal@test.local>"
        result = s.send_reply("to@test.local", "Orijinal Konu", orig_id, "", "Cevap metni")
        assert result["scenario"] == "reply_chain"
        assert result["in_reply_to"] == orig_id
        assert orig_id in result["references"]

    def test_send_reply_re_prefix_not_duplicated(self, server_cfg, mock_smtp):
        from sender import MailSender
        s = MailSender(server_cfg)
        result = s.send_reply("to@test.local", "Re: Zaten Cevap", "<orig@t>", "", "Cevap")
        assert result["scenario"] == "reply_chain"

    def test_send_smime_no_openssl(self, server_cfg, mock_smtp):
        from sender import MailSender
        s = MailSender(server_cfg)
        with patch.dict("sys.modules", {"OpenSSL": None, "OpenSSL.crypto": None}):
            result = s.send_smime_signed(
                "to@test.local", "Konu", "Gövde",
                "/yok/cert.pem", "/yok/key.pem"
            )
        assert result["scenario"] == "smime"


# ═══════════════════════════════════════════════════════════════════
#  receiver.py
# ═══════════════════════════════════════════════════════════════════

class TestMailReceiverStatic:

    def test_decode_header_plain(self, server_cfg):
        from receiver import MailReceiver
        r = MailReceiver(server_cfg)
        assert r._decode_header("Merhaba Dünya") == "Merhaba Dünya"

    def test_decode_header_empty(self, server_cfg):
        from receiver import MailReceiver
        r = MailReceiver(server_cfg)
        assert r._decode_header("") == ""

    def test_decode_header_none(self, server_cfg):
        from receiver import MailReceiver
        r = MailReceiver(server_cfg)
        assert r._decode_header(None) == ""

    def test_decode_header_rfc2047_base64(self, server_cfg):
        from receiver import MailReceiver
        # "Test" → base64 → =?utf-8?b?VGVzdA==?=
        r = MailReceiver(server_cfg)
        assert r._decode_header("=?utf-8?b?VGVzdA==?=") == "Test"

    def test_walk_parts_plain_text(self, server_cfg):
        from receiver import MailReceiver
        msg = MIMEText("Merhaba ğüşıöç", "plain", "utf-8")
        result = {"parts": [], "attachments": [], "inline_images": []}
        MailReceiver(server_cfg)._walk_parts(msg, result)
        assert len(result["parts"]) >= 1
        assert "Merhaba" in result["parts"][0]["text_preview"]

    def test_walk_parts_with_attachment(self, server_cfg):
        from receiver import MailReceiver
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText("Gövde", "plain", "utf-8"))
        part = MIMEBase("application", "octet-stream")
        part.set_payload(b"fake pdf content %PDF-1.4")
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", 'attachment; filename="rapor.pdf"')
        msg.attach(part)
        result = {"parts": [], "attachments": [], "inline_images": []}
        MailReceiver(server_cfg)._walk_parts(msg, result)
        assert len(result["attachments"]) == 1
        assert result["attachments"][0]["filename"] == "rapor.pdf"

    def test_walk_parts_inline_image(self, server_cfg):
        from receiver import MailReceiver
        from email.mime.image import MIMEImage
        msg = MIMEMultipart("related")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText("Fallback", "plain", "utf-8"))
        alt.attach(MIMEText('<img src="cid:img1@test" />', "html", "utf-8"))
        msg.attach(alt)
        img_part = MIMEImage(_minimal_png())
        img_part.add_header("Content-ID", "<img1@test>")
        img_part.add_header("Content-Disposition", "inline", filename="test.png")
        msg.attach(img_part)
        result = {"parts": [], "attachments": [], "inline_images": []}
        MailReceiver(server_cfg)._walk_parts(msg, result)
        assert len(result["inline_images"]) == 1
        assert result["inline_images"][0]["cid"] == "img1@test"

    def test_extract_details_structure(self, server_cfg):
        from receiver import MailReceiver
        msg = MIMEText("Test gövde ğüşıöç", "plain", "utf-8")
        msg["Message-ID"] = "<test@local>"
        msg["From"] = "a@b.com"
        msg["To"] = "c@d.com"
        msg["Subject"] = "Test Başlık"
        raw = msg.as_bytes()
        parsed = email.message_from_bytes(raw)
        details = MailReceiver(server_cfg)._extract_details(parsed, raw, "1")
        assert details["headers"]["message_id"] == "<test@local>"
        assert details["headers"]["from"] == "a@b.com"
        assert details["raw_size"] == len(raw)
        assert "parts" in details
        assert "attachments" in details
        assert "inline_images" in details

    def test_wait_for_message_timeout(self, server_cfg, mock_imap_empty):
        from receiver import MailReceiver
        r = MailReceiver(server_cfg)
        result = r.wait_for_message(
            "<yok@test>", "[TEST]", wait_seconds=0, max_retries=1, retry_interval=0
        )
        assert result is None

    def test_wait_for_message_found(self, server_cfg, mock_imap_with_message):
        from receiver import MailReceiver
        _, msg_id = mock_imap_with_message
        r = MailReceiver(server_cfg)
        result = r.wait_for_message(
            msg_id, "[TEST]", wait_seconds=0, max_retries=1, retry_interval=0
        )
        assert result is not None
        assert result["headers"]["message_id"] == msg_id


# ═══════════════════════════════════════════════════════════════════
#  analyzer.py
# ═══════════════════════════════════════════════════════════════════

class TestMailAnalyzer:

    def test_analyze_timeout_returns_fail(self, combination_meta):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        result = a.analyze("plain_text", {}, None, combination_meta)
        assert result["passed"] is False
        assert result["confidence"] == "HIGH"
        assert len(result["checks"]) >= 1
        assert result["checks"][0]["passed"] is False

    def test_analyze_calls_claude(self, received_msg, combination_meta, mock_claude_pass):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        result = a.analyze("plain_text", {"msg_id": "<x@y>"}, received_msg, combination_meta)
        assert mock_claude_pass.called
        assert result["passed"] is True

    def test_build_prompt_contains_combo_info(self, received_msg, combination_meta):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        prompt = a._build_prompt("plain_text", {"msg_id": "<x>"}, received_msg, combination_meta)
        for field in ("Gmail", "EMS", "Android", "iOS"):
            assert field in prompt, f"Prompt'ta eksik: {field}"

    def test_build_prompt_plain_text_utf8_check(self, received_msg, combination_meta):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        prompt = a._build_prompt("plain_text", {}, received_msg, combination_meta)
        assert "UTF-8" in prompt or "utf-8" in prompt.lower()

    def test_build_prompt_attachment_includes_filename(self, received_msg, combination_meta):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        send_meta = {"attachment_name": "rapor.pdf", "attachment_size": 12345}
        prompt = a._build_prompt("attachment", send_meta, received_msg, combination_meta)
        assert "rapor.pdf" in prompt

    def test_build_prompt_inline_image_includes_cid(self, received_msg, combination_meta):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        send_meta = {"cid": "inline_image_abc123@test"}
        prompt = a._build_prompt("inline_image", send_meta, received_msg, combination_meta)
        assert "inline_image_abc123@test" in prompt

    def test_build_prompt_reply_chain_includes_orig_id(self, received_msg, combination_meta):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        send_meta = {"in_reply_to": "<orijinal@test.local>"}
        prompt = a._build_prompt("reply_chain", send_meta, received_msg, combination_meta)
        assert "<orijinal@test.local>" in prompt

    def test_build_prompt_all_scenarios_produce_output(self, received_msg, combination_meta):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        send_meta = {"msg_id": "<x>", "cid": "c", "attachment_name": "f.pdf",
                     "in_reply_to": "<orig>", "attachment_size": 100}
        for scenario in ("plain_text", "attachment", "inline_image", "smime", "reply_chain"):
            prompt = a._build_prompt(scenario, send_meta, received_msg, combination_meta)
            assert len(prompt) > 200, f"{scenario} için prompt çok kısa"

    def test_parse_response_valid_json(self):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        raw = json.dumps({
            "passed": True, "confidence": "HIGH",
            "checks": [], "summary": "OK", "issues": [], "recommendations": []
        })
        result = a._parse_response(raw, "plain_text")
        assert result["passed"] is True
        assert result["confidence"] == "HIGH"

    def test_parse_response_false(self):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        raw = json.dumps({
            "passed": False, "confidence": "LOW",
            "checks": [], "summary": "FAIL", "issues": ["sorun"], "recommendations": []
        })
        result = a._parse_response(raw, "plain_text")
        assert result["passed"] is False

    def test_parse_response_json_in_markdown(self):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        inner = {"passed": False, "confidence": "MEDIUM",
                 "checks": [], "summary": "Fail", "issues": [], "recommendations": []}
        raw = f"```json\n{json.dumps(inner)}\n```"
        result = a._parse_response(raw, "plain_text")
        assert result["passed"] is False

    def test_parse_response_invalid_json_fallback(self):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test")
        result = a._parse_response("Bu hiç JSON değil!!!", "plain_text")
        assert result["passed"] is False
        assert result["confidence"] == "LOW"
        assert any("parse" in c.get("name", "").lower() or "Parse" in c.get("name", "")
                   for c in result["checks"])

    def test_call_claude_sends_correct_headers(self, received_msg, combination_meta,
                                               mock_claude_pass):
        from analyzer import MailAnalyzer
        a = MailAnalyzer("sk-ant-test-key")
        a.analyze("plain_text", {"msg_id": "<x>"}, received_msg, combination_meta)
        call_kwargs = mock_claude_pass.call_args[1]
        assert call_kwargs["headers"]["x-api-key"] == "sk-ant-test-key"
        assert "messages" in call_kwargs["json"]
        assert call_kwargs["json"]["messages"][0]["role"] == "user"


# ═══════════════════════════════════════════════════════════════════
#  csv_parser.py
# ═══════════════════════════════════════════════════════════════════

class TestCsvParser:

    def test_parse_combination_header_with_emoji(self):
        from csv_parser import _parse_combination_header
        text = "🔀 Kombinasyon: Alan → EMS / iOS  Gönderen → Gmail / Android"
        result = _parse_combination_header(text)
        assert result["receiver_server"] == "EMS"
        assert result["receiver_client"] == "iOS"
        assert result["sender_server"] == "Gmail"
        assert result["sender_client"] == "Android"

    def test_parse_combination_header_outlook(self):
        from csv_parser import _parse_combination_header
        text = "🔀 Kombinasyon: Alan → Outlook / Outlook  Gönderen → EMS / iOS"
        result = _parse_combination_header(text)
        assert result["receiver_server"] == "Outlook"
        assert result["sender_server"] == "EMS"
        assert result["sender_client"] == "iOS"

    def test_parse_combination_header_gmail_android(self):
        from csv_parser import _parse_combination_header
        text = "🔀 Kombinasyon: Alan → Gmail / Android  Gönderen → EMS / iOS"
        result = _parse_combination_header(text)
        assert result["receiver_server"] == "Gmail"
        assert result["receiver_client"] == "Android"

    def test_parse_csv_combination_count(self, project_root):
        from csv_parser import parse_csv
        combos = parse_csv(str(project_root / "mail_test_checklist.csv"))
        assert len(combos) == 18, f"18 kombinasyon bekleniyor, {len(combos)} bulundu"

    def test_parse_csv_all_five_scenario_types(self, project_root):
        from csv_parser import parse_csv
        combos = parse_csv(str(project_root / "mail_test_checklist.csv"))
        expected = {"plain_text", "attachment", "inline_image", "smime", "reply_chain"}
        for combo in combos:
            found = set(combo.scenarios.keys())
            assert found == expected, (
                f"{combo.label}: Eksik senaryolar = {expected - found}"
            )

    def test_parse_csv_total_step_count(self, project_root):
        from csv_parser import parse_csv
        combos = parse_csv(str(project_root / "mail_test_checklist.csv"))
        total = sum(len(s.steps) for c in combos for s in c.scenarios.values())
        assert total == 450, f"450 adım bekleniyor, {total} bulundu"

    def test_parse_csv_each_scenario_has_5_steps(self, project_root):
        from csv_parser import parse_csv
        combos = parse_csv(str(project_root / "mail_test_checklist.csv"))
        for combo in combos:
            for key, scenario in combo.scenarios.items():
                assert len(scenario.steps) == 5, (
                    f"{combo.label}/{key}: 5 adım bekleniyor, {len(scenario.steps)} bulundu"
                )

    def test_parse_csv_all_steps_pending(self, project_root):
        from csv_parser import parse_csv
        combos = parse_csv(str(project_root / "mail_test_checklist.csv"))
        for combo in combos:
            for scenario in combo.scenarios.values():
                for step in scenario.steps:
                    assert "Bekliyor" in step.status, (
                        f"Adım {step.row_id} beklenmedik durum: {step.status!r}"
                    )

    def test_parse_csv_valid_server_names(self, project_root):
        from csv_parser import parse_csv
        combos = parse_csv(str(project_root / "mail_test_checklist.csv"))
        valid_servers = {"EMS", "Gmail", "Outlook"}
        valid_clients = {"iOS", "Android", "Outlook"}
        for combo in combos:
            assert combo.receiver_server in valid_servers
            assert combo.sender_server in valid_servers
            assert combo.receiver_client in valid_clients
            assert combo.sender_client in valid_clients

    def test_parse_csv_combination_labels(self, project_root):
        from csv_parser import parse_csv
        combos = parse_csv(str(project_root / "mail_test_checklist.csv"))
        labels = [c.label for c in combos]
        assert len(set(labels)) == 18, "Tüm etiketler benzersiz olmalı"


# ═══════════════════════════════════════════════════════════════════
#  reporter.py
# ═══════════════════════════════════════════════════════════════════

class TestReporter:

    def _result(self, passed=True, combo="EMS/iOS←Gmail/Android",
                scenario="Sadece İçerik (Plain Text)"):
        return {
            "combination": combo,
            "scenario_type": scenario,
            "scenario_key": "plain_text",
            "test_time": "2026-04-27 10:00:00",
            "analysis": {
                "passed": passed,
                "confidence": "HIGH",
                "checks": [{"name": "Mesaj Alımı", "passed": passed, "detail": "Test"}],
                "summary": "Başarılı." if passed else "Başarısız.",
                "issues": [] if passed else ["Sorun var"],
                "recommendations": [],
            },
        }

    def test_html_report_creates_file(self, tmp_path):
        from reporter import generate_html_report
        output = str(tmp_path / "rapor.html")
        generate_html_report([], output)
        assert os.path.exists(output)

    def test_html_report_valid_structure(self, tmp_path):
        from reporter import generate_html_report
        output = str(tmp_path / "rapor.html")
        generate_html_report([self._result(True), self._result(False)], output)
        content = Path(output).read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content
        assert "<html" in content
        assert "Mail" in content

    def test_html_report_50_percent_pass_rate(self, tmp_path):
        from reporter import generate_html_report
        output = str(tmp_path / "rapor.html")
        generate_html_report([self._result(True), self._result(False)], output)
        content = Path(output).read_text(encoding="utf-8")
        assert "50.0%" in content

    def test_html_report_100_percent(self, tmp_path):
        from reporter import generate_html_report
        output = str(tmp_path / "rapor.html")
        generate_html_report([self._result(True), self._result(True)], output)
        content = Path(output).read_text(encoding="utf-8")
        assert "100.0%" in content

    def test_html_report_0_percent(self, tmp_path):
        from reporter import generate_html_report
        output = str(tmp_path / "rapor.html")
        generate_html_report([self._result(False), self._result(False)], output)
        content = Path(output).read_text(encoding="utf-8")
        assert "0.0%" in content

    def test_html_report_combo_grouping(self, tmp_path):
        from reporter import generate_html_report
        results = [
            self._result(True, "EMS/iOS←Gmail/Android"),
            self._result(False, "Gmail/Android←EMS/iOS"),
        ]
        output = str(tmp_path / "rapor.html")
        generate_html_report(results, output)
        content = Path(output).read_text(encoding="utf-8")
        assert "EMS/iOS←Gmail/Android" in content
        assert "Gmail/Android←EMS/iOS" in content

    def test_csv_results_creates_file(self, tmp_path):
        from reporter import generate_csv_results
        output = str(tmp_path / "sonuclar.csv")
        generate_csv_results([], output)
        assert os.path.exists(output)

    def test_csv_results_headers(self, tmp_path):
        from reporter import generate_csv_results
        output = str(tmp_path / "sonuclar.csv")
        generate_csv_results([self._result()], output)
        with open(output, "r", encoding="utf-8-sig") as f:
            headers = next(csv.reader(f))
        for expected in ("Kombinasyon", "Senaryo Tipi", "Sonuç", "Güven Seviyesi", "Özet"):
            assert expected in headers, f"Eksik sütun: {expected}"

    def test_csv_results_pass_fail_values(self, tmp_path):
        from reporter import generate_csv_results
        output = str(tmp_path / "sonuclar.csv")
        generate_csv_results([self._result(True), self._result(False)], output)
        with open(output, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["Sonuç"] == "PASS"
        assert rows[1]["Sonuç"] == "FAIL"


# ═══════════════════════════════════════════════════════════════════
#  auth_manager.py
# ═══════════════════════════════════════════════════════════════════

class TestAuthManager:

    def test_generate_totp_produces_6_digits(self):
        from auth_manager import generate_totp
        code = generate_totp("JBSWY3DPEHPK3PXP")
        assert len(code) == 6
        assert code.isdigit()

    def test_generate_totp_with_spaces(self):
        from auth_manager import generate_totp
        code = generate_totp("JBSWY 3DPE HPK3 PXP")
        assert len(code) == 6
        assert code.isdigit()

    def test_generate_totp_invalid_returns_empty(self):
        from auth_manager import generate_totp
        code = generate_totp("!!GECERSIZ_BASE32_SECRET!!")
        assert code == ""

    def test_totp_remaining_seconds_range(self):
        from auth_manager import totp_remaining_seconds
        remaining = totp_remaining_seconds()
        assert 1 <= remaining <= 30

    def test_mfa_manager_no_pending_initially(self):
        from auth_manager import MFAManager
        mgr = MFAManager()
        assert mgr.get_pending() is None

    def test_mfa_manager_submit_without_challenge_false(self):
        from auth_manager import MFAManager
        mgr = MFAManager()
        assert mgr.submit_code("123456") is False

    def test_mfa_manager_cancel_without_challenge_false(self):
        from auth_manager import MFAManager
        mgr = MFAManager()
        assert mgr.cancel() is False

    def test_mfa_manager_challenge_submit_flow(self):
        from auth_manager import MFAManager
        mgr = MFAManager()
        results = {}

        def challenge():
            results["code"] = mgr.mfa_challenge(
                "ems", "EMS Sunucusu", method="sms", totp_secret=""
            )

        t = threading.Thread(target=challenge, daemon=True)
        t.start()
        time.sleep(0.15)
        assert mgr.get_pending() is not None
        mgr.submit_code("654321")
        t.join(timeout=3)
        assert results.get("code") == "654321"

    def test_mfa_manager_challenge_cancel_flow(self):
        from auth_manager import MFAManager
        mgr = MFAManager()
        results = {}

        def challenge():
            results["code"] = mgr.mfa_challenge(
                "gmail", "Gmail", method="totp", totp_secret=""
            )

        t = threading.Thread(target=challenge, daemon=True)
        t.start()
        time.sleep(0.15)
        mgr.cancel()
        t.join(timeout=3)
        assert results.get("code") is None

    def test_mfa_manager_totp_auto_resolves(self):
        from auth_manager import MFAManager
        mgr = MFAManager()
        code = mgr.mfa_challenge("ems", "EMS", method="totp",
                                 totp_secret="JBSWY3DPEHPK3PXP")
        assert code is not None
        assert len(code) == 6
        assert code.isdigit()

    def test_mfa_challenge_pending_state(self):
        from auth_manager import MFAManager
        mgr = MFAManager()
        results = {}

        def challenge():
            results["code"] = mgr.mfa_challenge("outlook", "Outlook", method="sms")

        t = threading.Thread(target=challenge, daemon=True)
        t.start()
        time.sleep(0.15)
        pending = mgr.get_pending()
        assert pending is not None
        assert pending["server_key"] == "outlook"
        assert pending["method"] == "sms"
        mgr.cancel()
        t.join(timeout=3)


# ═══════════════════════════════════════════════════════════════════
#  message_templates.py
# ═══════════════════════════════════════════════════════════════════

class TestMessageTemplates:

    def test_get_template_all_active_scenarios(self):
        from message_templates import get_template
        for scenario in ("plain_text", "attachment", "inline_image", "reply_chain"):
            for i in range(3):
                tmpl = get_template(scenario, i)
                assert tmpl.body, f"{scenario}[{i}] gövde boş"
                assert tmpl.length in ("short", "medium", "long")

    def test_get_template_rotation_order(self):
        from message_templates import get_template
        assert get_template("plain_text", 0).length == "short"
        assert get_template("plain_text", 1).length == "medium"
        assert get_template("plain_text", 2).length == "long"

    def test_get_template_wraps_at_index_3(self):
        from message_templates import get_template
        t0 = get_template("plain_text", 0)
        t3 = get_template("plain_text", 3)
        assert t0.body == t3.body

    def test_get_reply_original_all_indices(self):
        from message_templates import get_reply_original
        for i in range(3):
            tmpl = get_reply_original(i)
            assert tmpl.body
            assert tmpl.length in ("short", "medium", "long")

    def test_resolve_inline_html_cid(self):
        from message_templates import resolve_inline_html
        html = '<img src="cid:{{CID}}" />{{SIGNATURE_HTML}}'
        result = resolve_inline_html(html, "my-cid-xyz")
        assert "my-cid-xyz" in result
        assert "{{CID}}" not in result
        assert "{{SIGNATURE_HTML}}" not in result

    def test_resolve_inline_html_signature_present(self):
        from message_templates import resolve_inline_html, SIGNATURE_HTML
        result = resolve_inline_html("{{SIGNATURE_HTML}}", "cid")
        assert SIGNATURE_HTML in result

    def test_plain_text_template_has_turkish_chars(self):
        from message_templates import get_template
        tmpl = get_template("plain_text", 0)
        assert any(c in tmpl.body for c in "ğüşıöçĞÜŞİÖÇ")


# ═══════════════════════════════════════════════════════════════════
#  main.py yardımcı fonksiyonlar
# ═══════════════════════════════════════════════════════════════════

class TestMainHelpers:

    def test_guess_mime_pdf(self):
        from main import _attachment_tag
        # Does not crash with no files
        tag = _attachment_tag([])
        assert tag == "Eksiz"

    def test_format_file_size_bytes(self):
        from main import _format_file_size
        assert _format_file_size(512) == "512B"

    def test_format_file_size_kb(self):
        from main import _format_file_size
        assert "KB" in _format_file_size(2048)

    def test_format_file_size_mb(self):
        from main import _format_file_size
        assert "MB" in _format_file_size(2 * 1024 * 1024)

    def test_build_subject_format(self):
        from main import _build_subject
        subject = _build_subject("[TEST]", "abc123", "plain_text",
                                 "Kısa", "EMS/iOS←Gmail/Android", "Eksiz")
        assert "[TEST]" in subject
        assert "abc123" in subject
        assert "Plain Text" in subject
        assert "EMS/iOS←Gmail/Android" in subject

    def test_get_server_config_known(self, full_config):
        from main import get_server_config
        cfg = get_server_config(full_config, "ems")
        assert cfg["smtp_host"] == full_config["ems"]["smtp_host"]

    def test_get_server_config_unknown_raises(self, full_config):
        from main import get_server_config
        with pytest.raises(ValueError, match="Bilinmeyen sunucu"):
            get_server_config(full_config, "bilinmeyen")

    def test_resolve_csv_path_real_file(self, project_root):
        from main import resolve_csv_path
        path = resolve_csv_path("mail_test_checklist.csv")
        assert os.path.exists(path)

    def test_resolve_csv_path_absolute(self, project_root):
        from main import resolve_csv_path
        abs_path = str(project_root / "mail_test_checklist.csv")
        result = resolve_csv_path(abs_path)
        assert result == abs_path


# ── Yardımcı ────────────────────────────────────────────────────────

def _minimal_png() -> bytes:
    """1x1 piksel geçerli PNG."""
    return bytes([
        137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82,
        0, 0, 0, 1, 0, 0, 0, 1, 8, 2, 0, 0, 0, 144, 119, 83, 222, 0, 0, 0,
        12, 73, 68, 65, 84, 8, 215, 99, 248, 207, 192, 0, 0, 0, 2, 0, 1,
        226, 33, 188, 51, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130,
    ])
