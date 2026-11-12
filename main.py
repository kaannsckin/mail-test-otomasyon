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
import re
import signal
import smtplib
import sys
import time
import uuid
import yaml
from datetime import datetime
from email.utils import formatdate, make_msgid
from pathlib import Path

# Proje modülleri
from csv_parser import parse_csv, TestCombination, SUPPORTED_SCENARIOS
from mail_transport import explain_connection_error, resolve_path
from sender import MailSender
from receiver import MailReceiver
from analyzer import MailAnalyzer
from reporter import generate_html_report, generate_csv_results
from message_templates import (
    get_template,
    get_reply_original,
    resolve_inline_html,
    resolve_html,
    html_to_plain,
    HTML_SCENARIOS,
    SIGNATURE_HTML,
)

# Çıkış kodları: CI/zamanlanmış koşular ve web arayüzü sonucu ayırt edebilsin.
EXIT_OK = 0              # tüm (atlanmayan) senaryolar PASS
EXIT_ERROR = 1           # araç/config hatası ya da hiç sonuç üretilemedi
EXIT_TEST_FAILURES = 2   # koşu tamamlandı, en az bir senaryo FAIL
EXIT_INTERRUPTED = 130   # Ctrl-C / arayüzden durdurma (kısmi rapor yazılır)

# prepare_test_files() tarafından üretilen ek örnek dosyalar — çoklu ek
# senaryosunun tek dosyayla anlamsızlaşmaması için.
EXTRA_TEST_FILES = ("test_files/test_data.csv", "test_files/test_notes.txt")

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
    log_cfg = config.get("logging") or {}
    log_file = Path(str(log_cfg.get("file") or "logs/automation.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, str(log_cfg.get("level") or "INFO").upper(), None)
    if not isinstance(level, int):
        level = logging.INFO

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


def load_config(path: str) -> dict:
    """config.yaml'ı okur; boş bölümleri {} yapar (``test:`` altı boş kalsa da
    çökmesin). Kök sözlük değilse ValueError."""
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        raise ValueError("config.yaml kökü 'anahtar: değer' yapısında olmalı")
    return {k: (v if v is not None else {}) for k, v in config.items()}


def _num(value, default, cast=int):
    """Config'den sayı oku: '15' (metin) da kabul edilir, bozuksa varsayılan."""
    try:
        return cast(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        logger.warning(f"Geçersiz sayısal config değeri {value!r} — varsayılan {default} kullanılıyor")
        return default


def _is_auth_failure(exc: BaseException) -> bool:
    """Kalıcı kimlik reddi mi? Yalnızca 5xx (ör. 535) — 4xx (ör. 454 "temporary
    authentication failure") geçicidir; eskiden sunucuyu koşu boyunca kilitleyip
    kalan senaryoları denemeden FAIL sayıyordu."""
    err = exc
    while err is not None:
        if isinstance(err, smtplib.SMTPAuthenticationError):
            code = getattr(err, "smtp_code", None)
            return not (isinstance(code, int) and 400 <= code < 500)
        err = err.__cause__ or err.__context__
    return False


# Giden SMTP portu kesildiğinde ortaya çıkan hatalar. Güvenlik duvarı paketi
# REJECT ederse ECONNREFUSED/ENETUNREACH gelir; DROP ederse (kurumsal ağlarda
# en yaygın davranış) hiçbir errno gelmez, bağlantı zaman aşımına uğrar.
_BLOCKED_PORT_ERRNOS = (
    97,   # EAFNOSUPPORT — adres ailesi desteklenmiyor (kısıtlı konteyner/sandbox)
    101,  # ENETUNREACH  — ağa erişilemiyor
    110,  # ETIMEDOUT    — bağlantı zaman aşımı
    111,  # ECONNREFUSED — bağlantı reddedildi
    113,  # EHOSTUNREACH — host'a erişilemiyor
)

_BLOCKED_PORT_TEXTS = (
    "network is unreachable", "no route to host", "timed out", "timeout",
    "connection refused", "address family not supported",
)


def _is_network_unreachable(exc: Exception) -> bool:
    """Giden SMTP portu engelli ya da erişilemez mi?

    Yalnızca gönderim/bağlantı hatalarında çağrılır. Zaman aşımı da yakalanır:
    paketi DROP eden güvenlik duvarları errno üretmez, sadece bekletir — bu
    durumda kullanıcı teşhis mesajı yerine anlamsız bir 'timed out' görüyordu.
    """
    err = exc
    while err is not None:
        if isinstance(err, OSError):
            if getattr(err, "errno", None) in _BLOCKED_PORT_ERRNOS:
                return True
            if isinstance(err, TimeoutError):   # socket.timeout errno taşımaz
                return True
        err = err.__cause__ or err.__context__
    text = str(exc).lower()
    return any(t in text for t in _BLOCKED_PORT_TEXTS)


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

    # Çoklu ek senaryosu için farklı MIME tiplerinde küçük dosyalar
    csv_path = "test_files/test_data.csv"
    if not os.path.exists(csv_path):
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("id,ürün,adet,açıklama\n"
                    "1,Şarj Kablosu,12,Türkçe karakter testi ğüşıöç\n"
                    "2,Güç Kaynağı,3,İkinci satır\n")
        logger.debug(f"Test CSV oluşturuldu: {csv_path}")

    txt_path = "test_files/test_notes.txt"
    if not os.path.exists(txt_path):
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("Mail Otomasyon — Test Notları\n"
                    "================================\n"
                    "Bu dosya çoklu ek senaryosu için üretilmiştir.\n"
                    "Karakter sınaması: ğüşıöç ĞÜŞİÖÇ\n")
        logger.debug(f"Test TXT oluşturuldu: {txt_path}")


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
    "plain_text":       "Plain Text",
    "attachment":       "Ek Dosya",
    "inline_image":     "Inline Görsel",
    "reply_chain":      "Reply Chain",
    "smime":            "S/MIME İmza",
    "calendar_invite":  "Takvim Daveti",
    "i18n":             "Uluslararası Alfabe",
    "complex_html":     "Zengin HTML",
    "html_table":       "HTML Tablo",
    "forward":          "Mesaj İletme",
    "forward_chain":    "Mesaj İletme",
    "multi_attachment": "Çoklu Ek",
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


def _resolve_multi_attachment_paths(test_config: dict) -> list[str]:
    """Çoklu ek senaryosu için dosya listesi.

    Öncelik ``test_multi_attachment_paths``; tanımlı değilse normal ek
    yolları + prepare_test_files()'ın ürettiği örnek dosyalar kullanılır.
    Senaryonun anlamlı olması için en az iki farklı dosya hedeflenir.
    """
    repo_root = Path(__file__).parent

    explicit = test_config.get("test_multi_attachment_paths")
    if explicit and isinstance(explicit, list):
        resolved = []
        for raw in explicit:
            p = Path(raw)
            if p.exists():
                resolved.append(str(p))
            elif (repo_root / p).exists():
                resolved.append(str(repo_root / p))
            else:
                logger.warning(f"Çoklu ek dosyası bulunamadı: {raw}")
        return resolved

    paths = list(_resolve_attachment_paths(test_config))
    for extra in EXTRA_TEST_FILES:
        # prepare_test_files() dosyaları çalışma dizinine (web arayüzünde
        # DATA_DIR) yazar; repo kökü yalnızca yedek.
        for candidate in (Path(extra), repo_root / extra):
            if candidate.exists():
                if str(candidate) not in paths:
                    paths.append(str(candidate))
                break
    return paths


_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def _stamp_html_run_id(html: str, run_id: str) -> str:
    """HTML gövdeye Run ID'yi </body> öncesine iliştirir.

    Düz metinde olduğu gibi sona eklemek </html>'den sonra metin bırakır ve
    bazı istemcilerde görünmez; bu yüzden gövdenin içine yazılır.

    Konum aramasında ``str.lower()`` KULLANILMAZ: Türkçe 'İ' küçültüldüğünde
    iki karaktere ('i' + U+0307) açılır, dizgenin boyu değişir ve küçük harfli
    kopyada bulunan indeks orijinal metinde kayar (kapanış etiketinin '<'
    karakteri yenirdi). Regex doğrudan orijinal metin üzerinde çalışır.
    """
    stamp = (f"<p style=\"font-size:11px;color:#999;margin-top:16px\">"
             f"[Run ID: {run_id}]</p>")
    matches = list(_BODY_CLOSE_RE.finditer(html))
    if matches:
        idx = matches[-1].start()
        return html[:idx] + stamp + html[idx:]
    return html + stamp


def _skipped_result(combo: TestCombination, scenario_key: str,
                    test_time: str, reason: str) -> dict:
    """Senaryo çalıştırılamadığında PASS/FAIL yerine SKIP sonucu üretir."""
    return {
        "combination": combo.label,
        "scenario_type": scenario_key,
        "scenario_key": scenario_key,
        "test_time": test_time,
        "analysis": {
            "passed": None,
            "confidence": "N/A",
            "checks": [],
            "summary": reason,
            "issues": [],
            "recommendations": [],
        },
        "skipped": True,
    }


def _error_result(combo: TestCombination, scenario_key: str, test_time: str,
                  summary: str, detail: str, recommendations: list | None = None,
                  check_name: str = "Çalıştırma") -> dict:
    """Senaryo yarıda kaldığında FAIL sonucu — sonuç ASLA sessizce düşmez."""
    return {
        "combination": combo.label,
        "scenario_type": scenario_key,
        "scenario_key": scenario_key,
        "test_time": test_time,
        "analysis": {
            "passed": False,
            "confidence": "HIGH",
            "checks": [{"name": check_name, "passed": False, "detail": detail}],
            "summary": summary,
            "issues": [detail],
            "recommendations": recommendations or [],
        },
    }


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
    subject_prefix = str(test_config.get("subject_prefix") or "[AUTO-TEST]")
    wait = _num(test_config.get("wait_seconds"), 15)
    retries = max(1, _num(test_config.get("max_retries"), 3))
    retry_int = _num(test_config.get("retry_interval"), 5)
    image_path = test_config.get("test_image_path", "test_files/test_image.png")

    attachment_paths = _resolve_attachment_paths(test_config)

    to_address = receiver.config["test_address"]
    run_id = uuid.uuid4().hex[:8]

    tmpl = get_template(scenario_key, combo_index)
    length_label = LENGTH_LABELS.get(tmpl.length, tmpl.length)

    if scenario_key == "attachment":
        detail_tag = _attachment_tag(attachment_paths)
    elif scenario_key == "multi_attachment":
        multi_paths = _resolve_multi_attachment_paths(test_config)
        detail_tag = _attachment_tag(multi_paths)
    elif scenario_key == "inline_image":
        detail_tag = _inline_image_tag(image_path)
    elif scenario_key == "reply_chain":
        detail_tag = "Eksiz, Thread Testi"
    elif scenario_key == "smime":
        detail_tag = "Dijital İmzalı"
    elif scenario_key == "calendar_invite":
        detail_tag = "iTIP Daveti, ICS Ekli"
    elif scenario_key == "i18n":
        # Başlığın kendisi test edilen şey: ASCII dışı karakterler subject
        # header'ında RFC 2047 ile kodlanmalı. Bu yüzden şablonun emoji/çok
        # alfabeli etiketi doğrudan konuya taşınır.
        detail_tag = tmpl.subject_tag
    elif scenario_key == "complex_html":
        detail_tag = "Zengin CSS + Media Query"
    elif scenario_key == "html_table":
        detail_tag = "HTML Tablo Yapısı"
    elif scenario_key in ("forward", "forward_chain"):
        detail_tag = "İletilmiş Mesaj, RFC822 Kapsüllü"
    else:
        detail_tag = "Eksiz"

    subject = _build_subject(
        subject_prefix, run_id, scenario_key, length_label, combo.label, detail_tag,
    )
    if scenario_key in HTML_SCENARIOS:
        html_body = resolve_html(tmpl.body)
        body = _stamp_html_run_id(html_body, run_id)
    else:
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
            cert = str(resolve_path(test_config.get("smime_cert_path", "")) or "")
            key = str(resolve_path(test_config.get("smime_key_path", "")) or "")
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

        elif scenario_key == "multi_attachment":
            paths = _resolve_multi_attachment_paths(test_config)
            if not paths:
                return _skipped_result(
                    combo, scenario_key, test_time,
                    "Çoklu ek senaryosu için en az bir dosya bulunamadı.",
                )
            send_meta = sender.send_with_attachment(to_address, subject, body, paths)
            send_meta["scenario"] = "multi_attachment"

        elif scenario_key in HTML_SCENARIOS:
            send_meta = sender.send_html_message(
                to_address, subject, body, html_to_plain(body), scenario=scenario_key,
            )

        elif scenario_key == "i18n":
            send_meta = sender.send_i18n(to_address, subject, body)

        elif scenario_key == "calendar_invite":
            send_meta = sender.send_calendar_invite(
                to_address, subject, body,
                summary=tmpl.subject_tag,
                duration_minutes=int(test_config.get("calendar_duration_minutes", 30)),
                location=test_config.get("calendar_location", "Çevrimiçi Toplantı"),
            )

        elif scenario_key in ("forward", "forward_chain"):
            # Reply chain gibi: önce gerçek bir orijinal mesaj gönderilir,
            # sonra o mesaj message/rfc822 olarak kapsüllenip iletilir.
            orig_tmpl = get_reply_original(combo_index)
            orig_length = LENGTH_LABELS.get(orig_tmpl.length, orig_tmpl.length)
            orig_subject = _build_subject(
                subject_prefix, f"ORIG-{run_id}", scenario_key,
                orig_length, combo.label, "İletme Kaynağı, Eksiz",
            )
            orig_body = f"{orig_tmpl.body}\n\n[Run ID: {run_id}]"

            original_meta = sender.send_plain_text(to_address, orig_subject, orig_body)
            time.sleep(5)
            send_meta = sender.send_forward(
                to_address, orig_subject, body,
                {
                    "subject": orig_subject,
                    "from": sender.from_address,
                    "to": to_address,
                    "date": formatdate(localtime=True),
                    "msg_id": original_meta.get("msg_id", ""),
                    "body": orig_body,
                },
            )

        else:
            raise ValueError(f"Bilinmeyen senaryo tipi: {scenario_key}")

    except Exception as e:
        logger.error(f"Gönderim hatası ({scenario_key}): {e}", exc_info=True)
        hint = explain_connection_error(e, getattr(sender, "config", None))
        if _is_auth_failure(e):
            summary = f"SMTP kimlik doğrulaması reddedildi: {e}"
            recommendations = [hint or "Kullanıcı adı/şifreyi kontrol edin",
                               "Hesap kilitlenmesin diye bu sunucunun kalan senaryoları denenmeyecek."]
            result = _error_result(combo, scenario_key, test_time, summary, str(e),
                                   recommendations, check_name="Gönderim")
            result["auth_failed_server"] = combo.sender_server
            return result
        if _is_network_unreachable(e):
            target = ""
            sc = getattr(sender, "config", None)
            if isinstance(sc, dict) and sc.get("smtp_host"):
                target = f" ({sc.get('smtp_host')}:{sc.get('smtp_port')})"
            summary = (f"SMTP bağlantısı kurulamadı{target}. "
                       + (f"{hint} " if hint else "")
                       + "Diğer olasılık: giden SMTP portuna erişim ağ/platform tarafından engelli.")
            recommendations = [
                "Önce host/port ve TLS modunu (587 STARTTLS, 465 SSL) doğrulayın; "
                "sunucunun açık olduğundan emin olun.",
                "Render/Heroku ücretsiz planları, Railway Hobby planı ve bazı VPS "
                "sağlayıcıları (yeni hesaplarda) giden SMTP'yi engeller.",
                "Kurumsal ağlarda 587/465 güvenlik duvarında kapalı olabilir — "
                "bağlantı sessizce zaman aşımına uğrar; ağ yöneticinize danışın.",
                "SMTP çıkışı açık bir host kullanın (Railway Pro, Fly.io, portu açık "
                "bir VPS) ya da uygulamayı yerel makinede çalıştırın.",
                "Teşhis için /api/diagnostics/network adresini açın.",
            ]
        else:
            summary = f"Mesaj gönderilemedi: {e}" + (f" — {hint}" if hint else "")
            recommendations = ([hint] if hint else []) + ["SMTP bağlantı ayarlarını kontrol edin"]
        return {
            "combination": combo.label,
            "scenario_type": scenario_key,
            "scenario_key": scenario_key,
            "test_time": test_time,
            "analysis": {
                "passed": False,
                "confidence": "HIGH",
                "checks": [{"name": "Gönderim", "passed": False, "detail": str(e)}],
                "summary": summary,
                "issues": [str(e)],
                "recommendations": recommendations,
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

    if isinstance(send_meta, dict) and send_meta.get("error"):
        # Gönderici hata sözlüğü döndürdü (ör. S/MIME imzalama başarısız): mesaj
        # hiç gitmedi. Eskiden tam bekleme süresi + tüm denemeler boyunca
        # beklenip "teslim edilemedi — timeout" deniyor, gerçek hata kayboluyordu.
        err = str(send_meta["error"]).strip()
        logger.info(f"◼ Sonuç: ❌ FAIL | {combo.label} / {scenario_key} | gönderilemedi")
        return _error_result(
            combo, scenario_key, test_time,
            f"Mesaj gönderilmedi: {err[:300]}", err,
            ["Gönderici loglarını inceleyin (ör. S/MIME sertifika/anahtar yolları)."],
            check_name="Gönderim")

    received = receiver.wait_for_message(
        expected_msg_id=send_meta["msg_id"],
        subject_prefix=subject_prefix,
        wait_seconds=wait,
        max_retries=retries,
        retry_interval=retry_int,
    )

    receive_error = getattr(receiver, "last_error", None) if received is None else None
    if isinstance(receive_error, BaseException):
        # Mesaj gelmiş olabilir ama kontrol edilemedi — "teslim edilemedi
        # (timeout)" demek yanıltıcı olurdu.
        hint = explain_connection_error(receive_error, getattr(receiver, "config", None))
        result = _error_result(
            combo, scenario_key, test_time,
            f"Alıcı posta kutusu (IMAP) kontrol edilemedi: {receive_error}"
            + (f" — {hint}" if hint else ""),
            str(receive_error),
            ([hint] if hint else []) + ["IMAP host/port/SSL ve kimlik bilgilerini kontrol edin"],
            check_name="Mesaj Alımı (IMAP)",
        )
        result["send_meta"] = {k: v for k, v in send_meta.items() if k != "raw_bytes"}
        is_auth = getattr(receiver, "is_auth_error", None)
        if callable(is_auth) and is_auth(receive_error) is True:
            result["auth_failed_server"] = combo.receiver_server
        logger.info(f"◼ Sonuç: ❌ FAIL | {combo.label} / {scenario_key} | IMAP hatası")
        return result

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
def _combo_list(value: str) -> list[int]:
    """'3' → [3], '1,4,7' → [1, 4, 7] (sırası korunur, tekrarlar atılır)."""
    out: list[int] = []
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            raise argparse.ArgumentTypeError(f"geçersiz kombinasyon indeksi: {part!r}")
        if n < 0:
            raise argparse.ArgumentTypeError(f"kombinasyon indeksi negatif olamaz: {n}")
        if n not in out:
            out.append(n)
    if not out:
        raise argparse.ArgumentTypeError("en az bir kombinasyon indeksi gerekli")
    return out


def main():
    parser = argparse.ArgumentParser(description="Mail Otomasyon Testi")
    parser.add_argument("--config", default="config.yaml", help="Config dosyası yolu")
    parser.add_argument("--csv", default=None, help="Test CSV dosyası yolu")
    parser.add_argument("--combo", type=_combo_list, default=None,
                        help="Yalnızca bu kombinasyon(lar)ı çalıştır (0-index): 3 veya 1,4,7")
    parser.add_argument("--scenario", default=None,
                        help="Sadece belirtilen senaryo tipini çalıştır "
                             "(plain_text|attachment|inline_image|smime|reply_chain)")
    parser.add_argument("--dry-run", action="store_true", help="Bağlantı testi, mail gönderme")
    args = parser.parse_args()

    # Config yükle
    if not os.path.exists(args.config):
        print(f"[HATA] Config dosyası bulunamadı: {args.config}")
        print("Web arayüzünden config.yaml oluşturun veya --config ile dosya belirtin.")
        sys.exit(EXIT_ERROR)
    try:
        config = load_config(args.config)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"[HATA] Config okunamadı: {e}")
        sys.exit(EXIT_ERROR)

    setup_logging(config)
    logger.info("=" * 60)
    logger.info("Mail Otomasyon Başlıyor")
    logger.info("=" * 60)

    test_config = config.get("test") or {}
    if args.csv:
        explicit = Path(args.csv)
        if not explicit.exists() and not (Path(__file__).parent / explicit).exists():
            logger.error(f"CSV dosyası bulunamadı: {args.csv}")
            sys.exit(EXIT_ERROR)
        csv_path = resolve_csv_path(args.csv)
    else:
        configured = str(test_config.get("csv_input") or "mail_test_checklist.csv")
        csv_path = resolve_csv_path(configured)
        if Path(csv_path).name != Path(configured).name:
            logger.warning(f"CSV bulunamadı: {configured} — yerine {csv_path} kullanılıyor")

    # Test dosyalarını hazırla
    prepare_test_files()

    # CSV oku
    try:
        combinations = parse_csv(csv_path)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        logger.error(f"CSV okunamadı ({csv_path}): {e}")
        sys.exit(EXIT_ERROR)
    if not combinations:
        logger.error("CSV'den test senaryosu okunamadı!")
        sys.exit(EXIT_ERROR)
    if not any(c.scenarios for c in combinations):
        logger.error(
            f"CSV'de {len(combinations)} kombinasyon bulundu ama hiç senaryo satırı okunamadı. "
            "Dosya ayracını (virgül/noktalı virgül) ve sütun başlıklarını kontrol edin.")
        sys.exit(EXIT_ERROR)

    # Filtrele — orijinal indeks korunur (şablon rotasyonu kombinasyona bağlı)
    indexed = list(enumerate(combinations))
    if args.combo is not None:
        invalid = [i for i in args.combo if i >= len(combinations)]
        if invalid:
            logger.error(f"Kombinasyon indeksi {invalid} geçersiz (toplam: {len(combinations)})")
            sys.exit(EXIT_ERROR)
        indexed = [(i, combinations[i]) for i in args.combo]
        for i, c in indexed:
            logger.info(f"Seçili kombinasyon #{i}: {c.label}")
    combinations = [c for _, c in indexed]

    # Arayüzdeki "Durdur" SIGTERM gönderir: Ctrl-C gibi ele alınır ki finally
    # blokları (2FA köprüsü temizliği) çalışsın ve o ana kadarki sonuçlar
    # raporlansın. Dry-run dahil tüm modlarda geçerli.
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass  # ana thread değil (testler) — yoksay

    # Dry-run
    if args.dry_run:
        try:
            _dry_run(config, combinations)
        except KeyboardInterrupt:
            logger.warning("⏹ Dry-run durduruldu.")
            sys.exit(EXIT_INTERRUPTED)
        return

    _run_all(args, config, test_config, indexed)


def _dry_run(config: dict, combinations: list) -> None:
    logger.info("DRY RUN — Bağlantı testleri (mail gönderilmez):")
    failures = 0
    checked = set()
    for combo in combinations:
        for server_name in (combo.sender_server, combo.receiver_server):
            if server_name in checked:
                continue
            checked.add(server_name)
            try:
                sc = get_server_config(config, server_name)
            except ValueError as e:
                failures += 1
                logger.error(f"  ❌ {server_name}: {e}")
                continue
            for proto, connect in (("SMTP", lambda: MailSender(sc)._connect().quit()),
                                   ("IMAP", lambda: MailReceiver(sc)._connect().logout())):
                host = sc.get("smtp_host" if proto == "SMTP" else "imap_host")
                try:
                    connect()
                    logger.info(f"  ✅ {proto} OK: {server_name} ({host})")
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    logger.error(f"  ❌ {proto} FAIL: {server_name} ({host}) — {e}")
                    hint = explain_connection_error(e, sc)
                    if hint:
                        logger.error(f"     ↳ {hint}")
                    if proto == "SMTP" and _is_network_unreachable(e):
                        logger.error(
                            "     ↳ Giden SMTP portuna (587/465) ulaşılamıyor — "
                            "bağlantı engelleniyor veya zaman aşımına uğruyor. "
                            "Ücretsiz PaaS'lar (Render/Heroku), bazı VPS sağlayıcıları ve "
                            "kurumsal güvenlik duvarları bu portları kapatır. "
                            "Teşhis: /api/diagnostics/network adresini açın."
                        )
    if failures:
        logger.error(f"DRY RUN: {failures} bağlantı başarısız.")
        sys.exit(EXIT_TEST_FAILURES)
    logger.info("DRY RUN: tüm bağlantılar başarılı.")


def _run_all(args, config: dict, test_config: dict, indexed: list) -> None:
    # Analiz için LLM — analysis.provider: claude (varsayılan) | gemini
    provider = (str((config.get("analysis") or {}).get("provider") or "claude")).lower()
    if provider == "gemini":
        gemini_cfg = config.get("gemini") or {}
        analyzer = MailAnalyzer(
            api_key=gemini_cfg.get("api_key", ""),
            model=gemini_cfg.get("model") or None,
            provider="gemini",
        )
    else:
        anthropic_cfg = config.get("anthropic") or {}
        analyzer_kwargs = {"api_key": anthropic_cfg.get("api_key", ""),
                           "model": anthropic_cfg.get("model") or None}
        if anthropic_cfg.get("effort"):
            analyzer_kwargs["effort"] = anthropic_cfg["effort"]
        analyzer = MailAnalyzer(**analyzer_kwargs)
    logger.info(f"Analiz sağlayıcısı: {provider} ({analyzer.model})")

    all_results = []
    auth_failed: dict[str, str] = {}   # sunucu → ilk kimlik hatası
    interrupted = False
    Path("reports").mkdir(exist_ok=True)

    try:
        for combo_idx, combo in indexed:
            logger.info(f"\n{'='*60}")
            logger.info(f"Kombinasyon [#{combo_idx}]: {combo.label}")
            logger.info(f"{'='*60}")

            scenarios_to_run = list(combo.scenarios.keys())
            if args.scenario:
                scenarios_to_run = [s for s in scenarios_to_run if s == args.scenario]

            # Sender/Receiver kurulumu
            try:
                sender_config = get_server_config(config, combo.sender_server)
                receiver_config = get_server_config(config, combo.receiver_server)
                sender = MailSender(sender_config)
                receiver = MailReceiver(receiver_config)
            except (ValueError, KeyError, TypeError) as e:
                # Kombinasyon çalıştırılamıyor — senaryoları sessizce düşmesin,
                # her biri FAIL satırı olarak rapora ve çıkış koduna yansısın.
                logger.error(f"Config hatası: {e} — kombinasyonun senaryoları FAIL sayılıyor.")
                test_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for sc_key in scenarios_to_run:
                    all_results.append(_error_result(
                        combo, sc_key, test_time,
                        f"Kombinasyon çalıştırılamadı — config hatası: {e}",
                        f"Config hatası: {e}",
                        ["config.yaml'da bu kombinasyonun gönderici/alıcı sunucu "
                         "bölümlerini (smtp_host, imap_host, test_address) tamamlayın."],
                        check_name="Konfigürasyon"))
                continue

            for sc_key in scenarios_to_run:
                test_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if sc_key not in SUPPORTED_SCENARIOS:
                    logger.warning(f"Desteklenmeyen senaryo tipi atlandı: {sc_key}")
                    all_results.append(_skipped_result(
                        combo, sc_key, test_time,
                        f"CSV'deki senaryo tipi desteklenmiyor: {sc_key}"))
                    continue
                blocked = [srv for srv in (combo.sender_server, combo.receiver_server)
                           if srv in auth_failed]
                if blocked:
                    srv = blocked[0]
                    all_results.append(_error_result(
                        combo, sc_key, test_time,
                        f"{srv} kimlik doğrulaması bu koşuda daha önce reddedildi — "
                        "hesap kilitlenmesin diye yeniden denenmedi.",
                        auth_failed[srv],
                        ["Şifre/uygulama şifresi/2FA ayarlarını düzeltip testi yeniden çalıştırın."],
                        check_name="Kimlik Doğrulama"))
                    continue
                try:
                    result = run_scenario(
                        sc_key, combo, combo_idx, sender, receiver, analyzer, test_config,
                    )
                except Exception as e:  # noqa: BLE001 — sonuç asla sessizce düşmesin
                    logger.error(f"Beklenmeyen hata ({sc_key}): {e}", exc_info=True)
                    result = _error_result(
                        combo, sc_key, test_time,
                        f"Senaryo beklenmeyen bir hatayla yarıda kaldı: {type(e).__name__}: {e}",
                        f"{type(e).__name__}: {e}",
                        ["Logları inceleyin; tekrarlıyorsa hata kaydı açın."])
                failed_srv = result.get("auth_failed_server") if isinstance(result, dict) else None
                if failed_srv and failed_srv not in auth_failed:
                    auth_failed[failed_srv] = (result.get("analysis") or {}).get("summary", "")
                    logger.error(f"🔒 {failed_srv}: kimlik doğrulama reddedildi — bu sunucunun "
                                 "kalan senaryoları denenmeyecek.")
                all_results.append(result)
                time.sleep(3)
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("⏹ Koşu durduruldu — o ana kadarki sonuçlar raporlanıyor.")

    # Raporla (durdurulsa bile)
    if not all_results:
        logger.warning("Hiçbir test sonucu üretilemedi.")
        sys.exit(EXIT_INTERRUPTED if interrupted else EXIT_ERROR)

    html_path = test_config.get("report_output", "reports/test_report.html")
    csv_path_out = test_config.get("results_csv", "reports/test_results.csv")
    note = ("Koşu kullanıcı tarafından DURDURULDU — bu rapor kısmi sonuçları içerir; "
            "çalıştırılmayan senaryolar listede yoktur.") if interrupted else ""
    # Rapor yazılamazsa sonuçlar kaybolur: koşu "başarılı" (çıkış 0) bitmemeli.
    report_errors = []
    for kind, writer, path, kwargs in (("HTML rapor", generate_html_report, html_path, {"note": note}),
                                        ("CSV sonuç", generate_csv_results, csv_path_out, {})):
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            writer(all_results, path, **kwargs)
        except Exception as e:  # noqa: BLE001 — biri bozulsa diğeri yine yazılsın
            logger.error(f"{kind} yazılamadı ({path}): {e}", exc_info=True)
            report_errors.append(kind)
    logger.info(f"\n{'='*60}")
    logger.info("TEST DURDURULDU (kısmi sonuç)" if interrupted else "TEST TAMAMLANDI")
    total = len(all_results)
    passed = sum(1 for r in all_results if (r.get("analysis") or {}).get("passed") is True)
    skipped = sum(1 for r in all_results if (r.get("analysis") or {}).get("passed") is None)
    effective = total - skipped
    failed = effective - passed
    rate = round(passed / effective * 100, 1) if effective else 0
    logger.info(f"Sonuç: {passed}/{effective} PASS ({rate}%) | {skipped} atlandı")
    logger.info(f"HTML Rapor: {'YAZILAMADI — ' if 'HTML rapor' in report_errors else ''}{html_path}")
    logger.info(f"CSV Sonuçlar: {'YAZILAMADI — ' if 'CSV sonuç' in report_errors else ''}{csv_path_out}")
    logger.info(f"{'='*60}")

    if interrupted:
        sys.exit(EXIT_INTERRUPTED)
    if report_errors:
        logger.error(f"{' ve '.join(report_errors)} yazılamadı — sonuçlar kaydedilmedi.")
        sys.exit(EXIT_ERROR)
    if failed:
        sys.exit(EXIT_TEST_FAILURES)

if __name__ == "__main__":
    main()
