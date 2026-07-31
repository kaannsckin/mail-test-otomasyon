"""
test_orchestrator.py — main.py orkestratör testleri.

Mevcut "e2e" testleri sender/receiver/analyzer'ı doğrudan çağırıyordu; bu
dosya aradaki asıl iş mantığını (senaryo yönlendirme, subject üretimi, atlama
ve hata sınıflandırma, rapor toplama, CLI akışı) mock'larla test eder.
Gerçek SMTP/IMAP/LLM bağlantısı kurulmaz.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as main_mod
# pytest "Test*" sınıflarını toplamaya çalışmasın diye takma adla alınır
from csv_parser import TestCombination as Combination, TestScenario as Scenario


# ── Yardımcılar ──────────────────────────────────────────────────────

def _combo(label="EMS/iOS ← Gmail/Android", with_scenarios=True) -> Combination:
    combo = Combination(
        label=label,
        receiver_server="EMS", receiver_client="iOS",
        sender_server="Gmail", sender_client="Android",
    )
    if with_scenarios:
        for key, tr in (
            ("plain_text", "Sadece İçerik (Plain Text)"),
            ("attachment", "Eklentili Mesaj (Attachment)"),
            ("inline_image", "Inline Resim (Embedded Image)"),
            ("smime", "İmzalı Mesaj (S/MIME / PGP)"),
            ("reply_chain", "Cevaplama & Bozulma Testi (Reply Chain)"),
        ):
            combo.scenarios[key] = Scenario(
                combination=label,
                receiver_server="EMS", receiver_client="iOS",
                sender_server="Gmail", sender_client="Android",
                scenario_type=tr, scenario_key=key,
            )
    return combo


def _sender_mock():
    s = MagicMock()
    s.send_plain_text.return_value = {"msg_id": "<p@t>", "sent_at": 1.0, "scenario": "plain_text"}
    s.send_with_attachment.return_value = {"msg_id": "<a@t>", "sent_at": 1.0, "scenario": "attachment"}
    s.send_inline_image.return_value = {"msg_id": "<i@t>", "sent_at": 1.0, "scenario": "inline_image", "cid": "c@test"}
    s.send_reply.return_value = {"msg_id": "<r@t>", "sent_at": 1.0, "scenario": "reply_chain",
                                 "in_reply_to": "<p@t>"}
    s.send_smime_signed.return_value = {"msg_id": "<s@t>", "sent_at": 1.0, "scenario": "smime", "signed": True}
    return s


def _receiver_mock(found=True):
    r = MagicMock()
    r.config = {"test_address": "receiver@ems.test"}
    r.wait_for_message.return_value = {"headers": {"message_id": "<p@t>"}} if found else None
    return r


def _analyzer_mock(passed=True):
    a = MagicMock()
    a.analyze.return_value = {
        "passed": passed, "confidence": "HIGH", "checks": [],
        "summary": "ok", "issues": [], "recommendations": [],
    }
    return a


@pytest.fixture
def test_cfg(tmp_path):
    return {
        "subject_prefix": "[AUTO-TEST]",
        "wait_seconds": 0,
        "max_retries": 1,
        "retry_interval": 0,
        "test_image_path": str(_make_png(tmp_path / "img.png")),
    }


def _make_png(path: Path) -> Path:
    path.write_bytes(bytes([
        137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82,
        0, 0, 0, 1, 0, 0, 0, 1, 8, 2, 0, 0, 0, 144, 119, 83, 222, 0, 0, 0,
        12, 73, 68, 65, 84, 8, 215, 99, 248, 207, 192, 0, 0, 0, 2, 0, 1,
        226, 33, 188, 51, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130,
    ]))
    return path


# ═══════════════════════════════════════════════════════════════════
#  run_scenario — senaryo yönlendirme
# ═══════════════════════════════════════════════════════════════════

class TestRunScenarioDispatch:

    def test_plain_text_calls_send_plain_text(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        sender.send_plain_text.assert_called_once()
        assert res["scenario_key"] == "plain_text"
        assert res["analysis"]["passed"] is True
        assert res["received"] is True

    def test_attachment_calls_send_with_attachment(self, test_cfg, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        cfg = {**test_cfg, "test_attachment_paths": [str(pdf)]}
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        main_mod.run_scenario("attachment", _combo(), 0, sender, receiver, analyzer, cfg)
        args = sender.send_with_attachment.call_args[0]
        assert args[3] == [str(pdf)]

    def test_inline_image_resolves_cid_placeholder(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        main_mod.run_scenario("inline_image", _combo(), 0, sender, receiver, analyzer, test_cfg)
        kwargs = sender.send_inline_image.call_args
        html_body = kwargs.kwargs["html_body"]
        # resolve_inline_html {{CID}} yer tutucusunu korumalı — sender dolduruyor
        assert "{{CID}}" in html_body

    def test_reply_chain_sends_original_then_reply(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        with patch.object(main_mod.time, "sleep"):
            res = main_mod.run_scenario("reply_chain", _combo(), 0, sender, receiver, analyzer, test_cfg)
        sender.send_plain_text.assert_called_once()          # orijinal mesaj
        sender.send_reply.assert_called_once()               # cevap
        # Cevap, orijinalin Message-ID'sine bağlanmalı
        assert sender.send_reply.call_args[0][2] == "<p@t>"
        assert res["scenario_key"] == "reply_chain"

    def test_reply_chain_original_subject_marked(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        with patch.object(main_mod.time, "sleep"):
            main_mod.run_scenario("reply_chain", _combo(), 0, sender, receiver, analyzer, test_cfg)
        orig_subject = sender.send_plain_text.call_args[0][1]
        assert "#ORIG-" in orig_subject
        assert "Thread Başlangıç" in orig_subject

    def test_unknown_scenario_returns_fail_not_raises(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        res = main_mod.run_scenario("calendar_invite", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert res["analysis"]["passed"] is False
        assert "Bilinmeyen senaryo tipi" in res["analysis"]["issues"][0]

    def test_analyzer_receives_combination_meta(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        meta = analyzer.analyze.call_args[0][3]
        assert meta == {"sender_server": "Gmail", "sender_client": "Android",
                        "receiver_server": "EMS", "receiver_client": "iOS"}

    def test_scenario_type_taken_from_combo(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert res["scenario_type"] == "Sadece İçerik (Plain Text)"

    def test_scenario_type_falls_back_to_key(self, test_cfg):
        combo = _combo(with_scenarios=False)
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        res = main_mod.run_scenario("plain_text", combo, 0, sender, receiver, analyzer, test_cfg)
        assert res["scenario_type"] == "plain_text"

    def test_raw_bytes_stripped_from_send_meta(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        sender.send_plain_text.return_value = {"msg_id": "<p@t>", "sent_at": 1.0,
                                               "scenario": "plain_text", "raw_bytes": b"x" * 100}
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert "raw_bytes" not in res["send_meta"]
        assert res["send_meta"]["msg_id"] == "<p@t>"

    def test_message_length_recorded(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert res["message_length"] in ("short", "medium", "long")

    def test_message_not_received_still_analyzed(self, test_cfg):
        sender, analyzer = _sender_mock(), _analyzer_mock(passed=False)
        receiver = _receiver_mock(found=False)
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert res["received"] is False
        assert analyzer.analyze.call_args[0][2] is None

    def test_receiver_gets_wait_params_from_config(self, test_cfg):
        cfg = {**test_cfg, "wait_seconds": 7, "max_retries": 4, "retry_interval": 2}
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, cfg)
        kw = receiver.wait_for_message.call_args.kwargs
        assert kw["wait_seconds"] == 7 and kw["max_retries"] == 4 and kw["retry_interval"] == 2


# ═══════════════════════════════════════════════════════════════════
#  run_scenario — subject formatı
# ═══════════════════════════════════════════════════════════════════

class TestSubjectBuilding:

    def _subject_for(self, scenario, cfg, sender=None):
        sender = sender or _sender_mock()
        receiver, analyzer = _receiver_mock(), _analyzer_mock()
        with patch.object(main_mod.time, "sleep"):
            main_mod.run_scenario(scenario, _combo(), 0, sender, receiver, analyzer, cfg)
        call_map = {
            "plain_text": sender.send_plain_text,
            "attachment": sender.send_with_attachment,
            "inline_image": sender.send_inline_image,
            "reply_chain": sender.send_reply,
        }
        return call_map[scenario].call_args[0][1]

    def test_plain_text_subject_shape(self, test_cfg):
        subject = self._subject_for("plain_text", test_cfg)
        assert subject.startswith("[AUTO-TEST] #")
        assert "Senaryo: Plain Text (Eksiz)" in subject
        assert subject.endswith("EMS/iOS ← Gmail/Android")

    def test_subject_honors_custom_prefix(self, test_cfg):
        subject = self._subject_for("plain_text", {**test_cfg, "subject_prefix": "[QA]"})
        assert subject.startswith("[QA] #")

    def test_attachment_subject_has_file_summary(self, test_cfg, tmp_path):
        pdf = tmp_path / "d.pdf"; pdf.write_bytes(b"x" * 2048)
        csvf = tmp_path / "d.csv"; csvf.write_bytes(b"y" * 1024)
        cfg = {**test_cfg, "test_attachment_paths": [str(pdf), str(csvf)]}
        subject = self._subject_for("attachment", cfg)
        assert "Senaryo: Ek Dosya (2 Ek: PDF+CSV, 3.0KB)" in subject

    def test_inline_image_subject_has_image_summary(self, test_cfg):
        subject = self._subject_for("inline_image", test_cfg)
        assert "Senaryo: Inline Görsel (Gömülü PNG," in subject

    def test_reply_chain_subject_tag(self, test_cfg):
        subject = self._subject_for("reply_chain", test_cfg)
        # send_reply orijinal subject'i alır; "Re:" önekini sender ekler
        assert "Senaryo: Reply Chain" in subject

    def test_run_id_is_shared_within_scenario(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        with patch.object(main_mod.time, "sleep"):
            main_mod.run_scenario("reply_chain", _combo(), 0, sender, receiver, analyzer, test_cfg)
        orig = sender.send_plain_text.call_args[0][1]
        run_id = orig.split("#ORIG-")[1].split(" ")[0]
        body = sender.send_reply.call_args[0][4]
        assert run_id in body

    def test_rotation_index_changes_length_label(self, test_cfg):
        labels = set()
        for idx in range(3):
            sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
            main_mod.run_scenario("plain_text", _combo(), idx, sender, receiver, analyzer, test_cfg)
            labels.add(sender.send_plain_text.call_args[0][1].split("|")[2].strip())
        assert labels == {"Kısa", "Orta", "Uzun"}


# ═══════════════════════════════════════════════════════════════════
#  run_scenario — atlama ve hata yolları
# ═══════════════════════════════════════════════════════════════════

class TestRunScenarioSkipAndErrors:

    def test_smime_without_cert_is_skipped(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        res = main_mod.run_scenario("smime", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert res["skipped"] is True
        assert res["analysis"]["passed"] is None
        assert res["analysis"]["confidence"] == "N/A"
        sender.send_smime_signed.assert_not_called()
        analyzer.analyze.assert_not_called()

    def test_smime_with_cert_is_sent(self, test_cfg, tmp_path):
        cert = tmp_path / "c.pem"; cert.write_text("cert")
        key = tmp_path / "k.pem"; key.write_text("key")
        cfg = {**test_cfg, "smime_cert_path": str(cert), "smime_key_path": str(key)}
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        res = main_mod.run_scenario("smime", _combo(), 0, sender, receiver, analyzer, cfg)
        sender.send_smime_signed.assert_called_once()
        assert res.get("skipped") is not True

    def test_sender_reported_skip_is_propagated(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        sender.send_plain_text.return_value = {"skipped": True, "skip_reason": "pyOpenSSL kurulu değil"}
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert res["skipped"] is True
        assert res["analysis"]["summary"] == "pyOpenSSL kurulu değil"
        receiver.wait_for_message.assert_not_called()

    def test_generic_send_error_returns_fail(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        sender.send_plain_text.side_effect = RuntimeError("SMTP auth reddedildi")
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert res["analysis"]["passed"] is False
        assert "SMTP auth reddedildi" in res["analysis"]["summary"]
        assert res["analysis"]["recommendations"] == ["SMTP bağlantı ayarlarını kontrol edin"]

    @pytest.mark.parametrize("errno", [101, 111, 113])
    def test_blocked_smtp_port_gets_platform_guidance(self, test_cfg, errno):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        sender.send_plain_text.side_effect = OSError(errno, "Network is unreachable")
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        summary = res["analysis"]["summary"]
        assert "giden SMTP portuna çıkamıyor" in summary
        assert any("Railway" in r for r in res["analysis"]["recommendations"])
        assert any("/api/diagnostics/network" in r for r in res["analysis"]["recommendations"])

    def test_failed_send_still_reports_combination(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        sender.send_plain_text.side_effect = RuntimeError("boom")
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, test_cfg)
        assert res["combination"] == "EMS/iOS ← Gmail/Android"
        assert res["test_time"]


# ═══════════════════════════════════════════════════════════════════
#  Ağ hatası sınıflandırması
# ═══════════════════════════════════════════════════════════════════

class TestNetworkUnreachableDetection:

    @pytest.mark.parametrize("errno", [101, 111, 113])
    def test_direct_oserror_detected(self, errno):
        assert main_mod._is_network_unreachable(OSError(errno, "unreachable")) is True

    def test_unrelated_errno_not_detected(self):
        assert main_mod._is_network_unreachable(OSError(2, "No such file")) is False

    def test_chained_cause_detected(self):
        try:
            try:
                raise OSError(101, "Network is unreachable")
            except OSError as e:
                raise RuntimeError("SMTP başarısız") from e
        except RuntimeError as outer:
            assert main_mod._is_network_unreachable(outer) is True

    def test_message_text_fallback(self):
        assert main_mod._is_network_unreachable(RuntimeError("No route to host")) is True

    def test_ordinary_error_not_detected(self):
        assert main_mod._is_network_unreachable(ValueError("geçersiz kimlik")) is False


# ═══════════════════════════════════════════════════════════════════
#  Ek dosya / görsel etiket yardımcıları
# ═══════════════════════════════════════════════════════════════════

class TestAttachmentTagging:

    def test_no_paths_returns_eksiz(self):
        assert main_mod._attachment_tag([]) == "Eksiz"

    def test_all_missing_returns_eksiz(self):
        assert main_mod._attachment_tag(["/yok/a.pdf", "/yok/b.csv"]) == "Eksiz"

    def test_single_file(self, tmp_path):
        p = tmp_path / "a.pdf"; p.write_bytes(b"x" * 500)
        assert main_mod._attachment_tag([str(p)]) == "1 Ek: PDF, 500B"

    def test_duplicate_extensions_listed_once(self, tmp_path):
        a = tmp_path / "a.pdf"; a.write_bytes(b"x" * 100)
        b = tmp_path / "b.pdf"; b.write_bytes(b"x" * 100)
        assert main_mod._attachment_tag([str(a), str(b)]) == "2 Ek: PDF, 200B"

    def test_missing_file_excluded_from_count(self, tmp_path):
        p = tmp_path / "a.txt"; p.write_bytes(b"x" * 10)
        assert main_mod._attachment_tag([str(p), "/yok/b.pdf"]) == "1 Ek: TXT, 10B"

    def test_jpeg_normalized_to_jpg(self, tmp_path):
        p = tmp_path / "a.jpeg"; p.write_bytes(b"x" * 10)
        assert "JPG" in main_mod._attachment_tag([str(p)])

    def test_unknown_extension_uppercased(self, tmp_path):
        p = tmp_path / "a.odt"; p.write_bytes(b"x" * 10)
        assert "ODT" in main_mod._attachment_tag([str(p)])

    def test_inline_tag_missing_file(self):
        assert main_mod._inline_image_tag("/yok/img.png") == "Gömülü Görsel"

    def test_inline_tag_existing_png(self, tmp_path):
        p = _make_png(tmp_path / "i.png")
        tag = main_mod._inline_image_tag(str(p))
        assert tag.startswith("Gömülü PNG, ") and tag.endswith("B")

    @pytest.mark.parametrize("size,expected", [
        (0, "0B"), (1023, "1023B"), (1024, "1.0KB"),
        (1536, "1.5KB"), (1024 * 1024, "1.0MB"), (1024 * 1024 * 3, "3.0MB"),
    ])
    def test_format_file_size(self, size, expected):
        assert main_mod._format_file_size(size) == expected


# ═══════════════════════════════════════════════════════════════════
#  Ek dosya yolu çözümleme
# ═══════════════════════════════════════════════════════════════════

class TestResolveAttachmentPaths:

    def test_list_config_preferred(self, tmp_path):
        a = tmp_path / "a.pdf"; a.write_text("a")
        b = tmp_path / "b.csv"; b.write_text("b")
        old = tmp_path / "old.pdf"; old.write_text("o")
        out = main_mod._resolve_attachment_paths({
            "test_attachment_paths": [str(a), str(b)],
            "test_attachment_path": str(old),
        })
        assert out == [str(a), str(b)]

    def test_falls_back_to_single_path(self, tmp_path):
        p = tmp_path / "only.pdf"; p.write_text("x")
        assert main_mod._resolve_attachment_paths({"test_attachment_path": str(p)}) == [str(p)]

    def test_missing_files_skipped(self, tmp_path):
        p = tmp_path / "a.pdf"; p.write_text("x")
        out = main_mod._resolve_attachment_paths({"test_attachment_paths": [str(p), "/yok/x.pdf"]})
        assert out == [str(p)]

    def test_empty_list_falls_back_to_single(self, tmp_path):
        p = tmp_path / "s.pdf"; p.write_text("x")
        assert main_mod._resolve_attachment_paths(
            {"test_attachment_paths": [], "test_attachment_path": str(p)}) == [str(p)]

    def test_relative_path_resolved_against_repo_root(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # cwd'de dosya yok
        out = main_mod._resolve_attachment_paths({"test_attachment_paths": ["mail_test_checklist.csv"]})
        assert out == [str(PROJECT_ROOT / "mail_test_checklist.csv")]

    def test_nothing_configured_returns_empty_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch.object(main_mod.Path, "exists", return_value=False):
            assert main_mod._resolve_attachment_paths({}) == []


# ═══════════════════════════════════════════════════════════════════
#  Test dosyası hazırlığı
# ═══════════════════════════════════════════════════════════════════

class TestPrepareTestFiles:

    def test_creates_pdf_and_png(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        main_mod.prepare_test_files()
        pdf = tmp_path / "test_files" / "test_document.pdf"
        png = tmp_path / "test_files" / "test_image.png"
        assert pdf.exists() and png.exists()
        assert pdf.read_bytes().startswith(b"%PDF-1.4")
        assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    def test_does_not_overwrite_existing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "test_files").mkdir()
        pdf = tmp_path / "test_files" / "test_document.pdf"
        pdf.write_bytes(b"KULLANICI DOSYASI")
        main_mod.prepare_test_files()
        assert pdf.read_bytes() == b"KULLANICI DOSYASI"

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        main_mod.prepare_test_files()
        first = (tmp_path / "test_files" / "test_image.png").read_bytes()
        main_mod.prepare_test_files()
        assert (tmp_path / "test_files" / "test_image.png").read_bytes() == first


# ═══════════════════════════════════════════════════════════════════
#  CSV yolu çözümleme
# ═══════════════════════════════════════════════════════════════════

class TestResolveCsvPath:

    def test_absolute_existing_path_kept(self, tmp_path):
        p = tmp_path / "x.csv"; p.write_text("a,b\n")
        assert main_mod.resolve_csv_path(str(p)) == str(p)

    def test_relative_resolved_against_repo_root(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert main_mod.resolve_csv_path("mail_test_checklist.csv") == \
            str(PROJECT_ROOT / "mail_test_checklist.csv")

    def test_missing_file_falls_back_to_repo_checklist(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert main_mod.resolve_csv_path("/yok/olmayan.csv") == \
            str(PROJECT_ROOT / "mail_test_checklist.csv")

    def test_basename_only_resolved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out = main_mod.resolve_csv_path("bir/yerde/mail_test_checklist.csv")
        assert out == str(PROJECT_ROOT / "mail_test_checklist.csv")

    def test_glob_fallback_prefers_checklist_csv(self, tmp_path, monkeypatch):
        """Beklenen CSV yoksa repo kökündeki en olası CSV seçilir."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(main_mod, "__file__", str(tmp_path / "main.py"))
        # Beklenen adda dosya YOK — glob yedeği "checklist" içereni tercih etmeli
        (tmp_path / "aaa_rastgele.csv").write_text("a\n", encoding="utf-8")
        (tmp_path / "eski_checklist.csv").write_text("a\n", encoding="utf-8")
        assert main_mod.resolve_csv_path("/yok/x.csv") == str(tmp_path / "eski_checklist.csv")

    def test_glob_fallback_picks_shortest_when_tied(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(main_mod, "__file__", str(tmp_path / "main.py"))
        (tmp_path / "cok_uzun_bir_isim.csv").write_text("a\n", encoding="utf-8")
        (tmp_path / "kisa.csv").write_text("a\n", encoding="utf-8")
        assert main_mod.resolve_csv_path("/yok/x.csv") == str(tmp_path / "kisa.csv")

    def test_no_csv_anywhere_returns_input_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(main_mod, "__file__", str(tmp_path / "main.py"))
        assert main_mod.resolve_csv_path("/yok/x.csv") == "/yok/x.csv"


class TestSetupLogging:

    def test_creates_log_dir_and_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        log_file = tmp_path / "logs" / "otomasyon.log"
        with patch.object(main_mod.logging, "basicConfig") as bc:
            main_mod.setup_logging({"logging": {"level": "INFO", "file": str(log_file)}})
        assert (tmp_path / "logs").is_dir()
        assert bc.call_args.kwargs["level"] == main_mod.logging.INFO

    def test_level_from_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch.object(main_mod.logging, "basicConfig") as bc:
            main_mod.setup_logging({"logging": {"level": "DEBUG", "file": str(tmp_path / "a.log")}})
        assert bc.call_args.kwargs["level"] == main_mod.logging.DEBUG

    def test_defaults_when_logging_section_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch.object(main_mod.logging, "basicConfig") as bc:
            main_mod.setup_logging({})
        assert bc.call_args.kwargs["level"] == main_mod.logging.INFO


# ═══════════════════════════════════════════════════════════════════
#  get_server_config
# ═══════════════════════════════════════════════════════════════════

class TestGetServerConfig:

    @pytest.mark.parametrize("name", ["ems", "EMS", "Gmail", "GMAIL", "outlook", "Outlook"])
    def test_case_insensitive(self, name, full_config):
        assert main_mod.get_server_config(full_config, name)

    def test_unknown_server_raises(self, full_config):
        with pytest.raises(ValueError, match="Bilinmeyen sunucu"):
            main_mod.get_server_config(full_config, "yandex")

    def test_known_name_missing_from_config_raises(self):
        with pytest.raises(ValueError):
            main_mod.get_server_config({"ems": {}}, "gmail")


# ═══════════════════════════════════════════════════════════════════
#  main() CLI akışı
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def cli_env(tmp_path, monkeypatch, full_config):
    """main() çalıştırmaya hazır izole çalışma dizini + config dosyası."""
    monkeypatch.chdir(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg = {**full_config}
    cfg["test"] = {**cfg["test"],
                   "report_output": str(tmp_path / "reports" / "r.html"),
                   "results_csv": str(tmp_path / "reports" / "r.csv"),
                   "csv_input": str(PROJECT_ROOT / "mail_test_checklist.csv")}
    cfg["logging"] = {"level": "WARNING", "file": str(tmp_path / "t.log")}
    cfg_path.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(main_mod, "setup_logging", lambda c: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda *a, **k: None)
    return cfg_path


def _run_cli(argv):
    with patch.object(sys, "argv", ["main.py"] + argv):
        main_mod.main()


class TestMainCli:

    def test_missing_config_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _run_cli(["--config", str(tmp_path / "yok.yaml")])
        assert exc.value.code == 1
        assert "Config dosyası bulunamadı" in capsys.readouterr().out

    def test_invalid_combo_index_exits_1(self, cli_env):
        with pytest.raises(SystemExit) as exc:
            _run_cli(["--config", str(cli_env), "--combo", "999"])
        assert exc.value.code == 1

    def test_empty_csv_exits_1(self, cli_env, tmp_path):
        empty = tmp_path / "bos.csv"
        empty.write_text("bir,iki\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _run_cli(["--config", str(cli_env), "--csv", str(empty)])
        assert exc.value.code == 1

    def test_dry_run_connects_without_sending(self, cli_env):
        with patch.object(main_mod, "MailSender") as MS, \
             patch.object(main_mod, "generate_html_report") as gen:
            _run_cli(["--config", str(cli_env), "--combo", "0", "--dry-run"])
        assert MS.return_value._connect.called
        MS.return_value.send_plain_text.assert_not_called()
        gen.assert_not_called()

    def test_dry_run_logs_blocked_port_guidance(self, cli_env, caplog):
        with patch.object(main_mod, "MailSender") as MS:
            MS.return_value._connect.side_effect = OSError(101, "Network is unreachable")
            with caplog.at_level("ERROR"):
                _run_cli(["--config", str(cli_env), "--combo", "0", "--dry-run"])
        assert "diagnostics/network" in caplog.text

    def test_single_combo_runs_all_scenarios(self, cli_env):
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "run_scenario") as rs, \
             patch.object(main_mod, "generate_html_report"), \
             patch.object(main_mod, "generate_csv_results"):
            rs.return_value = {"analysis": {"passed": True}}
            _run_cli(["--config", str(cli_env), "--combo", "0"])
        assert rs.call_count == 5  # CSV'deki 5 senaryo tipi

    def test_scenario_filter_limits_run(self, cli_env):
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "run_scenario") as rs, \
             patch.object(main_mod, "generate_html_report"), \
             patch.object(main_mod, "generate_csv_results"):
            rs.return_value = {"analysis": {"passed": True}}
            _run_cli(["--config", str(cli_env), "--combo", "0", "--scenario", "plain_text"])
        assert rs.call_count == 1
        assert rs.call_args[0][0] == "plain_text"

    def test_reports_generated_with_results(self, cli_env):
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "run_scenario") as rs, \
             patch.object(main_mod, "generate_html_report") as html, \
             patch.object(main_mod, "generate_csv_results") as csvr:
            rs.return_value = {"analysis": {"passed": True}}
            _run_cli(["--config", str(cli_env), "--combo", "0"])
        assert html.called and csvr.called
        assert len(html.call_args[0][0]) == 5

    def test_scenario_exception_does_not_abort_run(self, cli_env):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("beklenmeyen")
            return {"analysis": {"passed": True}}

        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "run_scenario", side_effect=flaky), \
             patch.object(main_mod, "generate_html_report") as html, \
             patch.object(main_mod, "generate_csv_results"):
            _run_cli(["--config", str(cli_env), "--combo", "0"])
        # 5 senaryodan biri patladı, kalan 4'ü raporlandı
        assert len(html.call_args[0][0]) == 4

    def test_unknown_server_skips_combination(self, cli_env, tmp_path):
        cfg = yaml.safe_load(cli_env.read_text(encoding="utf-8"))
        del cfg["gmail"]
        cli_env.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "run_scenario") as rs, \
             patch.object(main_mod, "generate_html_report"):
            _run_cli(["--config", str(cli_env), "--combo", "0"])
        rs.assert_not_called()

    def test_no_results_skips_reporting(self, cli_env, caplog):
        cfg = yaml.safe_load(cli_env.read_text(encoding="utf-8"))
        del cfg["gmail"]
        cli_env.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "generate_html_report") as html, \
             caplog.at_level("WARNING"):
            _run_cli(["--config", str(cli_env), "--combo", "0"])
        html.assert_not_called()
        assert "Hiçbir test sonucu üretilemedi" in caplog.text

    def test_pass_rate_excludes_skipped(self, cli_env, caplog):
        results = [
            {"analysis": {"passed": True}}, {"analysis": {"passed": True}},
            {"analysis": {"passed": False}}, {"analysis": {"passed": None}},
            {"analysis": {"passed": None}},
        ]
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "run_scenario", side_effect=results), \
             patch.object(main_mod, "generate_html_report"), \
             patch.object(main_mod, "generate_csv_results"), \
             caplog.at_level("INFO"):
            _run_cli(["--config", str(cli_env), "--combo", "0"])
        # 2/3 PASS = %66.7, 2 atlandı — atlananlar paydaya girmemeli
        assert "Sonuç: 2/3 PASS (66.7%)" in caplog.text
        assert "2 atlandı" in caplog.text


class TestAnalyzerProviderSelection:

    def test_claude_is_default(self, cli_env):
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer") as MA, \
             patch.object(main_mod, "run_scenario", return_value={"analysis": {"passed": True}}), \
             patch.object(main_mod, "generate_html_report"), \
             patch.object(main_mod, "generate_csv_results"):
            _run_cli(["--config", str(cli_env), "--combo", "0", "--scenario", "plain_text"])
        assert MA.call_args.kwargs["api_key"] == "sk-ant-test"
        assert "provider" not in MA.call_args.kwargs

    def test_gemini_selected_by_config(self, cli_env):
        cfg = yaml.safe_load(cli_env.read_text(encoding="utf-8"))
        cfg["analysis"] = {"provider": "gemini"}
        cfg["gemini"] = {"api_key": "gem-key", "model": "gemini-2.0-flash"}
        cli_env.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer") as MA, \
             patch.object(main_mod, "run_scenario", return_value={"analysis": {"passed": True}}), \
             patch.object(main_mod, "generate_html_report"), \
             patch.object(main_mod, "generate_csv_results"):
            _run_cli(["--config", str(cli_env), "--combo", "0", "--scenario", "plain_text"])
        assert MA.call_args.kwargs["provider"] == "gemini"
        assert MA.call_args.kwargs["api_key"] == "gem-key"

    def test_provider_name_case_insensitive(self, cli_env):
        cfg = yaml.safe_load(cli_env.read_text(encoding="utf-8"))
        cfg["analysis"] = {"provider": "GEMINI"}
        cfg["gemini"] = {"api_key": "g", "model": ""}
        cli_env.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer") as MA, \
             patch.object(main_mod, "run_scenario", return_value={"analysis": {"passed": True}}), \
             patch.object(main_mod, "generate_html_report"), \
             patch.object(main_mod, "generate_csv_results"):
            _run_cli(["--config", str(cli_env), "--combo", "0", "--scenario", "plain_text"])
        assert MA.call_args.kwargs["provider"] == "gemini"
        assert MA.call_args.kwargs["model"] is None  # boş model → varsayılan
