"""
test_csv_parser.py — CSV parser modülü unit testleri.
"""

import os
import tempfile
import pytest
from csv_parser import (
    parse_csv,
    _parse_combination_header,
    TestCombination,
    TestScenario,
    TestStep,
    SCENARIO_TYPE_MAP,
)


# ── Combination header parser ─────────────────────────────────
class TestParseCombinationHeader:
    """_parse_combination_header() testleri."""

    def test_standard_format(self):
        text = "🔀 Kombinasyon: Alan → EMS / iOS  Gönderen → Gmail / Android"
        result = _parse_combination_header(text)
        assert result["receiver_server"] == "EMS"
        assert result["receiver_client"] == "iOS"
        assert result["sender_server"] == "Gmail"
        assert result["sender_client"] == "Android"
        assert "EMS" in result["label"]

    def test_arrow_variations(self):
        text = "🔀 Kombinasyon: Alan > Outlook / Desktop  Gönderen > EMS / Thunderbird"
        result = _parse_combination_header(text)
        assert result["receiver_server"] == "Outlook"
        assert result["sender_server"] == "EMS"

    def test_fallback_on_unrecognized_format(self):
        text = "🔀 Some random text without proper format"
        result = _parse_combination_header(text)
        assert result["receiver_server"] == "?"
        assert result["sender_server"] == "?"
        assert result["label"]  # boş olmamalı


# ── CSV parse entegrasyon ──────────────────────────────────────
class TestParseCSV:
    """parse_csv() tam CSV parse testleri."""

    SAMPLE_CSV = """\
🔀 Kombinasyon: Alan → EMS / iOS  Gönderen → Gmail / Android
Senaryo,Senaryo Tipi,Alan Sunucu,Alan İstemci,Gönderen Sunucu,Gönderen İstemci,Açıklama,Durum
1,Sadece İçerik (Plain Text),EMS,iOS,Gmail,Android,Düz metin gönder ve al,⬜ Bekliyor
2,Sadece İçerik (Plain Text),EMS,iOS,Gmail,Android,Türkçe karakter kontrolü,⬜ Bekliyor
3,Eklentili Mesaj (Attachment),EMS,iOS,Gmail,Android,PDF ek gönder,⬜ Bekliyor

🔀 Kombinasyon: Alan → Gmail / Android  Gönderen → Outlook / Desktop
Senaryo,Senaryo Tipi,Alan Sunucu,Alan İstemci,Gönderen Sunucu,Gönderen İstemci,Açıklama,Durum
4,Sadece İçerik (Plain Text),Gmail,Android,Outlook,Desktop,Düz metin testi,⬜ Bekliyor
"""

    @pytest.fixture
    def csv_file(self, tmp_path):
        f = tmp_path / "test_checklist.csv"
        f.write_text(self.SAMPLE_CSV, encoding="utf-8")
        return str(f)

    def test_parses_two_combinations(self, csv_file):
        combos = parse_csv(csv_file)
        assert len(combos) == 2

    def test_first_combo_details(self, csv_file):
        combos = parse_csv(csv_file)
        c = combos[0]
        assert c.receiver_server == "EMS"
        assert c.receiver_client == "iOS"
        assert c.sender_server == "Gmail"
        assert c.sender_client == "Android"

    def test_scenarios_grouped_correctly(self, csv_file):
        combos = parse_csv(csv_file)
        c = combos[0]
        assert "plain_text" in c.scenarios
        assert "attachment" in c.scenarios
        assert len(c.scenarios["plain_text"].steps) == 2
        assert len(c.scenarios["attachment"].steps) == 1

    def test_second_combo_details(self, csv_file):
        combos = parse_csv(csv_file)
        c = combos[1]
        assert c.receiver_server == "Gmail"
        assert c.sender_server == "Outlook"

    def test_step_fields(self, csv_file):
        combos = parse_csv(csv_file)
        step = combos[0].scenarios["plain_text"].steps[0]
        assert step.row_id == 1
        assert step.scenario_key == "plain_text"
        assert step.step_description == "Düz metin gönder ve al"
        assert step.status == "⬜ Bekliyor"

    def test_empty_csv(self, tmp_path):
        f = tmp_path / "empty.csv"
        f.write_text("", encoding="utf-8")
        combos = parse_csv(str(f))
        assert combos == []


# ── SCENARIO_TYPE_MAP ──────────────────────────────────────────
class TestScenarioTypeMap:
    def test_has_nine_mappings(self):
        """Sprint 4 sonrası 9 senaryo tipi mevcut."""
        assert len(SCENARIO_TYPE_MAP) == 9

    def test_plain_text_mapping(self):
        assert SCENARIO_TYPE_MAP["Sadece İçerik (Plain Text)"] == "plain_text"

    def test_attachment_mapping(self):
        assert SCENARIO_TYPE_MAP["Eklentili Mesaj (Attachment)"] == "attachment"

    def test_inline_image_mapping(self):
        assert SCENARIO_TYPE_MAP["Inline Resim (Embedded Image)"] == "inline_image"

    def test_reply_chain_mapping(self):
        assert SCENARIO_TYPE_MAP["Cevaplama & Bozulma Testi (Reply Chain)"] == "reply_chain"

    def test_smime_mapping(self):
        assert SCENARIO_TYPE_MAP["İmzalı Mesaj (S/MIME / PGP)"] == "smime"

    def test_calendar_invite_mapping(self):
        assert SCENARIO_TYPE_MAP["Takvim Daveti (iTIP / ICS)"] == "calendar_invite"

    def test_i18n_mapping(self):
        assert SCENARIO_TYPE_MAP["Uluslararası Alfabe ve Emoji Sınaması"] == "i18n"

    def test_complex_html_mapping(self):
        assert SCENARIO_TYPE_MAP["Rich CSS ve Media Query Sınaması"] == "complex_html"

    def test_forward_mapping(self):
        assert SCENARIO_TYPE_MAP["Forward (Mesaj İletme) Akışı"] == "forward"

    def test_all_values_are_strings(self):
        assert all(isinstance(v, str) for v in SCENARIO_TYPE_MAP.values())

    def test_all_keys_are_strings(self):
        assert all(isinstance(k, str) for k in SCENARIO_TYPE_MAP.keys())

    def test_no_duplicate_values(self):
        values = list(SCENARIO_TYPE_MAP.values())
        assert len(values) == len(set(values)), "Duplicate scenario keys found!"


# ── Dataclass testleri ─────────────────────────────────────────
class TestDataclasses:
    def test_test_step_defaults(self):
        step = TestStep(
            row_id=1, scenario_type="test", scenario_key="test",
            receiver_server="A", receiver_client="B",
            sender_server="C", sender_client="D",
            step_description="desc"
        )
        assert step.status == "⬜ Bekliyor"
        assert step.result == ""
        assert step.detail == ""

    def test_test_combination_empty_scenarios(self):
        combo = TestCombination(
            label="test", receiver_server="A", receiver_client="B",
            sender_server="C", sender_client="D"
        )
        assert combo.scenarios == {}
