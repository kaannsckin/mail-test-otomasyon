#!/usr/bin/env python3
"""
extend_checklist.py — Test checklist CSV'sini yeni senaryolarla genişletir.

mail_test_checklist.csv her kombinasyon için senaryo bloklarından oluşur.
Bu betik, orkestratörün desteklediği ancak CSV'de bulunmayan senaryoları
her kombinasyona ekler ve adım numaralarını baştan sona yeniden verir.

Idempotenttir: zaten var olan senaryo blokları tekrar eklenmez, yalnızca
eksik olanlar tamamlanır.

Kullanım:
    python scripts/extend_checklist.py                 # yerinde günceller
    python scripts/extend_checklist.py --dry-run       # yalnızca rapor
    python scripts/extend_checklist.py --out yeni.csv  # başka dosyaya yaz
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from csv_parser import SCENARIO_TYPE_MAP, SUPPORTED_SCENARIOS  # noqa: E402

DEFAULT_CSV = REPO_ROOT / "mail_test_checklist.csv"
PENDING = "⬜ Bekliyor"

# Senaryoların CSV'deki görünme sırası. Mevcut dosyadaki 1-5 korunur,
# yeni senaryolar sonrasına eklenir.
SCENARIO_ORDER = [
    "Sadece İçerik (Plain Text)",
    "Eklentili Mesaj (Attachment)",
    "Inline Resim (Embedded Image)",
    "İmzalı Mesaj (S/MIME / PGP)",
    "Cevaplama & Bozulma Testi (Reply Chain)",
    "Çoklu Eklenti (Multi Attachment)",
    "HTML Tablo Render Testi",
    "Rich CSS ve Media Query Sınaması",
    "Uluslararası Alfabe ve Emoji Sınaması",
    "Takvim Daveti (iTIP / ICS)",
    "Forward (Mesaj İletme) Akışı",
]

# Her senaryo için 5 kontrol noktası — analyzer.py'deki kontrol listeleriyle
# aynı hususları insan diliyle karşılar.
SCENARIO_STEPS: dict[str, list[str]] = {
    "Çoklu Eklenti (Multi Attachment)": [
        "Birden fazla ek içeren mesaj gönder",
        "Eklerin tamamı iletildi mi? (adet kontrolü)",
        "Her ekin dosya adı ve uzantısı korundu mu?",
        "Ek boyutları orijinaliyle tutarlı mı?",
        "Ekler alıcı tarafında açılabiliyor / bozulmamış mı?",
    ],
    "HTML Tablo Render Testi": [
        "HTML tablo içeren mesaj gönder",
        "Tablo yapısı (satır / sütun) korundu mu?",
        "Hücre stilleri (kenarlık, dolgu, arka plan) korundu mu?",
        "Tablo içindeki Türkçe karakterler bozulmadı mı?",
        "Tablo düz metne indirgenmedi mi?",
    ],
    "Rich CSS ve Media Query Sınaması": [
        "Zengin CSS içeren HTML mesaj gönder",
        "HTML düzeni alıcı istemcide korundu mu?",
        "<style> bloğu veya inline stiller kaldırıldı mı?",
        "Media query ile duyarlı (mobil) yerleşim çalışıyor mu?",
        "Düz metin (text/plain) alternatifi okunabilir mi?",
    ],
    "Uluslararası Alfabe ve Emoji Sınaması": [
        "Çok alfabeli ve emoji içeren mesaj gönder",
        "Konu (Subject) satırındaki özel karakterler bozulmadı mı?",
        "Türkçe karakterler korundu mu? (ğüşıöç ĞÜŞİÖÇ)",
        "Latin dışı alfabeler korundu mu? (Arapça, CJK)",
        "Emoji karakterleri kutu / soru işaretine dönüşmedi mi?",
    ],
    "Takvim Daveti (iTIP / ICS)": [
        "Takvim daveti (ICS) içeren mesaj gönder",
        "Alıcı istemcide davet olarak tanındı mı? (Kabul / Reddet düğmeleri)",
        "text/calendar part'ı ve METHOD=REQUEST korundu mu?",
        "Toplantı başlığı, tarihi ve saati doğru görünüyor mu?",
        "invite.ics eki açılabiliyor mu?",
    ],
    "Forward (Mesaj İletme) Akışı": [
        "Orijinal mesajı gönder ve ilet (forward)",
        "Konu 'Fwd:' öneki ile geldi mi?",
        "message/rfc822 kapsüllemesi korundu mu?",
        "Orijinal Kimden / Tarih / Konu bilgileri okunabiliyor mu?",
        "MIME boundary izolasyonu bozulmadı mı?",
    ],
}


def is_combo_header(row: list[str]) -> bool:
    return bool(row) and "🔀" in row[0]


def is_scenario_header(row: list[str]) -> bool:
    return bool(row) and row[0].strip().startswith("Senaryo")


def is_step(row: list[str]) -> bool:
    return bool(row) and row[0].strip().isdigit()


class Combination:
    """Bir kombinasyon bloğu: başlık satırı + senaryo → adım satırları."""

    def __init__(self, header: list[str]):
        self.header = header
        self.scenarios: dict[str, list[list[str]]] = {}
        self.order: list[str] = []
        self.recv_server = self.recv_client = ""
        self.send_server = self.send_client = ""

    def add_step(self, scenario_name: str, row: list[str]):
        if scenario_name not in self.scenarios:
            self.scenarios[scenario_name] = []
            self.order.append(scenario_name)
        self.scenarios[scenario_name].append(row)
        if not self.recv_server and len(row) >= 6:
            self.recv_server, self.recv_client = row[2], row[3]
            self.send_server, self.send_client = row[4], row[5]


def parse(path: Path):
    """CSV'yi (önsöz satırları, kombinasyon listesi) olarak ayrıştırır."""
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    preamble: list[list[str]] = []
    combos: list[Combination] = []
    current: Combination | None = None
    current_scenario = ""

    for row in rows:
        if is_combo_header(row):
            current = Combination(row)
            combos.append(current)
            current_scenario = ""
            continue
        if current is None:
            preamble.append(row)
            continue
        if is_scenario_header(row):
            # "  Senaryo 3: Inline Resim (Embedded Image)" → senaryo adı
            current_scenario = row[0].split(":", 1)[1].strip() if ":" in row[0] else ""
            continue
        if is_step(row) and len(row) > 1:
            current.add_step(row[1].strip(), row)

    return preamble, combos


def build_step(scenario_name: str, combo: Combination, description: str) -> list[str]:
    return ["", scenario_name, combo.recv_server, combo.recv_client,
            combo.send_server, combo.send_client, description, PENDING, ""]


def extend(combos: list[Combination]) -> dict[str, int]:
    """Eksik senaryoları ekler; senaryo → eklenen kombinasyon sayısı döndürür."""
    added: dict[str, int] = {}
    for combo in combos:
        for scenario_name in SCENARIO_ORDER:
            if scenario_name in combo.scenarios:
                continue
            steps = SCENARIO_STEPS.get(scenario_name)
            if not steps:
                continue        # mevcut 1-5 senaryosu, tanımı burada yok
            combo.scenarios[scenario_name] = [
                build_step(scenario_name, combo, d) for d in steps
            ]
            combo.order.append(scenario_name)
            added[scenario_name] = added.get(scenario_name, 0) + 1
    return added


def render(preamble: list[list[str]], combos: list[Combination]) -> list[list[str]]:
    """Adım numaralarını baştan vererek satırları yeniden üretir."""
    out = list(preamble)
    counter = 1
    for combo in combos:
        out.append(combo.header)
        ordered = sorted(
            combo.order,
            key=lambda n: SCENARIO_ORDER.index(n) if n in SCENARIO_ORDER else 99,
        )
        for index, scenario_name in enumerate(ordered, start=1):
            width = len(combo.header)
            head = [f"  Senaryo {index}: {scenario_name}"] + [""] * (width - 1)
            out.append(head)
            for row in combo.scenarios[scenario_name]:
                new_row = list(row)
                new_row[0] = str(counter)
                counter += 1
                out.append(new_row)
    return out


def main():
    parser = argparse.ArgumentParser(description="Checklist CSV'sini genişlet")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Kaynak CSV")
    parser.add_argument("--out", default=None, help="Hedef CSV (varsayılan: yerinde)")
    parser.add_argument("--dry-run", action="store_true", help="Yazma, yalnızca raporla")
    parser.add_argument("--no-backup", action="store_true", help="Yedek alma")
    args = parser.parse_args()

    src = Path(args.csv)
    if not src.exists():
        raise SystemExit(f"HATA: CSV bulunamadı: {src}")

    preamble, combos = parse(src)
    before = sum(len(s) for c in combos for s in c.scenarios.values())
    print(f"Okundu: {len(combos)} kombinasyon, {before} adım")

    # Doğrulama: CSV'deki her senaryo orkestratör tarafından çalıştırılabilmeli
    unknown = {
        name for c in combos for name in c.scenarios
        if SCENARIO_TYPE_MAP.get(name, name) not in SUPPORTED_SCENARIOS
    }
    if unknown:
        print(f"UYARI: orkestratörün tanımadığı senaryo tipleri: {sorted(unknown)}")

    added = extend(combos)
    if not added:
        print("Tüm senaryolar zaten mevcut — değişiklik yok.")
        return

    for name, count in added.items():
        print(f"  + {name}: {count} kombinasyona eklendi")

    rows = render(preamble, combos)
    after = sum(len(s) for c in combos for s in c.scenarios.values())
    print(f"Sonuç: {len(combos)} kombinasyon, {after} adım (+{after - before})")

    if args.dry_run:
        print("(--dry-run: dosya yazılmadı)")
        return

    dest = Path(args.out) if args.out else src
    if dest == src and not args.no_backup:
        backup = src.with_suffix(".csv.bak")
        shutil.copy2(src, backup)
        print(f"Yedek: {backup}")

    with open(dest, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    print(f"Yazıldı: {dest}")


if __name__ == "__main__":
    main()
