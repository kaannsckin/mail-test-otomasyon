"""
test_catalog_scenarios.py — Katalog (spec) senaryoları, deterministik
doğrulayıcı ve manuel doğrulama akışı.

"Loopback" düzeneği: gönderilen ham baytlar bellekte tutulur ve alıcı aynı
baytları geri okur — araya isteğe bağlı bir "sunucu bozucusu" (mutator)
konularak gerçek sunucuların yaptığı hasar benzetilir. Böylece her senaryonun
hem sağlam mesajda PASS, hem bozulmuş mesajda FAIL verdiği kanıtlanır.
"""

import csv
import email
import json
import quopri
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as main_mod  # noqa: E402
import manual_verify  # noqa: E402
from analyzer import NullAnalyzer  # noqa: E402
from csv_parser import SCENARIO_TYPE_MAP, SUPPORTED_SCENARIOS, parse_csv  # noqa: E402
from receiver import MailReceiver  # noqa: E402
from reporter import generate_csv_results, generate_html_report  # noqa: E402
from scenario_catalog import (  # noqa: E402
    SPEC_SCENARIOS, catalog, expand_scenario_filter, is_valid_scenario_filter,
    manual_steps_for,
)
from sender import MailSender  # noqa: E402
from verifier import merge_verdicts, verify  # noqa: E402

SPEC_KEYS = sorted(SPEC_SCENARIOS)


# ── Loopback düzeneği ────────────────────────────────────────────────

class Loopback:
    """Bellek içi 'mail sunucusu': SMTP ile geleni IMAP ile geri verir."""

    def __init__(self, mutator=None):
        self.mutator = mutator
        self.store: dict = {}
        self.envelopes: list = []

    def smtp(self):
        inst = MagicMock()
        inst.__enter__ = MagicMock(return_value=inst)
        inst.__exit__ = MagicMock(return_value=False)
        inst.has_extn.return_value = True

        def sendmail(from_addr, to_addrs, raw, **_kw):
            assert isinstance(raw, bytes)
            # Kablo kuralları: yalnızca CRLF, satır ≤ 998 oktet (RFC 5322 §2.1.1)
            assert b"\n" not in raw.replace(b"\r\n", b""), "yalın LF kabloya çıktı"
            assert max(len(line) for line in raw.split(b"\r\n")) <= 998
            if self.mutator:
                raw = self.mutator(raw)
            msg_id = " ".join(str(email.message_from_bytes(raw)["Message-ID"]).split())
            self.store[msg_id] = raw
            self.envelopes.append(list(to_addrs))
            return {}
        inst.sendmail.side_effect = sendmail
        return inst

    def wait_for_message(self, receiver, expected_msg_id, **_kw):
        raw = self.store.get(" ".join(expected_msg_id.split()))
        if raw is None:
            return None
        return receiver._extract_details(email.message_from_bytes(raw), raw, "1")


def _server_cfg(name: str) -> dict:
    return {"smtp_host": f"smtp.{name}.test", "smtp_port": 587, "smtp_use_tls": False,
            "imap_host": f"imap.{name}.test", "imap_port": 993, "imap_use_ssl": True,
            "username": f"kullanici@{name}.test", "password": "x",
            "test_address": f"kullanici@{name}.test"}


@pytest.fixture
def combo():
    return SimpleNamespace(label="EMS/iOS ← Gmail/Android", receiver_server="EMS",
                           receiver_client="iOS", sender_server="Gmail",
                           sender_client="Android", scenarios={})


@pytest.fixture
def test_cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main_mod.prepare_test_files()
    return {"wait_seconds": 0, "max_retries": 1, "retry_interval": 0,
            "subject_prefix": "[TEST]", "large_attachment_mb": 0.05,
            "test_attachment_path": "test_files/test_document.pdf",
            "test_image_path": "test_files/test_image.png"}


def run(scenario, combo, test_cfg, mutator=None, analyzer=None):
    loop = Loopback(mutator)
    sender, receiver = MailSender(_server_cfg("gmail")), MailReceiver(_server_cfg("ems"))
    with patch.object(MailSender, "_connect", side_effect=lambda: loop.smtp()), \
         patch.object(MailReceiver, "wait_for_message",
                      side_effect=lambda **kw: loop.wait_for_message(receiver, **kw)):
        result = main_mod.run_scenario(scenario, combo, 0, sender, receiver,
                                       analyzer or NullAnalyzer(), test_cfg)
    return result, loop


def failed_checks(result):
    return [c["name"] for c in result["analysis"]["checks"] if not c["passed"]]


# ═══════════════════════════════════════════════════════════════════
#  Sağlam iletim: her senaryo PASS
# ═══════════════════════════════════════════════════════════════════

class TestCleanDelivery:

    @pytest.mark.parametrize("scenario", SPEC_KEYS)
    def test_scenario_passes_on_untouched_message(self, scenario, combo, test_cfg):
        result, _ = run(scenario, combo, test_cfg)
        analysis = result["analysis"]
        assert analysis["passed"] is True, failed_checks(result)
        assert analysis["verdict_source"] == "deterministic"
        assert analysis["confidence"] == "HIGH"
        assert any(c["name"].startswith("[Kesin]") for c in analysis["checks"])

    @pytest.mark.parametrize("scenario", SPEC_KEYS)
    def test_result_is_identifiable_for_manual_check(self, scenario, combo, test_cfg):
        result, _ = run(scenario, combo, test_cfg)
        # Kullanıcı mesajı istemcide konu satırındaki Run ID ile bulur.
        assert re.fullmatch(r"[0-9a-f]{8}", result["result_id"])
        assert f"#{result['result_id']}" in result["subject"]
        assert result["receiver_client"] == "iOS"
        assert len(result["manual_steps"]) >= 2
        assert manual_verify.is_eligible(result)

    def test_private_fields_not_leaked_into_result(self, combo, test_cfg):
        result, _ = run("large_attachment", combo, test_cfg)
        assert not {"expect", "raw_bytes", "check_prompt"} & set(result["send_meta"])
        json.dumps(result)   # JSON'a yazılabilir olmalı

    def test_cc_recipient_is_in_envelope(self, combo, test_cfg):
        _, loop = run("cc_reply_to", combo, test_cfg)
        assert loop.envelopes == [["kullanici@ems.test", "kullanici@gmail.test"]]

    def test_declared_encodings_reach_the_wire(self, combo, test_cfg):
        expected = {"enc_quoted_printable": ("utf-8", "quoted-printable"),
                    "enc_base64": ("utf-8", "base64"),
                    "enc_8bit": ("utf-8", "8bit"),
                    "charset_iso8859_9": ("iso-8859-9", "quoted-printable"),
                    "charset_windows_1254": ("windows-1254", "base64"),
                    "smtp_transparency": ("us-ascii", "7bit")}
        for scenario, (charset, cte) in expected.items():
            _, loop = run(scenario, combo, test_cfg)
            msg = email.message_from_bytes(next(iter(loop.store.values())))
            assert msg.get_content_charset() == charset, scenario
            assert msg["Content-Transfer-Encoding"] == cte, scenario

    def test_8bit_body_is_raw_utf8_on_the_wire(self, combo, test_cfg):
        _, loop = run("enc_8bit", combo, test_cfg)
        assert "ğüşıöç ĞÜŞİÖÇ".encode("utf-8") in next(iter(loop.store.values()))

    def test_legacy_charset_subject_is_not_utf8(self, combo, test_cfg):
        _, loop = run("charset_iso8859_9", combo, test_cfg)
        raw = next(iter(loop.store.values()))
        assert b"=?iso-8859-9?q?" in raw and b"=?utf-8?" not in raw

    def test_priority_headers_on_the_wire(self, combo, test_cfg):
        for scenario, importance, xprio in (("priority_high", "high", "1"),
                                            ("priority_low", "low", "5")):
            _, loop = run(scenario, combo, test_cfg)
            msg = email.message_from_bytes(next(iter(loop.store.values())))
            assert msg["Importance"] == importance
            assert msg["X-Priority"].startswith(xprio)


# ═══════════════════════════════════════════════════════════════════
#  Sunucu hasarı: doğrulayıcı yakalamalı
# ═══════════════════════════════════════════════════════════════════

def _drop_headers(*names):
    pattern = re.compile(
        rb"^(?:" + b"|".join(re.escape(n.encode()) for n in names) + rb"):.*\r\n(?:[ \t].*\r\n)*",
        re.M | re.I)
    return lambda raw: pattern.sub(b"", raw)


def _strip_high_bit(raw: bytes) -> bytes:
    head, _, body = raw.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n" + bytes(b & 0x7F for b in body)


def _transcode_8bit_to_qp(raw: bytes) -> bytes:
    """8BITMIME desteklemeyen aktarıcının meşru dönüşümü: içerik aynı kalır."""
    head, _, body = raw.partition(b"\r\n\r\n")
    head = head.replace(b"Content-Transfer-Encoding: 8bit",
                        b"Content-Transfer-Encoding: quoted-printable")
    encoded = quopri.encodestring(body.replace(b"\r\n", b"\n")).replace(b"\n", b"\r\n")
    return head + b"\r\n\r\n" + encoded


class TestServerDamageIsDetected:

    def test_all_priority_headers_stripped(self, combo, test_cfg):
        result, _ = run("priority_high", combo, test_cfg, _drop_headers(
            "X-Priority", "X-MSMail-Priority", "Importance", "Priority"))
        assert result["analysis"]["passed"] is False
        assert "[Kesin] Öncelik düzeyi" in failed_checks(result)

    def test_partial_priority_loss_still_passes(self, combo, test_cfg):
        # Sunucu yalnızca Importance'ı bırakmış: istemci önceliği yine gösterir.
        result, _ = run("priority_low", combo, test_cfg, _drop_headers(
            "X-Priority", "X-MSMail-Priority", "Priority"))
        assert result["analysis"]["passed"] is True
        info = next(c for c in result["analysis"]["checks"]
                    if c["name"] == "[Bilgi] Öncelik başlıkları")
        assert "korunan 1/4" in info["detail"] and "X-Priority" in info["detail"]

    def test_priority_flipped_to_normal(self, combo, test_cfg):
        result, _ = run("priority_high", combo, test_cfg,
                        lambda raw: raw.replace(b"Importance: high", b"Importance: normal"))
        assert "[Kesin] Öncelik düzeyi" in failed_checks(result)

    def test_high_bit_stripping_breaks_turkish(self, combo, test_cfg):
        result, _ = run("enc_8bit", combo, test_cfg, _strip_high_bit)
        assert "[Kesin] Gövde metni" in failed_checks(result)

    def test_legit_8bit_to_qp_transcoding_passes(self, combo, test_cfg):
        result, _ = run("enc_8bit", combo, test_cfg, _transcode_8bit_to_qp)
        assert result["analysis"]["passed"] is True, failed_checks(result)
        info = next(c for c in result["analysis"]["checks"]
                    if c["name"] == "[Bilgi] Kodlama beyanı")
        assert "yeniden kodlamış" in info["detail"]

    def test_appended_disclaimer_is_tolerated(self, combo, test_cfg):
        result, _ = run("smtp_transparency", combo, test_cfg,
                        lambda raw: raw + b"\r\n-- Kurumsal yasal uyari --\r\n")
        body = next(c for c in result["analysis"]["checks"] if c["name"] == "[Kesin] Gövde metni")
        assert body["passed"] and "ek içerik" in body["detail"]

    def test_from_munging_detected(self, combo, test_cfg):
        result, _ = run("smtp_transparency", combo, test_cfg,
                        lambda raw: raw.replace(b"\r\nFrom ile", b"\r\n>From ile"))
        assert "[Kesin] Gövde metni" in failed_checks(result)

    def test_lost_leading_dot_detected(self, combo, test_cfg):
        result, _ = run("smtp_transparency", combo, test_cfg,
                        lambda raw: raw.replace(b"\r\n..iki nokta", b"\r\n.iki nokta"))
        assert "[Kesin] Gövde metni" in failed_checks(result)

    def test_latin5_mislabelled_as_latin1(self, combo, test_cfg):
        result, _ = run("charset_iso8859_9", combo, test_cfg,
                        lambda raw: raw.replace(b'charset="iso-8859-9"', b'charset="iso-8859-1"'))
        assert "[Kesin] Gövde metni" in failed_checks(result)

    def test_truncated_attachment_detected(self, combo, test_cfg):
        def truncate(raw):
            lines = raw.split(b"\r\n")
            return b"\r\n".join(lines[:-40] + lines[-2:])
        result, _ = run("large_attachment", combo, test_cfg, truncate)
        assert any(n.startswith("[Kesin] Ek bütünlüğü") for n in failed_checks(result))

    def test_subject_change_detected(self, combo, test_cfg):
        result, _ = run("enc_base64", combo, test_cfg,
                        lambda raw: raw.replace(b"Subject: ", b"Subject: [DIS KAYNAK] ", 1))
        assert "[Kesin] Konu korunumu" in failed_checks(result)

    def test_url_rewriting_detected(self, combo, test_cfg):
        result, _ = run("html_links", combo, test_cfg, lambda raw: raw.replace(
            b"http://example.org/", b"https://korumali.gecit.example/?u=example.org"))
        failed = result["analysis"]["checks"]
        link = next(c for c in failed if c["name"] == "[Kesin] Bağlantı bütünlüğü (href)")
        assert not link["passed"] and "http://example.org/" in link["detail"]

    def test_cc_header_dropped(self, combo, test_cfg):
        result, _ = run("cc_reply_to", combo, test_cfg, _drop_headers("Cc"))
        assert "[Kesin] Adres başlığı: Cc" in failed_checks(result)

    def test_reply_to_display_name_mangled(self, combo, test_cfg):
        def mangle(raw):
            msg = email.message_from_bytes(raw)
            msg.replace_header("Reply-To", "Yanit Masasi <kullanici@gmail.test>")
            return msg.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        result, _ = run("cc_reply_to", combo, test_cfg, mangle)
        assert "[Kesin] Adres başlığı: Reply-To" in failed_checks(result)

    def test_custom_header_stripped(self, combo, test_cfg):
        result, _ = run("custom_headers", combo, test_cfg, _drop_headers("List-Unsubscribe"))
        assert failed_checks(result) == ["[Kesin] Başlık: List-Unsubscribe"]

    def test_mdn_request_stripped(self, combo, test_cfg):
        result, _ = run("read_receipt", combo, test_cfg,
                        _drop_headers("Disposition-Notification-To"))
        assert "[Kesin] Adres başlığı: Disposition-Notification-To" in failed_checks(result)

    def test_inline_image_reencoded(self, combo, test_cfg):
        def swap_image(raw):
            msg = email.message_from_bytes(raw)
            for part in msg.walk():
                if part.get_content_type() == "image/png":
                    part.set_payload("iVBORw0KGgo=")
            return msg.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        result, _ = run("nested_multipart", combo, test_cfg, swap_image)
        assert "[Kesin] Gömülü görsel (CID)" in failed_checks(result)

    def test_filename_mangled_but_content_intact(self, combo, test_cfg):
        def rename(raw):
            msg = email.message_from_bytes(raw)
            for part in msg.walk():
                if part.get_filename():
                    part.replace_header("Content-Disposition", 'attachment; filename="rapor.txt"')
            return msg.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        result, _ = run("header_encoding", combo, test_cfg, rename)
        failed = failed_checks(result)
        assert any(n.startswith("[Kesin] Ek dosya adı") for n in failed)
        assert not any(n.startswith("[Kesin] Ek bütünlüğü") for n in failed)

    def test_message_not_delivered_is_fail(self, combo, test_cfg):
        loop = Loopback()
        sender, receiver = MailSender(_server_cfg("gmail")), MailReceiver(_server_cfg("ems"))
        with patch.object(MailSender, "_connect", side_effect=lambda: loop.smtp()), \
             patch.object(MailReceiver, "wait_for_message", return_value=None):
            result = main_mod.run_scenario("priority_high", combo, 0, sender, receiver,
                                           NullAnalyzer(), test_cfg)
        assert result["analysis"]["passed"] is False
        assert "timeout" in result["analysis"]["summary"]


# ═══════════════════════════════════════════════════════════════════
#  Karar birleştirme: deterministik bağlayıcı, LLM danışma
# ═══════════════════════════════════════════════════════════════════

_DET_OK = [{"name": "[Kesin] Gövde metni", "passed": True, "detail": "aynı"}]
_DET_BAD = [{"name": "[Kesin] Gövde metni", "passed": False, "detail": "değişmiş"},
            {"name": "[Bilgi] Kodlama beyanı", "passed": True, "detail": "..."}]


class TestMergeVerdicts:

    def test_llm_disagreement_keeps_pass_but_lowers_confidence(self):
        llm = {"passed": False, "summary": "Başlık şüpheli", "issues": ["X eksik"],
               "checks": [{"name": "Başlık", "passed": False, "detail": "?"}],
               "recommendations": ["bak"]}
        out = merge_verdicts(_DET_OK, llm)
        assert out["passed"] is True and out["confidence"] == "MEDIUM"
        assert any("LLM farklı görüşte" in i for i in out["issues"])
        assert {"name": "[LLM] Başlık", "passed": False, "detail": "?"} in out["checks"]

    def test_llm_cannot_rescue_deterministic_failure(self):
        llm = {"passed": True, "summary": "Sorun yok", "issues": [], "recommendations": [],
               "checks": [{"name": "Genel", "passed": True, "detail": "ok"}]}
        out = merge_verdicts(_DET_BAD, llm)
        assert out["passed"] is False and out["confidence"] == "HIGH"
        assert "1/1 kesin kontrol başarısız" in out["summary"]

    def test_llm_outage_does_not_fail_scenario(self):
        llm = {"passed": False, "summary": "Claude API erişim hatası: 503",
               "checks": [{"name": "LLM Analizi", "passed": False, "detail": "503"}]}
        out = merge_verdicts(_DET_OK, llm)
        assert out["passed"] is True and out["confidence"] == "HIGH"
        assert "LLM yorumu alınamadı" in out["summary"]
        assert all(not c["name"].startswith("[LLM]") for c in out["checks"])

    def test_info_checks_never_fail(self):
        checks = [{"name": "[Bilgi] Kodlama beyanı", "passed": False, "detail": "x"}]
        assert merge_verdicts(checks, None)["passed"] is True

    def test_empty_raw_message_fails(self):
        assert verify({"subject": "x"}, b"")[0]["passed"] is False

    def test_llm_gets_checklist_and_deterministic_results(self, combo, test_cfg):
        analyzer = MagicMock()
        analyzer.analyze.return_value = {"passed": True, "confidence": "HIGH", "checks": [],
                                         "summary": "", "issues": [], "recommendations": []}
        run("priority_high", combo, test_cfg, analyzer=analyzer)
        send_meta = analyzer.analyze.call_args[0][1]
        assert "Kontrol edilecekler:" in send_meta["check_prompt"]
        assert send_meta["deterministic_checks"]


# ═══════════════════════════════════════════════════════════════════
#  Katalog bütünlüğü, filtreler, checklist
# ═══════════════════════════════════════════════════════════════════

class TestCatalog:

    def test_every_spec_is_registered_everywhere(self):
        for key, spec in SPEC_SCENARIOS.items():
            assert key in SUPPORTED_SCENARIOS
            assert SCENARIO_TYPE_MAP[spec.csv_name] == key
            assert main_mod.SCENARIO_LABELS[key] == spec.label
            assert len(spec.steps) == 5, f"{key}: checklist 5 adım olmalı"
            assert len(spec.manual_steps) >= 2

    def test_every_supported_scenario_has_manual_steps(self):
        for key in SUPPORTED_SCENARIOS:
            assert len(manual_steps_for(key)) >= 2, key

    def test_catalog_lists_all_runnable_scenarios(self):
        keys = {item["key"] for item in catalog()}
        assert keys == set(SUPPORTED_SCENARIOS) - {"forward_chain"}

    def test_checklist_csv_has_catalog_steps(self):
        combos = parse_csv(str(PROJECT_ROOT / "mail_test_checklist.csv"))
        for c in combos:
            for key, spec in SPEC_SCENARIOS.items():
                steps = [s.step_description for s in c.scenarios[key].steps]
                assert steps == list(spec.steps), f"{c.label}/{key}"

    def test_filter_forms(self):
        assert expand_scenario_filter(None) is None
        assert expand_scenario_filter("plain_text") == {"plain_text"}
        assert expand_scenario_filter("enc_base64, priority_high") == {"enc_base64", "priority_high"}
        assert expand_scenario_filter("group:oncelik") == {
            "priority_high", "priority_low", "read_receipt"}
        assert "plain_text" in expand_scenario_filter("group:temel")
        assert "enc_8bit" in expand_scenario_filter("group:kodlama,plain_text")

    def test_filter_validation(self):
        ok = set(SUPPORTED_SCENARIOS)
        assert is_valid_scenario_filter("plain_text,group:kodlama", ok)
        assert not is_valid_scenario_filter("group:yok", ok)
        assert not is_valid_scenario_filter("plain_text,--config", ok)
        assert not is_valid_scenario_filter(" , ", ok)


# ═══════════════════════════════════════════════════════════════════
#  Manuel doğrulama kayıtları
# ═══════════════════════════════════════════════════════════════════

def _result(rid="abcd1234", passed=True, **extra):
    return {"result_id": rid, "combination": "EMS/iOS ← Gmail/Android",
            "scenario_key": "priority_high", "scenario_type": "Yüksek Öncelikli Mesaj",
            "subject": f"[T] #{rid}", "received": True, "send_meta": {"msg_id": "<a@b>"},
            "manual_steps": ["Açılıyor mu?", "Ünlem görünüyor mu?"],
            "analysis": {"passed": passed, "confidence": "HIGH", "checks": [],
                         "summary": "özet", "issues": [], "recommendations": []},
            "test_time": "2026-11-13 10:00:00", **extra}


class TestManualVerify:

    def test_every_step_needs_a_status(self):
        with pytest.raises(ValueError, match="durum seçilmeli"):
            manual_verify.build_entry(_result(), [{"status": "pass"}, {"status": ""}])
        with pytest.raises(ValueError, match="2 adım bekleniyor"):
            manual_verify.build_entry(_result(), [{"status": "pass"}])

    def test_overall_is_fail_if_any_step_fails(self):
        entry = manual_verify.build_entry(
            _result(), [{"status": "pass"}, {"status": "fail", "note": "ünlem yok"}], "iOS 19")
        assert entry["overall"] == "fail" and entry["auto_status"] == "PASS"
        assert entry["steps"][1] == {"text": "Ünlem görünüyor mu?", "status": "fail",
                                     "note": "ünlem yok"}
        assert "ünlem yok" in manual_verify.entry_note(entry)

    def test_all_na_is_na(self):
        entry = manual_verify.build_entry(_result(), [{"status": "na"}, {"status": "na"}])
        assert entry["overall"] == "na"

    def test_agreement_counts_missed_and_false_alarm(self):
        results = [_result("a" * 8, True), _result("b" * 8, True),
                   _result("c" * 8, False), _result("d" * 8, True),
                   {**_result("e" * 8), "skipped": True}]
        ok = [{"status": "pass"}, {"status": "pass"}]
        bad = [{"status": "pass"}, {"status": "fail"}]
        store = {"a" * 8: manual_verify.build_entry(results[0], ok),
                 "b" * 8: manual_verify.build_entry(results[1], bad),    # araç kaçırdı
                 "c" * 8: manual_verify.build_entry(results[2], ok)}     # yanlış alarm
        stats = manual_verify.agreement(results, store)
        assert (stats["eligible"], stats["verified"], stats["pending"]) == (4, 3, 1)
        assert (stats["agree"], stats["false_pass"], stats["false_alarm"]) == (1, 1, 1)
        assert stats["agreement_rate"] == 33.3

    def test_store_roundtrip(self, tmp_path):
        path = manual_verify.store_path(tmp_path / "r" / "test_results.csv")
        entry = manual_verify.build_entry(_result(), [{"status": "pass"}, {"status": "na"}])
        manual_verify.save_entry(path, "abcd1234", entry)
        manual_verify.save_entry(path, "ffff0000", entry)
        assert set(manual_verify.load_store(path)) == {"abcd1234", "ffff0000"}
        path.write_text("{bozuk", encoding="utf-8")
        assert manual_verify.load_store(path) == {}

    def test_results_json_strips_private_fields(self, tmp_path):
        path = tmp_path / "test_results.json"
        r = _result(send_meta={"msg_id": "<a@b>", "expect": {"body_text": "x" * 9999},
                               "raw_bytes": b"\xff"})
        manual_verify.write_results_json([r], path, {"finished": True})
        data = manual_verify.load_results_json(path)
        assert data["run"] == {"finished": True}
        assert data["results"][0]["send_meta"] == {"msg_id": "<a@b>"}

    def test_cli_prompt_asks_status_and_note_per_step(self):
        answers = iter(["x", "g", "sorun yok", "K", "ünlem görünmüyor", "iPhone 16"])
        out: list = []
        entry = manual_verify.prompt_cli(_result(), lambda _p: next(answers), out.append)
        assert [s["status"] for s in entry["steps"]] == ["pass", "fail"]
        assert entry["steps"][1]["note"] == "ünlem görünmüyor"
        assert entry["note"] == "iPhone 16" and entry["overall"] == "fail"
        text = "\n".join(out)
        assert "#abcd1234" in text and "[2/2] Ünlem görünüyor mu?" in text
        assert "Lütfen g, k, u veya a girin." in text

    def test_cli_prompt_skip_item(self):
        answers = iter(["g", "", "a"])
        assert manual_verify.prompt_cli(_result(), lambda _p: next(answers),
                                        lambda _s: None) is None


class TestReportsWithManual:

    def test_csv_has_manual_columns(self, tmp_path):
        r1, r2 = _result("a" * 8, True), _result("b" * 8, True)
        manual = {"a" * 8: manual_verify.build_entry(
            r1, [{"status": "pass"}, {"status": "fail", "note": "=ünlem yok"}])}
        out = tmp_path / "r.csv"
        generate_csv_results([r1, r2], str(out), manual=manual)
        rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
        assert rows[0]["Run ID"] == "a" * 8
        assert rows[0]["Manuel Sonuç"] == "KALDI"
        assert rows[0]["Oto/Manuel Uyumu"] == "ARAÇ KAÇIRDI"
        assert "=ünlem yok" in rows[0]["Manuel Not"]
        assert rows[1]["Manuel Sonuç"] == "" and rows[1]["Oto/Manuel Uyumu"] == ""

    def test_html_shows_manual_verdict_and_escapes_notes(self, tmp_path):
        r = _result(passed=False)
        manual = {"abcd1234": manual_verify.build_entry(
            r, [{"status": "pass"}, {"status": "pass"}], "<script>alert(1)</script>")}
        out = tmp_path / "r.html"
        generate_html_report([r], str(out), manual=manual)
        html = out.read_text(encoding="utf-8")
        assert "👁 GEÇTİ" in html and "YANLIŞ ALARM" in html
        assert "<script>alert(1)</script>" not in html
        assert "Manuel Doğrulanan" in html

    def test_reports_without_manual_unchanged_in_shape(self, tmp_path):
        out = tmp_path / "r.html"
        generate_html_report([_result()], str(out))
        assert "Manuel Doğrulanan" not in out.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════
#  Web uç noktaları
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def seeded(flask_client, tmp_path):
    """Son koşunun sonuç dosyası: 2 doğrulanabilir + 1 atlanmış madde."""
    import app as app_module
    results = [_result("a" * 8, True), _result("b" * 8, False),
               {**_result("c" * 8), "skipped": True}]
    manual_verify.write_results_json(
        results, app_module.REPORTS_DIR / "test_results.json", {"finished": True, "note": ""})
    return flask_client, app_module


class TestManualEndpoints:

    def test_scenarios_endpoint(self, flask_client):
        data = flask_client.get("/api/scenarios").get_json()
        keys = {s["key"] for s in data["scenarios"]}
        assert {"plain_text", "priority_low", "enc_8bit"} <= keys
        assert {g["key"] for g in data["groups"]} >= {"temel", "kodlama", "oncelik"}

    def test_queue_lists_eligible_items(self, seeded):
        client, _ = seeded
        data = client.get("/api/manual/queue").get_json()
        assert [i["id"] for i in data["items"]] == ["a" * 8, "b" * 8]
        first = data["items"][0]
        assert first["manual"] is None and first["auto_status"] == "PASS"
        assert first["manual_steps"] == ["Açılıyor mu?", "Ünlem görünüyor mu?"]
        assert data["stats"]["pending"] == 2

    def test_queue_empty_without_results(self, flask_client):
        data = flask_client.get("/api/manual/queue").get_json()
        assert data["ok"] and data["items"] == []

    def test_submit_saves_and_regenerates_reports(self, seeded):
        client, app_module = seeded
        resp = client.post("/api/manual/submit", json={
            "id": "a" * 8, "note": "iOS 19.1",
            "steps": [{"status": "pass"}, {"status": "fail", "note": "ünlem yok"}]})
        data = resp.get_json()
        assert resp.status_code == 200 and data["entry"]["overall"] == "fail"
        assert data["stats"]["false_pass"] == 1 and data["stats"]["pending"] == 1
        rows = list(csv.DictReader(open(app_module.REPORTS_DIR / "test_results.csv",
                                        encoding="utf-8-sig")))
        assert rows[0]["Manuel Sonuç"] == "KALDI" and "ünlem yok" in rows[0]["Manuel Not"]
        assert "👁 KALDI" in (app_module.REPORTS_DIR / "test_report.html").read_text("utf-8")
        again = client.get("/api/manual/queue").get_json()
        assert again["items"][0]["manual"]["note"] == "iOS 19.1"

    def test_submit_requires_status_for_every_step(self, seeded):
        client, _ = seeded
        resp = client.post("/api/manual/submit", json={
            "id": "a" * 8, "steps": [{"status": "pass"}, {"status": "belki"}]})
        assert resp.status_code == 400 and "durum seçilmeli" in resp.get_json()["error"]

    @pytest.mark.parametrize("payload", [
        {"id": "yok00000", "steps": []},
        {"id": "c" * 8, "steps": [{"status": "pass"}, {"status": "pass"}]},   # atlanmış madde
    ])
    def test_submit_unknown_or_ineligible_item(self, seeded, payload):
        client, _ = seeded
        assert client.post("/api/manual/submit", json=payload).status_code == 404

    def test_submit_rejects_non_list_steps(self, seeded):
        client, _ = seeded
        assert client.post("/api/manual/submit",
                           json={"id": "a" * 8, "steps": "pass"}).status_code == 400

    def test_run_start_accepts_groups_and_lists(self, flask_client):
        import app as app_module
        proc = MagicMock()
        proc.stdout = iter(())
        proc.pid, proc.returncode = 4242, 0
        with patch.object(app_module.subprocess, "Popen", return_value=proc) as popen:
            resp = flask_client.post("/api/run/start",
                                     json={"scenario": "group:kodlama,priority_high"})
        assert resp.get_json()["ok"], resp.get_json()
        cmd = popen.call_args[0][0]
        assert cmd[cmd.index("--scenario") + 1] == "group:kodlama,priority_high"

    @pytest.mark.parametrize("bad", ["group:yok", "plain_text,olmayan", "--dry-run"])
    def test_run_start_rejects_unknown_filter(self, flask_client, bad):
        resp = flask_client.post("/api/run/start", json={"scenario": bad})
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════
#  Orkestratör: LLM'siz kip, sonuç JSON'u, --manual
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def cli_cfg(tmp_path, monkeypatch, full_config):
    import yaml
    monkeypatch.chdir(tmp_path)
    full_config["analysis"] = {"provider": "none"}
    full_config["test"].update({"large_attachment_mb": 0.05,
                                "test_attachment_path": "test_files/test_document.pdf",
                                "test_image_path": "test_files/test_image.png"})
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(full_config, allow_unicode=True), encoding="utf-8")
    return path, full_config


def _cli(argv, loop):
    with patch.object(sys, "argv", ["main.py", *argv]), \
         patch.object(MailSender, "_connect", side_effect=lambda: loop.smtp()), \
         patch.object(MailReceiver, "wait_for_message", autospec=True,
                      side_effect=lambda self, **kw: loop.wait_for_message(self, **kw)), \
         patch.object(main_mod.time, "sleep"):
        main_mod.main()


class TestOrchestratorIntegration:

    def test_llm_free_run_of_a_group(self, cli_cfg):
        path, cfg = cli_cfg
        _cli(["--config", str(path), "--combo", "0", "--scenario", "group:oncelik"], Loopback())
        data = manual_verify.load_results_json(
            manual_verify.results_json_path(cfg["test"]["results_csv"]))
        assert data["run"]["finished"] is True and data["run"]["analysis_provider"] == "none"
        assert [r["scenario_key"] for r in data["results"]] == [
            "priority_high", "priority_low", "read_receipt"]
        assert all(r["analysis"]["passed"] is True for r in data["results"])
        assert all(r["manual_steps"] and r["result_id"] for r in data["results"])

    def test_llm_free_legacy_scenario_says_content_not_checked(self, cli_cfg):
        path, cfg = cli_cfg
        _cli(["--config", str(path), "--combo", "0", "--scenario", "plain_text"], Loopback())
        rows = list(csv.DictReader(open(cfg["test"]["results_csv"], encoding="utf-8-sig")))
        assert rows[0]["Sonuç"] == "PASS" and rows[0]["Güven Seviyesi"] == "LOW"
        assert "içerik kontrolü YAPILMADI" in rows[0]["Özet"]

    def test_damage_makes_cli_exit_with_failures(self, cli_cfg):
        path, _ = cli_cfg
        with pytest.raises(SystemExit) as exc:
            _cli(["--config", str(path), "--combo", "0", "--scenario", "priority_high"],
                 Loopback(_drop_headers("X-Priority", "X-MSMail-Priority",
                                        "Importance", "Priority")))
        assert exc.value.code == main_mod.EXIT_TEST_FAILURES

    def test_manual_flag_prompts_after_each_scenario(self, cli_cfg):
        path, cfg = cli_cfg
        answers = iter(["g", "", "k", "ünlem yok", "g", "", "not"])
        with patch.object(sys.stdin, "isatty", return_value=True, create=True), \
             patch("builtins.input", side_effect=lambda _p="": next(answers)):
            _cli(["--config", str(path), "--combo", "0", "--scenario", "priority_high",
                  "--manual"], Loopback())
        store = manual_verify.load_store(manual_verify.store_path(cfg["test"]["results_csv"]))
        (entry,) = store.values()
        assert entry["overall"] == "fail" and entry["note"] == "not"
        rows = list(csv.DictReader(open(cfg["test"]["results_csv"], encoding="utf-8-sig")))
        assert rows[0]["Manuel Sonuç"] == "KALDI"
        assert rows[0]["Oto/Manuel Uyumu"] == "ARAÇ KAÇIRDI"

    def test_manual_flag_without_tty_does_not_block(self, cli_cfg, caplog):
        path, _ = cli_cfg
        with patch.object(sys.stdin, "isatty", return_value=False, create=True), \
             patch("builtins.input", side_effect=AssertionError("sorulmamalı")), \
             caplog.at_level("WARNING"):
            _cli(["--config", str(path), "--combo", "0", "--scenario", "priority_low",
                  "--manual"], Loopback())
        assert "etkileşimli terminal ister" in caplog.text
