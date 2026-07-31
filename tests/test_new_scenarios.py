"""
test_new_scenarios.py — Sonradan tamamlanan senaryoların testleri.

Kapsam: calendar_invite (iTIP/ICS), i18n, complex_html, html_table,
forward (message/rfc822) ve multi_attachment. Şablon → gönderim → analiz →
orkestratör zincirinin tamamı, gerçek ağ bağlantısı olmadan doğrulanır.
"""

import email
import re
import sys
from email import policy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as main_mod
from analyzer import MailAnalyzer
from csv_parser import SCENARIO_TYPE_MAP, SUPPORTED_SCENARIOS, parse_csv
from csv_parser import TestCombination as Combination
from message_templates import (
    ALL_TEMPLATES,
    HTML_SCENARIOS,
    get_template,
    html_to_plain,
    resolve_html,
)
from sender import (
    MailSender,
    _build_ics,
    _detect_scripts,
    _ics_escape,
    _ics_fold,
)

NEW_SCENARIOS = ["calendar_invite", "i18n", "complex_html",
                 "html_table", "forward", "multi_attachment"]


# ── Yardımcılar ──────────────────────────────────────────────────────

@pytest.fixture
def capture_smtp():
    """SMTP'yi mock'lar ve gönderilen ham mesajları biriktirir."""
    sent = []
    smtp = MagicMock()
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)
    smtp.sendmail = lambda frm, to, msg: sent.append(msg)
    with patch("smtplib.SMTP", return_value=smtp):
        yield sent


@pytest.fixture
def sender(server_cfg):
    return MailSender({**server_cfg, "test_address": "gonderen@test.local"})


def parse_last(sent) -> email.message.Message:
    return email.message_from_bytes(sent[-1], policy=policy.default)


def content_types(msg) -> list[str]:
    return [p.get_content_type() for p in msg.walk()]


def find_part(msg, content_type):
    return next((p for p in msg.walk() if p.get_content_type() == content_type), None)


def unfold_ics(ics: str) -> list[str]:
    """RFC 5545 katlanmış satırları geri birleştirir."""
    return ics.replace("\r\n ", "").strip().split("\r\n")


# ═══════════════════════════════════════════════════════════════════
#  Şablon bütünlüğü
# ═══════════════════════════════════════════════════════════════════

class TestTemplateIntegrity:

    def test_every_supported_scenario_has_templates(self):
        # smime şablon kullanmaz (gövde doğrudan üretilir)
        needs_template = SUPPORTED_SCENARIOS - {"smime"}
        assert needs_template <= set(ALL_TEMPLATES), (
            f"Şablonsuz senaryolar: {sorted(needs_template - set(ALL_TEMPLATES))}"
        )

    def test_complex_html_has_no_cid_placeholder(self):
        """Regresyon: complex_html eskiden inline_image şablonlarını yeniden
        kullanıyordu; o şablonlardaki {{CID}} resimsiz mesajda çözümlenmeden
        kalıyordu."""
        for index in range(3):
            body = get_template("complex_html", index).body
            assert "{{CID}}" not in body
            assert "cid:" not in body

    def test_complex_html_differs_from_inline_image(self):
        assert ALL_TEMPLATES["complex_html"] is not ALL_TEMPLATES["inline_image"]

    def test_multi_attachment_differs_from_attachment(self):
        assert ALL_TEMPLATES["multi_attachment"] is not ALL_TEMPLATES["attachment"]

    def test_forward_alias_points_to_same_templates(self):
        assert ALL_TEMPLATES["forward"] is ALL_TEMPLATES["forward_chain"]

    @pytest.mark.parametrize("scenario", NEW_SCENARIOS)
    def test_rotation_covers_three_lengths(self, scenario):
        lengths = {get_template(scenario, i).length for i in range(3)}
        assert lengths == {"short", "medium", "long"}

    @pytest.mark.parametrize("scenario", sorted(HTML_SCENARIOS))
    def test_html_templates_resolve_all_placeholders(self, scenario):
        for index in range(3):
            resolved = resolve_html(get_template(scenario, index).body)
            assert "{{" not in resolved, f"{scenario}[{index}] yer tutucu kaldı"
            assert "</html>" in resolved.lower()

    def test_complex_html_contains_media_query(self):
        for index in range(3):
            assert "@media" in get_template("complex_html", index).body

    def test_html_table_contains_table_markup(self):
        for index in range(3):
            body = get_template("html_table", index).body
            assert "<table" in body and "</table>" in body

    def test_i18n_templates_carry_multiple_scripts(self):
        # Orta ve uzun şablonlar Latin dışı alfabe içermeli
        for index in (1, 2):
            scripts = _detect_scripts(get_template("i18n", index).body)
            assert "arabic" in scripts and "cjk" in scripts and "emoji" in scripts

    def test_i18n_subject_tags_are_non_ascii(self):
        for index in range(3):
            assert not get_template("i18n", index).subject_tag.isascii()


class TestHtmlToPlain:

    def test_strips_tags_and_keeps_text(self):
        out = html_to_plain("<html><body><p>Merhaba</p><p>ğüşıöç</p></body></html>")
        assert out == "Merhaba\nğüşıöç"

    def test_removes_style_and_script_blocks(self):
        html = "<style>.a{color:red}</style><script>alert(1)</script><p>Metin</p>"
        out = html_to_plain(html)
        assert "color:red" not in out and "alert" not in out
        assert "Metin" in out

    def test_unescapes_entities(self):
        assert "<style>" in html_to_plain("<p>&lt;style&gt; bloğu</p>")

    def test_table_rows_become_lines(self):
        out = html_to_plain("<table><tr><td>bir</td></tr><tr><td>iki</td></tr></table>")
        assert "bir" in out and "iki" in out

    def test_br_becomes_newline(self):
        assert html_to_plain("<p>a<br/>b</p>") == "a\nb"

    def test_blank_input_returns_empty(self):
        assert html_to_plain("") == ""

    @pytest.mark.parametrize("scenario", sorted(HTML_SCENARIOS))
    def test_every_html_template_yields_readable_fallback(self, scenario):
        for index in range(3):
            plain = html_to_plain(resolve_html(get_template(scenario, index).body))
            assert len(plain) > 40
            # Metnin kendisi "<style> bloğu" gibi ifadeler içerebilir; kalmaması
            # gereken şey HTML ETİKETLERİ ve kapanış etiketleridir.
            assert "</" not in plain
            assert not re.search(r"<(p|div|table|tr|td|html|body|span)\b", plain)


# ═══════════════════════════════════════════════════════════════════
#  iCalendar üretimi (RFC 5545)
# ═══════════════════════════════════════════════════════════════════

class TestIcsBuilder:

    def test_escapes_special_characters(self):
        assert _ics_escape("a;b") == r"a\;b"
        assert _ics_escape("a,b") == r"a\,b"
        assert _ics_escape("a\\b") == r"a\\b"
        assert _ics_escape("a\nb") == r"a\nb"
        assert _ics_escape("a\r\nb") == r"a\nb"

    def test_backslash_escaped_before_others(self):
        # Önce \ kaçışlanmazsa \; çift kaçış hatası oluşur
        assert _ics_escape("a\\;b") == r"a\\\;b"

    def test_short_line_not_folded(self):
        assert "\r\n" not in _ics_fold("SUMMARY:kısa")

    def test_long_line_folded_under_75_octets(self):
        folded = _ics_fold("DESCRIPTION:" + "x" * 300)
        for line in folded.split("\r\n"):
            assert len(line.encode("utf-8")) <= 75

    def test_folding_does_not_split_multibyte_chars(self):
        folded = _ics_fold("SUMMARY:" + "ğüşıöç" * 40)
        # Bozuk kesim olsaydı decode sırasında hata alırdık
        rejoined = folded.replace("\r\n ", "")
        assert rejoined == "SUMMARY:" + "ğüşıöç" * 40

    def test_continuation_lines_start_with_space(self):
        folded = _ics_fold("DESCRIPTION:" + "y" * 200)
        for line in folded.split("\r\n")[1:]:
            assert line.startswith(" ")

    def test_emoji_survives_folding(self):
        folded = _ics_fold("SUMMARY:" + "🚀" * 60)
        assert folded.replace("\r\n ", "") == "SUMMARY:" + "🚀" * 60

    def _ics(self, **over):
        from datetime import datetime
        params = dict(
            uid="uid-1@test", summary="Toplantı", description="Açıklama",
            location="Oda 3", start=datetime(2026, 8, 1, 9, 0),
            end=datetime(2026, 8, 1, 9, 30),
            organizer="org@test.local", attendee="kat@test.local",
        )
        params.update(over)
        return _build_ics(**params)

    def test_structure_is_wellformed(self):
        lines = unfold_ics(self._ics())
        assert lines[0] == "BEGIN:VCALENDAR"
        assert lines[-1] == "END:VCALENDAR"
        assert "BEGIN:VEVENT" in lines and "END:VEVENT" in lines
        assert lines.index("BEGIN:VEVENT") < lines.index("END:VEVENT")

    def test_method_is_request(self):
        assert "METHOD:REQUEST" in unfold_ics(self._ics())

    def test_required_properties_present(self):
        lines = unfold_ics(self._ics())
        keys = {line.split(":", 1)[0].split(";", 1)[0] for line in lines}
        for required in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY",
                         "ORGANIZER", "ATTENDEE", "SEQUENCE", "STATUS"):
            assert required in keys, f"{required} eksik"

    def test_uid_preserved(self):
        assert "UID:benzersiz-42@test" in unfold_ics(self._ics(uid="benzersiz-42@test"))

    def test_datetime_format(self):
        lines = unfold_ics(self._ics())
        start = next(l for l in lines if l.startswith("DTSTART"))
        assert start == "DTSTART:20260801T090000"
        stamp = next(l for l in lines if l.startswith("DTSTAMP"))
        assert re.fullmatch(r"DTSTAMP:\d{8}T\d{6}Z", stamp)

    def test_attendee_has_rsvp(self):
        attendee = next(l for l in unfold_ics(self._ics()) if l.startswith("ATTENDEE"))
        assert "RSVP=TRUE" in attendee
        assert "mailto:kat@test.local" in attendee

    def test_crlf_line_endings(self):
        ics = self._ics()
        assert ics.endswith("\r\n")
        assert "\n" not in ics.replace("\r\n", "")

    def test_special_chars_in_summary_escaped(self):
        lines = unfold_ics(self._ics(summary="Toplantı; acil, önemli"))
        summary = next(l for l in lines if l.startswith("SUMMARY"))
        assert r"\;" in summary and r"\," in summary


class TestScriptDetection:

    @pytest.mark.parametrize("text,expected", [
        ("ğüşıöç", "latin_extended"),
        ("﷽", "arabic"),
        ("مرحبا", "arabic"),
        ("漢字", "cjk"),
        ("ひらがな", "cjk"),
        ("🚀", "emoji"),
        ("Привет", "cyrillic"),
        ("Ελλάδα", "greek"),
    ])
    def test_detects_script(self, text, expected):
        assert expected in _detect_scripts(text)

    def test_plain_ascii_detects_nothing(self):
        assert _detect_scripts("hello world 123") == []

    def test_multiple_scripts_detected(self):
        found = _detect_scripts("ğ ﷽ 漢 🚀")
        assert set(found) >= {"latin_extended", "arabic", "cjk", "emoji"}


# ═══════════════════════════════════════════════════════════════════
#  Gönderim — zengin HTML
# ═══════════════════════════════════════════════════════════════════

class TestSendHtmlMessage:

    def test_multipart_alternative_structure(self, sender, capture_smtp):
        sender.send_html_message("a@t.local", "Konu", "<html><body><p>x</p></body></html>",
                                 "x", scenario="complex_html")
        msg = parse_last(capture_smtp)
        assert msg.get_content_type() == "multipart/alternative"
        assert content_types(msg) == ["multipart/alternative", "text/plain", "text/html"]

    def test_plain_leg_comes_before_html(self, sender, capture_smtp):
        """RFC 2046: istemci en sondaki desteklenen bacağı gösterir, bu yüzden
        HTML sonda olmalı."""
        sender.send_html_message("a@t.local", "K", "<p>zengin</p>", "yalın")
        parts = [p for p in parse_last(capture_smtp).walk() if not p.is_multipart()]
        assert parts[0].get_content_type() == "text/plain"
        assert parts[1].get_content_type() == "text/html"

    def test_turkish_chars_preserved_in_both_legs(self, sender, capture_smtp):
        sender.send_html_message("a@t.local", "K", "<p>ğüşıöç</p>", "ğüşıöç")
        msg = parse_last(capture_smtp)
        for ctype in ("text/plain", "text/html"):
            assert "ğüşıöç" in find_part(msg, ctype).get_content()

    def test_metadata_reports_scenario_and_fallback(self, sender, capture_smtp):
        meta = sender.send_html_message("a@t.local", "K", "<p>x</p>", "x",
                                        scenario="html_table")
        assert meta["scenario"] == "html_table"
        assert meta["has_plain_fallback"] is True
        assert meta["html_length"] == len("<p>x</p>")
        assert meta["msg_id"]

    def test_empty_fallback_flagged(self, sender, capture_smtp):
        meta = sender.send_html_message("a@t.local", "K", "<p>x</p>", "   ")
        assert meta["has_plain_fallback"] is False

    def test_media_query_survives_serialization(self, sender, capture_smtp):
        html = resolve_html(get_template("complex_html", 0).body)
        sender.send_html_message("a@t.local", "K", html, html_to_plain(html))
        received = find_part(parse_last(capture_smtp), "text/html").get_content()
        assert "@media" in received


# ═══════════════════════════════════════════════════════════════════
#  Gönderim — i18n
# ═══════════════════════════════════════════════════════════════════

class TestSendI18n:

    def test_subject_rfc2047_encoded(self, sender, capture_smtp):
        sender.send_i18n("a@t.local", "🌍 ÖÇŞĞÜİ 漢字", "gövde")
        raw = capture_smtp[-1]
        subject_line = next(l for l in raw.split(b"\n") if l.startswith(b"Subject:"))
        assert subject_line.startswith(b"Subject: =?")
        assert b"utf-8" in subject_line.lower()

    def test_subject_decodes_back_to_original(self, sender, capture_smtp):
        original = "🌍 Test ÖÇŞĞÜİ 漢字 🚀"
        sender.send_i18n("a@t.local", original, "gövde")
        assert parse_last(capture_smtp)["Subject"] == original

    def test_body_preserved_across_scripts(self, sender, capture_smtp):
        body = "Arapça: ﷽\nAsya: 漢字\nEmoji: 🚀🔥\nTürkçe: ğüşıöç"
        sender.send_i18n("a@t.local", "K", body)
        assert parse_last(capture_smtp).get_content().strip() == body

    def test_charset_is_utf8(self, sender, capture_smtp):
        sender.send_i18n("a@t.local", "K", "ğüşıöç")
        assert parse_last(capture_smtp).get_content_charset() == "utf-8"

    def test_metadata_reports_scripts(self, sender, capture_smtp):
        meta = sender.send_i18n("a@t.local", "🌍", "﷽ 漢字 🚀 ğüşıöç")
        assert meta["scenario"] == "i18n"
        assert meta["subject_is_ascii"] is False
        assert set(meta["body_charsets"]) >= {"latin_extended", "arabic", "cjk", "emoji"}

    def test_ascii_subject_flagged(self, sender, capture_smtp):
        meta = sender.send_i18n("a@t.local", "Plain Subject", "gövde")
        assert meta["subject_is_ascii"] is True


# ═══════════════════════════════════════════════════════════════════
#  Gönderim — takvim daveti
# ═══════════════════════════════════════════════════════════════════

class TestSendCalendarInvite:

    def test_mime_structure(self, sender, capture_smtp):
        sender.send_calendar_invite("a@t.local", "Konu", "Açıklama", summary="Toplantı")
        types = content_types(parse_last(capture_smtp))
        assert types == ["multipart/mixed", "multipart/alternative",
                         "text/plain", "text/calendar", "application/ics"]

    def test_calendar_part_declares_request_method(self, sender, capture_smtp):
        sender.send_calendar_invite("a@t.local", "K", "A", summary="T")
        cal = find_part(parse_last(capture_smtp), "text/calendar")
        assert cal.get_param("method") == "REQUEST"

    def test_ics_attachment_named_invite(self, sender, capture_smtp):
        sender.send_calendar_invite("a@t.local", "K", "A", summary="T")
        ics_part = find_part(parse_last(capture_smtp), "application/ics")
        assert ics_part.get_filename() == "invite.ics"

    def test_attachment_and_body_carry_same_uid(self, sender, capture_smtp):
        meta = sender.send_calendar_invite("a@t.local", "K", "A", summary="T")
        msg = parse_last(capture_smtp)
        body_ics = find_part(msg, "text/calendar").get_content()
        attached = find_part(msg, "application/ics").get_payload(decode=True).decode()
        assert meta["ics_uid"] in body_ics
        assert meta["ics_uid"] in attached

    def test_summary_reaches_ics(self, sender, capture_smtp):
        sender.send_calendar_invite("a@t.local", "K", "A", summary="Haftalık Senkron ğüşıöç")
        ics = find_part(parse_last(capture_smtp), "text/calendar").get_content()
        assert "Haftalık Senkron ğüşıöç" in "".join(unfold_ics(ics))

    def test_duration_reflected_in_dtend(self, sender, capture_smtp):
        from datetime import datetime
        sender.send_calendar_invite("a@t.local", "K", "A", summary="T",
                                    start=datetime(2026, 8, 1, 10, 0),
                                    duration_minutes=90)
        lines = unfold_ics(find_part(parse_last(capture_smtp), "text/calendar").get_content())
        assert "DTSTART:20260801T100000" in lines
        assert "DTEND:20260801T113000" in lines

    def test_custom_location(self, sender, capture_smtp):
        sender.send_calendar_invite("a@t.local", "K", "A", summary="T",
                                    location="Ankara Toplantı Salonu")
        ics = find_part(parse_last(capture_smtp), "text/calendar").get_content()
        assert "Ankara Toplantı Salonu" in "".join(unfold_ics(ics))

    def test_organizer_and_attendee_addresses(self, sender, capture_smtp):
        sender.send_calendar_invite("alici@t.local", "K", "A", summary="T")
        ics = "".join(unfold_ics(find_part(parse_last(capture_smtp), "text/calendar").get_content()))
        assert "mailto:gonderen@test.local" in ics
        assert "mailto:alici@t.local" in ics

    def test_default_start_is_in_future(self, sender, capture_smtp):
        from datetime import datetime
        meta = sender.send_calendar_invite("a@t.local", "K", "A", summary="T")
        start = datetime.strptime(meta["event_start"], "%Y-%m-%d %H:%M")
        assert start > datetime.now()

    def test_each_invite_gets_unique_uid(self, sender, capture_smtp):
        uids = {sender.send_calendar_invite("a@t.local", "K", "A", summary="T")["ics_uid"]
                for _ in range(3)}
        assert len(uids) == 3

    def test_description_with_special_chars_escaped(self, sender, capture_smtp):
        sender.send_calendar_invite("a@t.local", "K", "Açıklama; virgül, ters\\bölü",
                                    summary="T")
        ics = find_part(parse_last(capture_smtp), "text/calendar").get_content()
        desc = next(l for l in unfold_ics(ics) if l.startswith("DESCRIPTION"))
        assert r"\;" in desc and r"\," in desc and r"\\" in desc


# ═══════════════════════════════════════════════════════════════════
#  Gönderim — forward
# ═══════════════════════════════════════════════════════════════════

ORIGINAL = {
    "subject": "Orijinal Konu ğüşıöç",
    "from": "ilk@test.local",
    "to": "gonderen@test.local",
    "date": "Mon, 27 Jul 2026 10:00:00 +0300",
    "msg_id": "<orijinal@test.local>",
    "body": "Orijinal gövde ğüşıöç",
}


class TestSendForward:

    def test_mime_structure(self, sender, capture_smtp):
        sender.send_forward("a@t.local", "Konu", "İletiyorum.", ORIGINAL)
        types = content_types(parse_last(capture_smtp))
        assert types[:3] == ["multipart/mixed", "text/plain", "message/rfc822"]

    def test_subject_gets_fwd_prefix(self, sender, capture_smtp):
        sender.send_forward("a@t.local", "Protokol Bilgilendirme", "x", ORIGINAL)
        assert parse_last(capture_smtp)["Subject"] == "Fwd: Protokol Bilgilendirme"

    def test_existing_fwd_prefix_not_duplicated(self, sender, capture_smtp):
        sender.send_forward("a@t.local", "Fwd: Zaten İletilmiş", "x", ORIGINAL)
        assert parse_last(capture_smtp)["Subject"] == "Fwd: Zaten İletilmiş"

    def test_fwd_prefix_case_insensitive(self, sender, capture_smtp):
        sender.send_forward("a@t.local", "FWD: Büyük Harf", "x", ORIGINAL)
        assert parse_last(capture_smtp)["Subject"] == "FWD: Büyük Harf"

    def test_encapsulated_message_keeps_headers(self, sender, capture_smtp):
        sender.send_forward("a@t.local", "K", "x", ORIGINAL)
        rfc822 = find_part(parse_last(capture_smtp), "message/rfc822")
        inner = rfc822.get_payload()[0]
        assert inner["Message-ID"] == ORIGINAL["msg_id"]
        assert inner["Subject"] == ORIGINAL["subject"]
        assert inner["From"] == ORIGINAL["from"]
        assert inner["Date"] == ORIGINAL["date"]

    def test_encapsulated_body_preserved(self, sender, capture_smtp):
        sender.send_forward("a@t.local", "K", "x", ORIGINAL)
        inner = find_part(parse_last(capture_smtp), "message/rfc822").get_payload()[0]
        assert ORIGINAL["body"] in inner.get_content()

    def test_visible_header_block_in_body(self, sender, capture_smtp):
        """message/rfc822 ekini açamayan istemcide bilgi kaybolmamalı."""
        sender.send_forward("a@t.local", "K", "İlginize.", ORIGINAL)
        body = find_part(parse_last(capture_smtp), "text/plain").get_content()
        assert "İletilen Mesaj" in body
        assert ORIGINAL["from"] in body
        assert ORIGINAL["subject"] in body
        assert ORIGINAL["date"] in body
        assert "İlginize." in body

    def test_attachment_filename(self, sender, capture_smtp):
        sender.send_forward("a@t.local", "K", "x", ORIGINAL)
        rfc822 = find_part(parse_last(capture_smtp), "message/rfc822")
        assert rfc822.get_filename() == "iletilen_mesaj.eml"

    def test_metadata(self, sender, capture_smtp):
        meta = sender.send_forward("a@t.local", "K", "x", ORIGINAL)
        assert meta["scenario"] == "forward"
        assert meta["original_msg_id"] == ORIGINAL["msg_id"]
        assert meta["original_subject"] == ORIGINAL["subject"]

    def test_missing_original_fields_tolerated(self, sender, capture_smtp):
        meta = sender.send_forward("a@t.local", "K", "x", {"body": "yalnız gövde"})
        assert meta["original_msg_id"] == ""
        inner = find_part(parse_last(capture_smtp), "message/rfc822").get_payload()[0]
        assert "yalnız gövde" in inner.get_content()


class TestGuessMimeNewTypes:

    @pytest.mark.parametrize("filename,expected", [
        ("veri.csv", "text/csv"),
        ("davet.ics", "text/calendar"),
        ("mesaj.eml", "message/rfc822"),
        ("resim.gif", "image/gif"),
    ])
    def test_new_extensions(self, filename, expected):
        assert MailSender._guess_mime(filename) == expected


# ═══════════════════════════════════════════════════════════════════
#  Orkestratör yönlendirmesi
# ═══════════════════════════════════════════════════════════════════

def _combo() -> Combination:
    return Combination(label="EMS/iOS ← Gmail/Android", receiver_server="EMS",
                       receiver_client="iOS", sender_server="Gmail",
                       sender_client="Android")


def _mocks():
    s = MagicMock()
    for name, scenario in (
        ("send_plain_text", "plain_text"), ("send_with_attachment", "attachment"),
        ("send_html_message", "complex_html"), ("send_i18n", "i18n"),
        ("send_calendar_invite", "calendar_invite"), ("send_forward", "forward"),
    ):
        getattr(s, name).return_value = {"msg_id": f"<{scenario}@t>", "sent_at": 1.0,
                                         "scenario": scenario}
    s.from_address = "gonderen@test.local"
    r = MagicMock()
    r.config = {"test_address": "alici@test.local"}
    r.wait_for_message.return_value = {"headers": {}}
    a = MagicMock()
    a.analyze.return_value = {"passed": True, "confidence": "HIGH", "checks": [],
                              "summary": "ok", "issues": [], "recommendations": []}
    return s, r, a


@pytest.fixture
def run_cfg(tmp_path):
    pdf = tmp_path / "belge.pdf"; pdf.write_bytes(b"%PDF-1.4" + b"x" * 100)
    csvf = tmp_path / "veri.csv"; csvf.write_text("a,b\n1,2\n", encoding="utf-8")
    png = tmp_path / "img.png"; png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40)
    return {
        "subject_prefix": "[AUTO-TEST]", "wait_seconds": 0, "max_retries": 1,
        "retry_interval": 0, "test_image_path": str(png),
        "test_attachment_paths": [str(pdf)],
        "test_multi_attachment_paths": [str(pdf), str(csvf)],
    }


def _run(scenario, cfg, index=0):
    s, r, a = _mocks()
    with patch.object(main_mod.time, "sleep"):
        result = main_mod.run_scenario(scenario, _combo(), index, s, r, a, cfg)
    return s, result


class TestOrchestratorDispatch:

    def test_calendar_invite_dispatch(self, run_cfg):
        s, res = _run("calendar_invite", run_cfg)
        s.send_calendar_invite.assert_called_once()
        assert res["scenario_key"] == "calendar_invite"
        assert s.send_calendar_invite.call_args.kwargs["summary"]

    def test_calendar_config_overrides(self, run_cfg):
        cfg = {**run_cfg, "calendar_duration_minutes": 60,
               "calendar_location": "Ankara Salon"}
        s, _ = _run("calendar_invite", cfg)
        kwargs = s.send_calendar_invite.call_args.kwargs
        assert kwargs["duration_minutes"] == 60
        assert kwargs["location"] == "Ankara Salon"

    def test_i18n_dispatch(self, run_cfg):
        s, res = _run("i18n", run_cfg)
        s.send_i18n.assert_called_once()
        assert res["scenario_key"] == "i18n"

    def test_i18n_subject_carries_non_ascii(self, run_cfg):
        """i18n'in amacı başlık kodlamasını sınamak — konu ASCII kalmamalı."""
        s, _ = _run("i18n", run_cfg)
        subject = s.send_i18n.call_args[0][1]
        assert not subject.isascii()

    @pytest.mark.parametrize("scenario", ["complex_html", "html_table"])
    def test_html_scenarios_dispatch(self, run_cfg, scenario):
        s, res = _run(scenario, run_cfg)
        s.send_html_message.assert_called_once()
        assert s.send_html_message.call_args.kwargs["scenario"] == scenario
        assert res["scenario_key"] == scenario

    @pytest.mark.parametrize("scenario", ["complex_html", "html_table"])
    def test_html_body_has_no_placeholders(self, run_cfg, scenario):
        s, _ = _run(scenario, run_cfg)
        html = s.send_html_message.call_args[0][2]
        assert "{{" not in html

    @pytest.mark.parametrize("scenario", ["complex_html", "html_table"])
    def test_html_plain_fallback_generated(self, run_cfg, scenario):
        s, _ = _run(scenario, run_cfg)
        plain = s.send_html_message.call_args[0][3]
        assert len(plain) > 40
        assert "</" not in plain

    @pytest.mark.parametrize("scenario", ["complex_html", "html_table"])
    def test_run_id_stamped_inside_body(self, run_cfg, scenario):
        """Run ID </body> içinde olmalı; </html>'den sonraki metin bazı
        istemcilerde görünmez."""
        s, _ = _run(scenario, run_cfg)
        html = s.send_html_message.call_args[0][2]
        assert "[Run ID:" in html
        assert html.lower().index("[run id:") < html.lower().index("</body>")

    def test_forward_sends_original_then_forwards(self, run_cfg):
        s, res = _run("forward", run_cfg)
        s.send_plain_text.assert_called_once()
        s.send_forward.assert_called_once()
        assert res["scenario_key"] == "forward"

    def test_forward_encapsulates_real_original(self, run_cfg):
        s, _ = _run("forward", run_cfg)
        original = s.send_forward.call_args[0][3]
        assert original["msg_id"] == "<plain_text@t>"      # gerçekten gönderilen mesaj
        assert original["from"] == "gonderen@test.local"
        assert original["to"] == "alici@test.local"
        assert original["subject"] and original["date"] and original["body"]

    def test_forward_original_subject_marked(self, run_cfg):
        s, _ = _run("forward", run_cfg)
        assert "İletme Kaynağı" in s.send_plain_text.call_args[0][1]

    def test_forward_chain_alias_dispatches(self, run_cfg):
        s, res = _run("forward_chain", run_cfg)
        s.send_forward.assert_called_once()
        assert res["scenario_key"] == "forward_chain"

    def test_multi_attachment_sends_all_files(self, run_cfg):
        s, res = _run("multi_attachment", run_cfg)
        paths = s.send_with_attachment.call_args[0][3]
        assert len(paths) == 2
        assert res["scenario_key"] == "multi_attachment"

    def test_multi_attachment_overrides_scenario_label(self, run_cfg):
        s, res = _run("multi_attachment", run_cfg)
        assert res["send_meta"]["scenario"] == "multi_attachment"

    def test_multi_attachment_skips_when_no_files(self, run_cfg):
        cfg = {**run_cfg, "test_multi_attachment_paths": ["/yok/a.pdf", "/yok/b.csv"]}
        s, res = _run("multi_attachment", cfg)
        s.send_with_attachment.assert_not_called()
        assert res["skipped"] is True
        assert res["analysis"]["passed"] is None

    def test_multi_attachment_falls_back_to_generated_files(self, run_cfg, monkeypatch,
                                                            tmp_path):
        monkeypatch.chdir(tmp_path)
        main_mod.prepare_test_files()
        monkeypatch.setattr(main_mod, "__file__", str(tmp_path / "main.py"))
        cfg = {k: v for k, v in run_cfg.items()
               if k not in ("test_multi_attachment_paths", "test_attachment_paths")}
        cfg["test_attachment_path"] = "test_files/test_document.pdf"
        s, _ = _run("multi_attachment", cfg)
        paths = s.send_with_attachment.call_args[0][3]
        assert len(paths) >= 3            # pdf + csv + txt
        assert any(p.endswith(".csv") for p in paths)
        assert any(p.endswith(".txt") for p in paths)


class TestNewScenarioSubjects:

    def _subject(self, scenario, cfg, method):
        s, _ = _run(scenario, cfg)
        return getattr(s, method).call_args[0][1]

    def test_calendar_subject_tag(self, run_cfg):
        subject = self._subject("calendar_invite", run_cfg, "send_calendar_invite")
        assert "Senaryo: Takvim Daveti (iTIP Daveti, ICS Ekli)" in subject

    def test_complex_html_subject_tag(self, run_cfg):
        subject = self._subject("complex_html", run_cfg, "send_html_message")
        assert "Senaryo: Zengin HTML (Zengin CSS + Media Query)" in subject

    def test_html_table_subject_tag(self, run_cfg):
        subject = self._subject("html_table", run_cfg, "send_html_message")
        assert "Senaryo: HTML Tablo (HTML Tablo Yapısı)" in subject

    def test_forward_carries_original_subject(self, run_cfg):
        """reply_chain ile aynı desen: iletide konu, thread'in bozulmaması için
        orijinal mesajın konusudur; 'Fwd:' önekini sender ekler."""
        s, _ = _run("forward", run_cfg)
        forwarded_subject = s.send_forward.call_args[0][1]
        original_subject = s.send_plain_text.call_args[0][1]
        assert "İletme Kaynağı" in forwarded_subject
        assert forwarded_subject == original_subject

    def test_multi_attachment_subject_lists_files(self, run_cfg):
        subject = self._subject("multi_attachment", run_cfg, "send_with_attachment")
        assert "Senaryo: Çoklu Ek (2 Ek: PDF+CSV," in subject

    def test_i18n_subject_uses_template_tag(self, run_cfg):
        subject = self._subject("i18n", run_cfg, "send_i18n")
        assert get_template("i18n", 0).subject_tag in subject

    @pytest.mark.parametrize("scenario,method", [
        ("calendar_invite", "send_calendar_invite"), ("i18n", "send_i18n"),
        ("complex_html", "send_html_message"), ("html_table", "send_html_message"),
        ("multi_attachment", "send_with_attachment"),
    ])
    def test_subject_has_run_id_and_combo(self, run_cfg, scenario, method):
        subject = self._subject(scenario, run_cfg, method)
        assert subject.startswith("[AUTO-TEST] #")
        assert subject.endswith("EMS/iOS ← Gmail/Android")


class TestHtmlRunIdStamp:

    def test_inserted_before_closing_body(self):
        html = "<html><body><p>içerik</p></body></html>"
        out = main_mod._stamp_html_run_id(html, "abc123")
        assert out == "<html><body><p>içerik</p><p style=\"font-size:11px;color:#999;margin-top:16px\">[Run ID: abc123]</p></body></html>"

    def test_appended_when_no_body_tag(self):
        out = main_mod._stamp_html_run_id("<p>parça</p>", "xyz")
        assert out.startswith("<p>parça</p>")
        assert "xyz" in out

    def test_uppercase_body_tag_handled(self):
        out = main_mod._stamp_html_run_id("<HTML><BODY>x</BODY></HTML>", "id1")
        assert out.index("id1") < out.index("</BODY>")

    def test_uses_last_body_tag(self):
        out = main_mod._stamp_html_run_id("<body>a</body><body>b</body>", "r1")
        assert out.rindex("r1") < out.rindex("</body>")

    def test_turkish_dotted_i_does_not_shift_index(self):
        """Regresyon: 'İ'.lower() iki karaktere ('i' + U+0307) açılır. Konum
        araması küçük harfli kopya üzerinde yapılırsa indeks kayar ve kapanış
        etiketinin '<' karakteri yenirdi (…</p>/body></html>)."""
        html = "<html><body><p>ĞÜŞİÖÇ İstanbul İzmir</p></body></html>"
        out = main_mod._stamp_html_run_id(html, "abc")
        assert "</body>" in out
        assert "/body>" not in out.replace("</body>", "")
        assert out.endswith("</body></html>")
        assert out.count("<body>") == 1

    @pytest.mark.parametrize("scenario", sorted(HTML_SCENARIOS))
    def test_real_templates_keep_closing_tags(self, scenario):
        for index in range(3):
            html = resolve_html(get_template(scenario, index).body)
            out = main_mod._stamp_html_run_id(html, "run1")
            assert out.endswith("</body></html>")
            assert out.count("</body>") == html.count("</body>")

    def test_spaced_closing_tag_matched(self):
        out = main_mod._stamp_html_run_id("<body>x</body >", "r2")
        assert out.index("r2") < out.index("</body >")


# ═══════════════════════════════════════════════════════════════════
#  Analiz kontrol noktaları
# ═══════════════════════════════════════════════════════════════════

class TestAnalyzerPromptsForNewScenarios:

    @pytest.fixture
    def analyzer(self):
        with patch("analyzer.anthropic.Anthropic"):
            return MailAnalyzer("sk-ant-test")

    @pytest.mark.parametrize("scenario", SUPPORTED_SCENARIOS - {"forward_chain"})
    def test_every_scenario_has_dedicated_checks(self, analyzer, scenario,
                                                 received_msg, combination_meta):
        prompt = analyzer._build_prompt(scenario, {"msg_id": "<a@b>"},
                                        received_msg, combination_meta)
        assert "Genel mesaj iletim kontrolü yap." not in prompt, (
            f"{scenario} için özel kontrol listesi tanımlanmamış"
        )
        assert "Kontrol edilecekler:" in prompt

    def test_calendar_prompt_mentions_itip(self, analyzer, received_msg, combination_meta):
        prompt = analyzer._build_prompt(
            "calendar_invite", {"ics_uid": "uid-9", "ics_method": "REQUEST"},
            received_msg, combination_meta)
        assert "text/calendar" in prompt
        assert "METHOD=REQUEST" in prompt
        assert "uid-9" in prompt

    def test_i18n_prompt_lists_scripts(self, analyzer, received_msg, combination_meta):
        prompt = analyzer._build_prompt(
            "i18n", {"body_charsets": ["arabic", "cjk"], "subject_is_ascii": False},
            received_msg, combination_meta)
        assert "RFC 2047" in prompt
        assert "arabic" in prompt

    def test_complex_html_prompt_mentions_media_query(self, analyzer, received_msg,
                                                      combination_meta):
        prompt = analyzer._build_prompt("complex_html", {"html_length": 900},
                                        received_msg, combination_meta)
        assert "@media" in prompt
        assert "multipart/alternative" in prompt

    def test_forward_prompt_mentions_rfc822(self, analyzer, received_msg, combination_meta):
        prompt = analyzer._build_prompt(
            "forward", {"original_msg_id": "<o@t>"}, received_msg, combination_meta)
        assert "message/rfc822" in prompt
        assert "<o@t>" in prompt

    def test_forward_chain_alias_shares_checks(self, analyzer, received_msg,
                                               combination_meta):
        meta = {"original_msg_id": "<o@t>"}
        a = analyzer._build_prompt("forward", meta, received_msg, combination_meta)
        b = analyzer._build_prompt("forward_chain", meta, received_msg, combination_meta)
        assert "message/rfc822" in b
        assert a.replace("forward", "") == b.replace("forward_chain", "").replace("forward", "")

    def test_multi_attachment_prompt_mentions_count(self, analyzer, received_msg,
                                                    combination_meta):
        prompt = analyzer._build_prompt(
            "multi_attachment", {"attachment_count": 3, "attachment_name": "a.pdf, b.csv"},
            received_msg, combination_meta)
        assert "3" in prompt
        assert "a.pdf" in prompt

    def test_html_table_prompt_mentions_table(self, analyzer, received_msg,
                                              combination_meta):
        prompt = analyzer._build_prompt("html_table", {}, received_msg, combination_meta)
        assert "<table>" in prompt or "table" in prompt.lower()


# ═══════════════════════════════════════════════════════════════════
#  CSV bütünlüğü
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def combos():
    return parse_csv(str(PROJECT_ROOT / "mail_test_checklist.csv"))


class TestChecklistCsv:

    def test_all_combinations_have_all_scenarios(self, combos):
        for combo in combos:
            assert len(combo.scenarios) == 11, f"{combo.label}: {len(combo.scenarios)}"

    @pytest.mark.parametrize("scenario", NEW_SCENARIOS)
    def test_new_scenario_present_everywhere(self, combos, scenario):
        missing = [c.label for c in combos if scenario not in c.scenarios]
        assert not missing, f"{scenario} eksik: {missing}"

    def test_every_scenario_has_five_steps(self, combos):
        for combo in combos:
            for key, scenario in combo.scenarios.items():
                assert len(scenario.steps) == 5, f"{combo.label}/{key}"

    def test_all_scenarios_runnable(self, combos):
        found = {k for c in combos for k in c.scenarios}
        assert found <= SUPPORTED_SCENARIOS

    def test_step_ids_sequential(self, combos):
        ids = sorted(s.row_id for c in combos for sc in c.scenarios.values()
                     for s in sc.steps)
        assert ids == list(range(1, len(ids) + 1))

    def test_all_steps_pending(self, combos):
        for combo in combos:
            for scenario in combo.scenarios.values():
                for step in scenario.steps:
                    assert "Bekliyor" in step.status

    def test_server_fields_consistent_within_combination(self, combos):
        for combo in combos:
            for scenario in combo.scenarios.values():
                for step in scenario.steps:
                    assert step.receiver_server == combo.receiver_server
                    assert step.sender_server == combo.sender_server

    def test_scenario_type_map_covers_csv_names(self, combos):
        names = {s.scenario_type for c in combos for s in c.scenarios.values()}
        assert names <= set(SCENARIO_TYPE_MAP), (
            f"Eşlenmemiş senaryo adları: {sorted(names - set(SCENARIO_TYPE_MAP))}"
        )
