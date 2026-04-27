"""
main.py — Mail Otomasyon Orkestratörü
======================================
Kullanım:
  python main.py                          # Tüm testleri çalıştır
  python main.py --combo 0               # Sadece 0. kombinasyonu çalıştır
  python main.py --scenario plain_text   # Sadece plain_text senaryolarını çalıştır
  python main.py --dry-run               # Bağlantı testi yap, mail gönderme
  python main.py --csv yol/dosya.csv    # Farklı CSV kullan
"""

import argparse
import logging
import os
import sys
import time
import uuid
import yaml
from datetime import datetime
from email.utils import make_msgid
from pathlib import Path

# Proje modülleri
from csv_parser import parse_csv, TestCombination
from sender import MailSender
from receiver import MailReceiver
from analyzer import MailAnalyzer
from reporter import generate_html_report, generate_csv_results
from message_templates import (
    get_template,
    get_reply_original,
    resolve_inline_html,
    SIGNATURE_HTML,
)

# ------------------------------------------------------------------ #
#  CSV path resolution
# ------------------------------------------------------------------ #
def resolve_csv_path(csv_path: str) -> str:
    """
    Resolve CSV path robustly relative to repo root.
    - Accept absolute paths.
    - Resolve relative paths against the directory containing this file.
    - If missing, try common fallbacks and pick a likely CSV in repo root.
    """
    repo_root = Path(__file__).parent
    p = Path(csv_path)

    candidates: list[Path] = [p]
    if not p.is_absolute():
        candidates += [repo_root / p, repo_root / p.name]
    candidates.append(repo_root / "mail_test_checklist.csv")

    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return str(c)
        except OSError:
            continue

    csvs = [x for x in repo_root.glob("*.csv") if x.is_file()]
    if csvs:
        preferred = sorted(
            csvs,
            key=lambda x: (
                0 if "checklist" in x.name.lower() else 1,
                0 if "mail" in x.name.lower() else 1,
                len(x.name),
            ),
        )[0]
        return str(preferred)

    return csv_path

# ------------------------------------------------------------------ #
#  Logging kurulumu
# ------------------------------------------------------------------ #
def setup_logging(config: dict):
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = config.get("logging", {}).get("file", "logs/automation.log")
    level = getattr(logging, config.get("logging", {}).get("level", "INFO"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ]
    )

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Server config resolver
# ------------------------------------------------------------------ #
SERVER_KEY_MAP = {
    "ems": "ems",
    "gmail": "gmail",
    "outlook": "outlook",
}

def get_server_config(config: dict, server_name: str) -> dict:
    key = SERVER_KEY_MAP.get(server_name.lower())
    if not key or key not in config:
        raise ValueError(f"Bilinmeyen sunucu: {server_name}. config.yaml'da tanımlı değil.")
    return config[key]


# ------------------------------------------------------------------ #
#  Test dosyalarını hazırla
# ------------------------------------------------------------------ #
def prepare_test_files():
    """Yoksa örnek test dosyaları oluşturur."""
    Path("test_files").mkdir(exist_ok=True)

    pdf_path = "test_files/test_document.pdf"
    if not os.path.exists(pdf_path):
        # Minimal geçerli PDF
        pdf_content = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]
  /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length 44 >> stream
BT /F1 12 Tf 100 700 Td (Mail Otomasyon Test Dosyasi) Tj ET
endstream endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000274 00000 n
0000000369 00000 n
trailer << /Size 6 /Root 1 0 R >>
startxref
441
%%EOF"""
        with open(pdf_path, "wb") as f:
            f.write(pdf_content)
        logger.debug(f"Test PDF oluşturuldu: {pdf_path}")

    img_path = "test_files/test_image.png"
    if not os.path.exists(img_path):
        # 1x1 kırmızı PNG
        png_bytes = bytes([
            137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82,
            0,0,0,1,0,0,0,1,8,2,0,0,0,144,119,83,222,0,0,0,
            12,73,68,65,84,8,215,99,248,207,192,0,0,0,2,0,1,
            226,33,188,51,0,0,0,0,73,69,78,68,174,66,96,130
        ])
        with open(img_path, "wb") as f:
            f.write(png_bytes)
        logger.debug(f"Test PNG oluşturuldu: {img_path}")


# ------------------------------------------------------------------ #
#  Tek senaryo çalıştır
# ------------------------------------------------------------------ #
def _resolve_attachment_paths(test_config: dict) -> list[str]:
    """Config'den attachment dosya yollarını çözümler.

    Önce ``test_attachment_paths`` (liste) kontrol edilir,
    yoksa eski ``test_attachment_path`` (tekil) kullanılır.
    """
    repo_root = Path(__file__).parent

    paths_cfg = test_config.get("test_attachment_paths")
    if paths_cfg and isinstance(paths_cfg, list):
        raw_list = paths_cfg
    else:
        raw_list = [test_config.get("test_attachment_path", "test_files/test_document.pdf")]

    resolved: list[str] = []
    for raw in raw_list:
        p = Path(raw)
        if p.exists():
            resolved.append(str(p))
        elif (repo_root / p).exists():
            resolved.append(str(repo_root / p))
        else:
            logger.warning(f"Ek dosya bulunamadı: {raw}")
    return resolved


# ------------------------------------------------------------------ #
#  Subject builder helpers
# ------------------------------------------------------------------ #
SCENARIO_LABELS: dict[str, str] = {
    "plain_text":   "Plain Text",
    "attachment":   "Ek Dosya",
    "inline_image": "Inline Görsel",
    "reply_chain":  "Reply Chain",
    "smime":        "S/MIME İmza",
}

LENGTH_LABELS: dict[str, str] = {
    "short":  "Kısa",
    "medium": "Orta",
    "long":   "Uzun",
}

EXT_LABELS: dict[str, str] = {
    "pdf": "PDF", "csv": "CSV", "txt": "TXT", "docx": "DOCX",
    "xlsx": "XLSX", "png": "PNG", "jpg": "JPG", "jpeg": "JPG",
    "zip": "ZIP", "eml": "EML",
}


def _format_file_size(total_bytes: int) -> str:
    if total_bytes < 1024:
        return f"{total_bytes}B"
    if total_bytes < 1024 * 1024:
        return f"{total_bytes / 1024:.1f}KB"
    return f"{total_bytes / (1024 * 1024):.1f}MB"


def _attachment_tag(paths: list[str]) -> str:
    """Ek dosya bilgisini özet olarak döndürür. Ör: '3 Ek: PDF+CSV+TXT, 1.2MB'"""
    if not paths:
        return "Eksiz"
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        return "Eksiz"
    exts = []
    total_size = 0
    for p in existing:
        ext = Path(p).suffix.lstrip(".").lower()
        label = EXT_LABELS.get(ext, ext.upper())
        if label not in exts:
            exts.append(label)
        total_size += os.path.getsize(p)
    count = len(existing)
    types_str = "+".join(exts)
    size_str = _format_file_size(total_size)
    return f"{count} Ek: {types_str}, {size_str}"


def _inline_image_tag(image_path: str) -> str:
    """Inline görsel bilgisini özet olarak döndürür. Ör: 'Gömülü PNG, 0.5KB'"""
    if not os.path.exists(image_path):
        return "Gömülü Görsel"
    ext = Path(image_path).suffix.lstrip(".").lower()
    label = EXT_LABELS.get(ext, ext.upper())
    size = _format_file_size(os.path.getsize(image_path))
    return f"Gömülü {label}, {size}"


def _build_subject(
    prefix: str,
    run_id: str,
    scenario_key: str,
    length_label: str,
    combo_label: str,
    detail_tag: str,
) -> str:
    """Standart subject satırı üretir.

    Format:
      [AUTO-TEST] #run_id | Senaryo: Plain Text (Eksiz) | Kısa | EMS/iOS←Gmail/Android
    """
    scenario_label = SCENARIO_LABELS.get(scenario_key, scenario_key)
    return (
        f"{prefix} #{run_id} | "
        f"Senaryo: {scenario_label} ({detail_tag}) | "
        f"{length_label} | {combo_label}"
    )


def run_scenario(
    scenario_key: str,
    combo: TestCombination,
    combo_index: int,
    sender: MailSender,
    receiver: MailReceiver,
    analyzer: MailAnalyzer,
    test_config: dict,
) -> dict:
    """Bir senaryo tipini end-to-end çalıştırır.

    ``combo_index`` mesaj şablonu rotasyonu (kısa/orta/uzun) için kullanılır.
    """
    subject_prefix = test_config.get("subject_prefix", "[AUTO-TEST]")
    wait = test_config.get("wait_seconds", 15)
    retries = test_config.get("max_retries", 3)
    retry_int = test_config.get("retry_interval", 5)
    image_path = test_config.get("test_image_path", "test_files/test_image.png")

    attachment_paths = _resolve_attachment_paths(test_config)

    to_address = receiver.config["test_address"]
    run_id = uuid.uuid4().hex[:8]

    tmpl = get_template(scenario_key, combo_index)
    length_label = LENGTH_LABELS.get(tmpl.length, tmpl.length)

    if scenario_key == "attachment":
        detail_tag = _attachment_tag(attachment_paths)
    elif scenario_key == "inline_image":
        detail_tag = _inline_image_tag(image_path)
    elif scenario_key == "reply_chain":
        detail_tag = "Eksiz, Thread Testi"
    elif scenario_key == "smime":
        detail_tag = "Dijital İmzalı"
    else:
        detail_tag = "Eksiz"

    subject = _build_subject(
        subject_prefix, run_id, scenario_key, length_label, combo.label, detail_tag,
    )
    body = f"{tmpl.body}\n\n[Run ID: {run_id}]"

    combination_meta = {
        "sender_server": combo.sender_server,
        "sender_client": combo.sender_client,
        "receiver_server": combo.receiver_server,
        "receiver_client": combo.receiver_client,
    }

    send_meta = None
    test_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info(
        f"▶ Senaryo: {scenario_key} ({tmpl.length}) | "
        f"Kombo: {combo.label} | run_id={run_id}"
    )

    try:
        if scenario_key == "plain_text":
            send_meta = sender.send_plain_text(to_address, subject, body)

        elif scenario_key == "attachment":
            send_meta = sender.send_with_attachment(
                to_address, subject, body, attachment_paths or "test_files/test_document.pdf",
            )

        elif scenario_key == "inline_image":
            html_body = resolve_inline_html(tmpl.body, "{{CID}}")
            send_meta = sender.send_inline_image(
                to_address, subject, image_path, html_body=html_body,
            )

        elif scenario_key == "smime":
            cert = test_config.get("smime_cert_path", "")
            key = test_config.get("smime_key_path", "")
            if cert and key and os.path.exists(cert) and os.path.exists(key):
                send_meta = sender.send_smime_signed(to_address, subject, body, cert, key)
            else:
                logger.warning("S/MIME sertifikası bulunamadı, senaryo atlanıyor.")
                return {
                    "combination": combo.label,
                    "scenario_type": "İmzalı Mesaj (S/MIME / PGP)",
                    "scenario_key": scenario_key,
                    "test_time": test_time,
                    "analysis": {
                        "passed": None,
                        "confidence": "N/A",
                        "checks": [],
                        "summary": "S/MIME sertifikası yapılandırılmamış — manuel test gerekli.",
                        "issues": ["smime_cert_path ve smime_key_path config.yaml'da tanımlı değil"],
                        "recommendations": ["config.yaml'a geçerli test sertifikası ekleyin"],
                    },
                    "skipped": True,
                }

        elif scenario_key == "reply_chain":
            orig_tmpl = get_reply_original(combo_index)
            orig_length = LENGTH_LABELS.get(orig_tmpl.length, orig_tmpl.length)
            orig_subject = _build_subject(
                subject_prefix, f"ORIG-{run_id}", "reply_chain",
                orig_length, combo.label, "Thread Başlangıç, Eksiz",
            )
            orig_body = f"{orig_tmpl.body}\n\n[Run ID: {run_id}]"

            original_meta = sender.send_plain_text(to_address, orig_subject, orig_body)
            time.sleep(5)
            send_meta = sender.send_reply(
                to_address,
                orig_subject,
                original_meta.get("msg_id", ""),
                "",
                body,
            )

        else:
            raise ValueError(f"Bilinmeyen senaryo tipi: {scenario_key}")

    except Exception as e:
        logger.error(f"Gönderim hatası ({scenario_key}): {e}", exc_info=True)
        return {
            "combination": combo.label,
            "scenario_type": scenario_key,
            "scenario_key": scenario_key,
            "test_time": test_time,
            "analysis": {
                "passed": False,
                "confidence": "HIGH",
                "checks": [{"name": "Gönderim", "passed": False, "detail": str(e)}],
                "summary": f"Mesaj gönderilemedi: {e}",
                "issues": [str(e)],
                "recommendations": ["SMTP bağlantı ayarlarını kontrol edin"],
            },
        }

    if send_meta and send_meta.get("skipped"):
        return {
            "combination": combo.label,
            "scenario_type": scenario_key,
            "scenario_key": scenario_key,
            "test_time": test_time,
            "analysis": {
                "passed": None,
                "confidence": "N/A",
                "checks": [],
                "summary": send_meta.get("skip_reason", "Atlandı"),
                "issues": [],
                "recommendations": [],
            },
            "skipped": True,
        }

    received = receiver.wait_for_message(
        expected_msg_id=send_meta["msg_id"],
        subject_prefix=subject_prefix,
        wait_seconds=wait,
        max_retries=retries,
        retry_interval=retry_int,
    )

    analysis = analyzer.analyze(
        scenario_key, send_meta, received, combination_meta
    )

    status = "✅ PASS" if analysis.get("passed") else "❌ FAIL"
    logger.info(
        f"◼ Sonuç: {status} | {combo.label} / {scenario_key} ({tmpl.length}) "
        f"| confidence={analysis.get('confidence')}"
    )

    return {
        "combination": combo.label,
        "scenario_type": combo.scenarios.get(
            scenario_key,
            type("", (), {"scenario_type": scenario_key}),
        ).scenario_type if scenario_key in combo.scenarios else scenario_key,
        "scenario_key": scenario_key,
        "message_length": tmpl.length,
        "test_time": test_time,
        "send_meta": {k: v for k, v in send_meta.items() if k not in ("raw_bytes",)},
        "received": bool(received),
        "analysis": analysis,
    }


# ------------------------------------------------------------------ #
#  Ana çalıştırıcı
# ------------------------------------------------------------------ #
def main():
    parser = argparse.ArgumentParser(description="Mail Otomasyon Testi")
    parser.add_argument("--config", default="config.yaml", help="Config dosyası yolu")
    parser.add_argument("--csv", default=None, help="Test CSV dosyası yolu")
    parser.add_argument("--combo", type=int, default=None, help="Sadece N. kombinasyonu çalıştır (0-index)")
    parser.add_argument("--scenario", default=None,
                        help="Sadece belirtilen senaryo tipini çalıştır "
                             "(plain_text|attachment|inline_image|smime|reply_chain)")
    parser.add_argument("--dry-run", action="store_true", help="Bağlantı testi, mail gönderme")
    args = parser.parse_args()

    # Config yükle
    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    setup_logging(config)
    logger.info("=" * 60)
    logger.info("Mail Otomasyon Başlıyor")
    logger.info("=" * 60)

    test_config = config.get("test", {})
    csv_path = args.csv or test_config.get("csv_input", "mail_test_checklist.csv")
    csv_path = resolve_csv_path(csv_path)

    # Test dosyalarını hazırla
    prepare_test_files()

    # CSV oku
    combinations = parse_csv(csv_path)
    if not combinations:
        logger.error("CSV'den test senaryosu okunamadı!")
        sys.exit(1)

    # Filtrele
    if args.combo is not None:
        if args.combo >= len(combinations):
            logger.error(f"Kombinasyon indeksi {args.combo} geçersiz (toplam: {len(combinations)})")
            sys.exit(1)
        combinations = [combinations[args.combo]]
        logger.info(f"Sadece kombinasyon #{args.combo} çalıştırılıyor: {combinations[0].label}")

    # Dry-run
    if args.dry_run:
        logger.info("DRY RUN — Bağlantı testleri:")
        for combo in combinations:
            for server_name in {combo.sender_server, combo.receiver_server}:
                try:
                    sc = get_server_config(config, server_name)
                    sender = MailSender(sc)
                    conn = sender._connect()
                    conn.quit()
                    logger.info(f"  ✅ SMTP OK: {server_name} ({sc['smtp_host']})")
                except Exception as e:
                    logger.error(f"  ❌ SMTP FAIL: {server_name} — {e}")
        return

    # Analiz için Claude
    anthropic_cfg = config.get("anthropic", {})
    analyzer = MailAnalyzer(api_key=anthropic_cfg.get("api_key", ""))

    all_results = []
    Path("reports").mkdir(exist_ok=True)

    for combo_idx, combo in enumerate(combinations):
        logger.info(f"\n{'='*60}")
        logger.info(f"Kombinasyon [{combo_idx+1}/{len(combinations)}]: {combo.label}")
        logger.info(f"{'='*60}")

        # Sender/Receiver kurulumu
        try:
            sender_config = get_server_config(config, combo.sender_server)
            receiver_config = get_server_config(config, combo.receiver_server)
        except ValueError as e:
            logger.error(f"Config hatası: {e} — kombinasyon atlanıyor.")
            continue

        sender = MailSender(sender_config)
        receiver = MailReceiver(receiver_config)

        scenarios_to_run = list(combo.scenarios.keys())
        if args.scenario:
            scenarios_to_run = [s for s in scenarios_to_run if s == args.scenario]

        for sc_key in scenarios_to_run:
            try:
                result = run_scenario(
                    sc_key, combo, combo_idx, sender, receiver, analyzer, test_config,
                )
                all_results.append(result)
                time.sleep(3)
            except Exception as e:
                logger.error(f"Beklenmeyen hata ({sc_key}): {e}", exc_info=True)

    # Raporla
    if all_results:
        html_path = test_config.get("report_output", "reports/test_report.html")
        csv_path_out = test_config.get("results_csv", "reports/test_results.csv")
        generate_html_report(all_results, html_path)
        generate_csv_results(all_results, csv_path_out)
        logger.info(f"\n{'='*60}")
        logger.info(f"TEST TAMAMLANDI")
        total = len(all_results)
        passed = sum(1 for r in all_results if r.get("analysis", {}).get("passed"))
        logger.info(f"Sonuç: {passed}/{total} PASS ({round(passed/total*100,1) if total else 0}%)")
        logger.info(f"HTML Rapor: {html_path}")
        logger.info(f"CSV Sonuçlar: {csv_path_out}")
        logger.info(f"{'='*60}")
    else:
        logger.warning("Hiçbir test sonucu üretilemedi.")


if __name__ == "__main__":
    main()
