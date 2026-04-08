"""
test_template_manager.py — template_manager modülü unit testleri.
"""
import sys
from pathlib import Path
import tempfile, os

import pytest
import yaml

# Proje kökünü path'e ekle
sys.path.insert(0, str(Path(__file__).parent.parent))
import template_manager as tm
from message_templates import (
    PLAIN_TEXT_TEMPLATES,
    ALL_TEMPLATES,
    REPLY_ORIGINAL_TEMPLATES,
    MessageTemplate,
)


# ── Yardımcı ───────────────────────────────────────────────────────

class IsolatedTemplateManager:
    """Her testin kendi geçici templates.yaml ile çalışmasını sağlar."""
    def __init__(self, tmpdir):
        self.orig_path = tm.TEMPLATES_PATH
        self.tmp_path = Path(tmpdir) / "templates.yaml"
        tm.TEMPLATES_PATH = self.tmp_path

    def __enter__(self):
        return self

    def __exit__(self, *_):
        tm.TEMPLATES_PATH = self.orig_path


# ── Hardcoded fallback ──────────────────────────────────────────────

def test_get_template_fallback_no_yaml(tmp_path):
    """templates.yaml yoksa message_templates.py'deki hardcoded şablon döner."""
    with IsolatedTemplateManager(tmp_path):
        t = tm.get_template("plain_text", 0)
    assert isinstance(t, MessageTemplate)
    assert t.subject_tag == PLAIN_TEXT_TEMPLATES[0].subject_tag


def test_get_template_fallback_rotation(tmp_path):
    """Rotation indeksi modulo ile çalışmalı."""
    with IsolatedTemplateManager(tmp_path):
        t0 = tm.get_template("plain_text", 0)
        t3 = tm.get_template("plain_text", 3)  # 3 şablon var → idx 3 % 3 = 0
    assert t0.subject_tag == t3.subject_tag


def test_get_reply_original_fallback(tmp_path):
    with IsolatedTemplateManager(tmp_path):
        t = tm.get_reply_original(0)
    assert t.subject_tag == REPLY_ORIGINAL_TEMPLATES[0].subject_tag


def test_resolve_inline_html():
    html = "<img src='cid:{{CID}}'>{{SIGNATURE_HTML}}"
    result = tm.resolve_inline_html(html, "abc123")
    assert "cid:abc123" in result
    assert "{{CID}}" not in result
    assert "{{SIGNATURE_HTML}}" not in result


# ── YAML-tabanlı şablonlar ──────────────────────────────────────────

def test_get_template_from_yaml(tmp_path):
    """templates.yaml varsa oradan yükler."""
    with IsolatedTemplateManager(tmp_path) as m:
        yaml_data = {
            "templates": {
                "plain_text": [
                    {"subject_tag": "YAML Konu", "body": "YAML gövde", "length": "short"}
                ]
            }
        }
        m.tmp_path.write_text(yaml.dump(yaml_data, allow_unicode=True), encoding="utf-8")
        t = tm.get_template("plain_text", 0)
    assert t.subject_tag == "YAML Konu"
    assert t.body == "YAML gövde"
    assert t.length == "short"


def test_get_template_yaml_rotation(tmp_path):
    """YAML'daki şablon listesinde de rotation çalışmalı."""
    with IsolatedTemplateManager(tmp_path) as m:
        yaml_data = {
            "templates": {
                "attachment": [
                    {"subject_tag": "A", "body": "body a", "length": "short"},
                    {"subject_tag": "B", "body": "body b", "length": "medium"},
                ]
            }
        }
        m.tmp_path.write_text(yaml.dump(yaml_data), encoding="utf-8")
        t0 = tm.get_template("attachment", 0)
        t1 = tm.get_template("attachment", 1)
        t2 = tm.get_template("attachment", 2)  # 2 % 2 = 0 → "A"
    assert t0.subject_tag == "A"
    assert t1.subject_tag == "B"
    assert t2.subject_tag == "A"


def test_yaml_missing_scenario_falls_back(tmp_path):
    """YAML'da olmayan senaryo için hardcoded fallback çalışmalı."""
    with IsolatedTemplateManager(tmp_path) as m:
        yaml_data = {"templates": {"attachment": []}}  # plain_text YOK
        m.tmp_path.write_text(yaml.dump(yaml_data), encoding="utf-8")
        t = tm.get_template("plain_text", 0)
    # Hardcoded'dan gelmeli
    assert t.subject_tag == PLAIN_TEXT_TEMPLATES[0].subject_tag


# ── export_defaults_to_yaml ─────────────────────────────────────────

def test_export_defaults_creates_file(tmp_path):
    with IsolatedTemplateManager(tmp_path) as m:
        tm.export_defaults_to_yaml()
        assert m.tmp_path.exists()
        data = yaml.safe_load(m.tmp_path.read_text(encoding="utf-8"))
    assert "templates" in data
    assert "plain_text" in data["templates"]
    assert len(data["templates"]["plain_text"]) == len(PLAIN_TEXT_TEMPLATES)


def test_export_defaults_contains_reply_original(tmp_path):
    with IsolatedTemplateManager(tmp_path) as m:
        tm.export_defaults_to_yaml()
        data = yaml.safe_load(m.tmp_path.read_text(encoding="utf-8"))
    assert "reply_original" in data["templates"]
    assert len(data["templates"]["reply_original"]) == len(REPLY_ORIGINAL_TEMPLATES)


# ── save_templates / get_all_templates_for_api ──────────────────────

def test_save_and_load_roundtrip(tmp_path):
    payload = {
        "signature_block": "-- test sig",
        "templates": {
            "plain_text": [{"subject_tag": "X", "body": "Y", "length": "short"}]
        }
    }
    with IsolatedTemplateManager(tmp_path) as m:
        tm.save_templates(payload)
        result = tm.get_all_templates_for_api()
    assert result["templates"]["plain_text"][0]["subject_tag"] == "X"


def test_get_all_templates_no_file_returns_defaults(tmp_path):
    with IsolatedTemplateManager(tmp_path):
        result = tm.get_all_templates_for_api()
    assert "templates" in result
    for key in ALL_TEMPLATES:
        assert key in result["templates"]
