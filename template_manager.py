"""
template_manager.py — YAML tabanlı şablon yönetimi.

templates.yaml varsa oradan yükler; yoksa message_templates.py'deki
hardcoded şablonlara fallback yapar. Aynı public API'yi sağlar:
  get_template(), get_reply_original(), resolve_inline_html(), SIGNATURE_HTML
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from message_templates import (
    MessageTemplate,
    SIGNATURE_HTML,
    SIGNATURE_BLOCK,
    ALL_TEMPLATES,
    REPLY_ORIGINAL_TEMPLATES,
    get_template as _default_get_template,
    get_reply_original as _default_get_reply_original,
)

TEMPLATES_PATH = Path("templates.yaml")


# ── Yardımcılar ────────────────────────────────────────────────────

def _tmpl_list_to_dicts(templates) -> list:
    return [{"subject_tag": t.subject_tag, "body": t.body, "length": t.length}
            for t in templates]


def _defaults_as_dict() -> dict:
    """Hardcoded şablonları YAML-serializable dict'e çevirir."""
    tmpls: dict = {}
    for key, lst in ALL_TEMPLATES.items():
        tmpls[key] = _tmpl_list_to_dicts(lst)
    tmpls["reply_original"] = _tmpl_list_to_dicts(REPLY_ORIGINAL_TEMPLATES)
    return tmpls


# ── CRUD ───────────────────────────────────────────────────────────

def export_defaults_to_yaml() -> None:
    """Hardcoded şablonları templates.yaml dosyasına aktar (ilk kurulum)."""
    data = {
        "signature_block": SIGNATURE_BLOCK,
        "signature_html": SIGNATURE_HTML,
        "templates": _defaults_as_dict(),
    }
    with open(TEMPLATES_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def load_templates_yaml() -> Optional[dict]:
    """templates.yaml'ı yükler; dosya yoksa None döner."""
    if not TEMPLATES_PATH.exists():
        return None
    with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or None


def save_templates(data: dict) -> None:
    """Şablon verisini templates.yaml'a kaydeder."""
    with open(TEMPLATES_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def get_all_templates_for_api() -> dict:
    """API için tüm şablonları döndürür (YAML varsa ondan, yoksa hardcoded)."""
    data = load_templates_yaml()
    if data:
        return data
    return {
        "signature_block": SIGNATURE_BLOCK,
        "signature_html": SIGNATURE_HTML,
        "templates": _defaults_as_dict(),
    }


# ── Public API (main.py aynı şekilde kullanır) ─────────────────────

def get_template(scenario_key: str, rotation_index: int) -> MessageTemplate:
    """Senaryo ve rotasyon indeksine göre şablon döndürür."""
    data = load_templates_yaml()
    if data:
        tmpl_list = data.get("templates", {}).get(scenario_key, [])
        if tmpl_list:
            t = tmpl_list[rotation_index % len(tmpl_list)]
            return MessageTemplate(
                subject_tag=t["subject_tag"],
                body=t["body"],
                length=t["length"],
            )
    return _default_get_template(scenario_key, rotation_index)


def get_reply_original(rotation_index: int) -> MessageTemplate:
    """Reply chain orijinal mesajı için şablon döndürür."""
    data = load_templates_yaml()
    if data:
        tmpl_list = data.get("templates", {}).get("reply_original", [])
        if tmpl_list:
            t = tmpl_list[rotation_index % len(tmpl_list)]
            return MessageTemplate(
                subject_tag=t["subject_tag"],
                body=t["body"],
                length=t["length"],
            )
    return _default_get_reply_original(rotation_index)


def resolve_inline_html(html: str, cid: str) -> str:
    """Inline image şablonundaki {{CID}} ve {{SIGNATURE_HTML}} yer tutucularını doldurur."""
    return html.replace("{{CID}}", cid).replace("{{SIGNATURE_HTML}}", SIGNATURE_HTML)
