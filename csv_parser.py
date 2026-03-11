"""
csv_parser.py — Mail Test Checklist CSV'sini okuyarak test senaryolarını gruplandırır.
"""

import csv
import re
import logging
from dataclasses import dataclass, field
from typing import List, Dict

logger = logging.getLogger(__name__)

SCENARIO_TYPE_MAP = {
    "Sadece İçerik (Plain Text)": "plain_text",
    "Eklentili Mesaj (Attachment)": "attachment",
    "Inline Resim (Embedded Image)": "inline_image",
    "İmzalı Mesaj (S/MIME / PGP)": "smime",
    "Cevaplama & Bozulma Testi (Reply Chain)": "reply_chain",
}


@dataclass
class TestStep:
    row_id: int
    scenario_type: str
    scenario_key: str
    receiver_server: str
    receiver_client: str
    sender_server: str
    sender_client: str
    step_description: str
    status: str = "⬜ Bekliyor"
    result: str = ""
    detail: str = ""


@dataclass
class TestScenario:
    combination: str
    receiver_server: str
    receiver_client: str
    sender_server: str
    sender_client: str
    scenario_type: str
    scenario_key: str
    steps: List[TestStep] = field(default_factory=list)


@dataclass
class TestCombination:
    label: str
    receiver_server: str
    receiver_client: str
    sender_server: str
    sender_client: str
    scenarios: Dict[str, TestScenario] = field(default_factory=dict)


def parse_csv(csv_path: str) -> List[TestCombination]:
    """CSV dosyasını okuyarak TestCombination listesi döndürür."""
    combinations = []
    current_combo = None

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not any(row):
                continue

            # Kombinasyon başlık satırı
            # Bazı dosyalarda "Farklı Client Kombinasyonları ..." gibi açıklama satırları
            # "Kombinasyon" kelimesini içerir ama gerçek kombinasyon header'ı değildir.
            # Gerçek header genellikle "🔀" içerir veya "Alan/Gönderen" kalıbını taşır.
            if ("🔀" in row[0]) or (
                "Kombinasyon" in row[0] and ("Alan" in row[0] and "Gönderen" in row[0])
            ):
                combo_info = _parse_combination_header(row[0])
                if combo_info:
                    current_combo = TestCombination(**combo_info)
                    combinations.append(current_combo)
                continue

            # Senaryo başlık satırı (numara yok, senaryo adı var)
            if row[0].strip().startswith("Senaryo") or (
                not row[0].strip().isdigit() and row[1].strip() in SCENARIO_TYPE_MAP
                and not row[0].strip()
            ):
                continue

            # Test adımı satırı
            try:
                row_id = int(row[0].strip())
            except (ValueError, IndexError):
                continue

            if current_combo is None or len(row) < 7:
                continue

            scenario_type_tr = row[1].strip()
            scenario_key = SCENARIO_TYPE_MAP.get(scenario_type_tr, scenario_type_tr)

            step = TestStep(
                row_id=row_id,
                scenario_type=scenario_type_tr,
                scenario_key=scenario_key,
                receiver_server=row[2].strip(),
                receiver_client=row[3].strip(),
                sender_server=row[4].strip(),
                sender_client=row[5].strip(),
                step_description=row[6].strip(),
                status=row[7].strip() if len(row) > 7 else "⬜ Bekliyor",
            )

            # Senaryoyu mevcut kombinasyona ekle
            if scenario_key not in current_combo.scenarios:
                current_combo.scenarios[scenario_key] = TestScenario(
                    combination=current_combo.label,
                    receiver_server=current_combo.receiver_server,
                    receiver_client=current_combo.receiver_client,
                    sender_server=current_combo.sender_server,
                    sender_client=current_combo.sender_client,
                    scenario_type=scenario_type_tr,
                    scenario_key=scenario_key,
                )
            current_combo.scenarios[scenario_key].steps.append(step)

    logger.info(f"CSV okundu: {len(combinations)} kombinasyon, "
                f"{sum(len(c.scenarios) for c in combinations)} senaryo, "
                f"{sum(len(s.steps) for c in combinations for s in c.scenarios.values())} adım")
    return combinations


def _parse_combination_header(text: str) -> dict:
    """
    '🔀 Kombinasyon: Alan → EMS / iOS  Gönderen → Gmail / Android'
    formatından sunucu/istemci bilgisini çıkarır.
    """
    # Alan → X / Y  Gönderen → A / B
    pattern = r"Alan\s*[→>]\s*(\w+)\s*/\s*(\w+).*?Gönderen\s*[→>]\s*(\w+)\s*/\s*(\w+)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        recv_server, recv_client, send_server, send_client = match.groups()
        label = f"{recv_server}/{recv_client} ← {send_server}/{send_client}"
        return {
            "label": label,
            "receiver_server": recv_server,
            "receiver_client": recv_client,
            "sender_server": send_server,
            "sender_client": send_client,
        }
    # Eşleşme bulunamazsa ham metni kullan
    clean = re.sub(r'[🔀\s]+', ' ', text).strip()
    return {
        "label": clean,
        "receiver_server": "?",
        "receiver_client": "?",
        "sender_server": "?",
        "sender_client": "?",
    }
