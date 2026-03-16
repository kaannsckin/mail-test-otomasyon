"""
test_message_templates.py — MessageTemplate modülü unit testleri.
"""

import pytest
from message_templates import (
    MessageTemplate,
    PLAIN_TEXT_TEMPLATES,
    ATTACHMENT_TEMPLATES,
    INLINE_IMAGE_TEMPLATES,
    REPLY_CHAIN_TEMPLATES,
    REPLY_ORIGINAL_TEMPLATES,
    ALL_TEMPLATES,
    SIGNATURE_BLOCK,
    SIGNATURE_HTML,
    get_template,
    get_reply_original,
    resolve_inline_html,
)


# ── Template yapı testleri ─────────────────────────────────────
class TestTemplateStructure:
    """Tüm template koleksiyonlarının yapısal doğruluğu."""

    @pytest.mark.parametrize("templates,name", [
        (PLAIN_TEXT_TEMPLATES, "plain_text"),
        (ATTACHMENT_TEMPLATES, "attachment"),
        (INLINE_IMAGE_TEMPLATES, "inline_image"),
        (REPLY_CHAIN_TEMPLATES, "reply_chain"),
        (REPLY_ORIGINAL_TEMPLATES, "reply_original"),
    ])
    def test_each_collection_has_three_templates(self, templates, name):
        assert len(templates) == 3, f"{name} koleksiyonu 3 şablon içermeli"

    @pytest.mark.parametrize("templates,name", [
        (PLAIN_TEXT_TEMPLATES, "plain_text"),
        (ATTACHMENT_TEMPLATES, "attachment"),
        (INLINE_IMAGE_TEMPLATES, "inline_image"),
        (REPLY_CHAIN_TEMPLATES, "reply_chain"),
        (REPLY_ORIGINAL_TEMPLATES, "reply_original"),
    ])
    def test_template_lengths_short_medium_long(self, templates, name):
        lengths = [t.length for t in templates]
        assert lengths == ["short", "medium", "long"], \
            f"{name}: beklenen sıra short/medium/long, bulunan {lengths}"

    def test_all_templates_are_frozen_dataclass(self):
        for key, templates in ALL_TEMPLATES.items():
            for t in templates:
                assert isinstance(t, MessageTemplate)
                with pytest.raises(AttributeError):
                    t.subject_tag = "changed"  # frozen olmalı

    def test_all_templates_have_nonempty_fields(self):
        all_lists = list(ALL_TEMPLATES.values()) + [REPLY_ORIGINAL_TEMPLATES]
        for templates in all_lists:
            for t in templates:
                assert t.subject_tag.strip(), "subject_tag boş olmamalı"
                assert t.body.strip(), "body boş olmamalı"
                assert t.length in ("short", "medium", "long")


# ── Türkçe karakter testleri ───────────────────────────────────
class TestTurkishCharacters:
    """Tüm şablonlarda Türkçe özel karakter bulunmalı."""

    TURKISH_CHARS = set("ğüşıöçĞÜŞİÖÇ")

    def test_all_templates_contain_turkish_chars(self):
        all_lists = list(ALL_TEMPLATES.values()) + [REPLY_ORIGINAL_TEMPLATES]
        for templates in all_lists:
            for t in templates:
                body_chars = set(t.body)
                found = body_chars & self.TURKISH_CHARS
                assert found, f"'{t.subject_tag}' şablonunda Türkçe karakter yok"


# ── get_template rotasyonu ─────────────────────────────────────
class TestGetTemplate:
    """get_template() rotasyon mantığı."""

    def test_rotation_returns_short_medium_long(self):
        for key in ALL_TEMPLATES:
            t0 = get_template(key, 0)
            t1 = get_template(key, 1)
            t2 = get_template(key, 2)
            assert t0.length == "short"
            assert t1.length == "medium"
            assert t2.length == "long"

    def test_rotation_wraps_around(self):
        t3 = get_template("plain_text", 3)
        t0 = get_template("plain_text", 0)
        assert t3.subject_tag == t0.subject_tag

    def test_unknown_scenario_falls_back_to_plain_text(self):
        t = get_template("nonexistent_scenario", 0)
        assert t == PLAIN_TEXT_TEMPLATES[0]


class TestGetReplyOriginal:
    """get_reply_original() testleri."""

    def test_rotation(self):
        for i in range(6):
            t = get_reply_original(i)
            expected = REPLY_ORIGINAL_TEMPLATES[i % 3]
            assert t == expected


# ── Inline HTML çözümleme ──────────────────────────────────────
class TestResolveInlineHtml:
    """resolve_inline_html() yer tutucu doldurma testleri."""

    def test_replaces_cid_placeholder(self):
        html = '<img src="cid:{{CID}}" />'
        result = resolve_inline_html(html, "my_image_123")
        assert "cid:my_image_123" in result
        assert "{{CID}}" not in result

    def test_replaces_signature_placeholder(self):
        html = "<p>Test</p>{{SIGNATURE_HTML}}"
        result = resolve_inline_html(html, "test_cid")
        assert "{{SIGNATURE_HTML}}" not in result
        assert "Mail Otomasyon Test Sistemi" in result

    def test_inline_templates_have_cid_placeholder(self):
        for t in INLINE_IMAGE_TEMPLATES:
            assert "{{CID}}" in t.body, f"'{t.subject_tag}' CID placeholder eksik"
            assert "{{SIGNATURE_HTML}}" in t.body


# ── Signature testleri ─────────────────────────────────────────
class TestSignatures:
    def test_signature_block_not_empty(self):
        assert "Saygılarımla" in SIGNATURE_BLOCK
        assert "Bilgi Teknolojileri" in SIGNATURE_BLOCK

    def test_signature_html_is_html(self):
        assert "<hr" in SIGNATURE_HTML
        assert "<strong>" in SIGNATURE_HTML


# ── ALL_TEMPLATES sözlüğü ─────────────────────────────────────
class TestAllTemplatesDict:
    def test_has_four_scenario_keys(self):
        expected = {"plain_text", "attachment", "inline_image", "reply_chain"}
        assert set(ALL_TEMPLATES.keys()) == expected

    def test_reply_original_not_in_all_templates(self):
        assert "reply_original" not in ALL_TEMPLATES
