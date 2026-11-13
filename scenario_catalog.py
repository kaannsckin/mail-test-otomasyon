"""
scenario_catalog.py — Bildirimsel (spec tabanlı) genişletilmiş test senaryoları.

İlk 11 senaryo sender.py içinde elle yazılmış metotlarla gönderilir ve yalnızca
LLM tarafından değerlendirilir. Buradaki senaryolar farklı çalışır:

  * Her senaryo mesajı kurarken bir **beklenti** (``expect``) üretir: gövdenin
    tam metni, eklerin SHA-256 özeti, korunması gereken başlıklar...
  * ``verifier.py`` alınan ham mesajı bu beklentiyle **deterministik** olarak
    karşılaştırır. Karar LLM'e değil bayt düzeyinde karşılaştırmaya dayanır;
    aynı mesaj her koşuda aynı sonucu verir (regresyon kanıtı için şart).
  * Her senaryo ayrıca alıcı **istemcide** (iOS Mail, Outlook, ...) elle
    bakılacak adımları tanımlar — IMAP ile görülemeyen "ekranda nasıl
    görünüyor" sorusu Manuel Doğrulama sihirbazında bu adımlarla sorulur.

Yeni senaryo eklemek: bir ``_build_*`` fonksiyonu yazıp ``_SPECS`` listesine
``ScenarioSpec`` ekleyin, ardından ``python scripts/extend_checklist.py``
çalıştırın (checklist CSV'sine işler).
"""
from __future__ import annotations

import base64
import hashlib
import os
import quopri
import uuid
from dataclasses import dataclass, field
from email import encoders
from email.charset import QP, Charset
from email.header import Header
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.nonmultipart import MIMENonMultipart
from email.utils import formataddr
from typing import Callable, Optional

TR_CHARS = "ğüşıöç ĞÜŞİÖÇ"


# ────────────────────────────────────────────────────────────────────
#  Veri yapıları
# ────────────────────────────────────────────────────────────────────
@dataclass
class BuildContext:
    """Bir senaryonun mesajını kurmak için orkestratörün verdiği bağlam."""
    from_address: str
    to_address: str
    subject: str
    run_id: str
    test_config: dict = field(default_factory=dict)
    image_path: str = "test_files/test_image.png"
    attachment_paths: list = field(default_factory=list)


@dataclass
class BuiltMessage:
    msg: Message
    expect: dict
    extra_recipients: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioSpec:
    key: str
    csv_name: str            # checklist CSV'sindeki "Senaryo Türü" metni
    label: str               # konu satırı / arayüz etiketi
    group: str               # filtre grubu anahtarı
    detail_tag: str          # konu satırındaki parantez içi açıklama
    build: Callable[[BuildContext], BuiltMessage]
    steps: tuple             # checklist adımları (ilki gönderim eylemi)
    manual_steps: tuple      # alıcı istemcide elle bakılacaklar


GROUP_LABELS = {
    "temel": "Temel",
    "kodlama": "Kodlama / Karakter Seti",
    "oncelik": "Öncelik ve Bayraklar",
    "adresleme": "Adresleme ve Başlıklar",
    "icerik": "İçerik ve Taşıma",
}

# Konu satırında kullanılan kısa grup adı.
GROUP_SHORT = {"temel": "Temel", "kodlama": "Kodlama", "oncelik": "Öncelik",
               "adresleme": "Adresleme", "icerik": "İçerik"}

# Elle yazılmış ilk 11 senaryo (sender.py) — yalnızca gruplama/etiket için.
LEGACY_SCENARIOS = {
    "plain_text": "Plain Text",
    "attachment": "Ek Dosya",
    "multi_attachment": "Çoklu Ek",
    "inline_image": "Inline Görsel",
    "smime": "S/MIME İmza",
    "reply_chain": "Reply Chain",
    "forward": "Mesaj İletme",
    "html_table": "HTML Tablo",
    "complex_html": "Zengin HTML",
    "i18n": "Uluslararası Alfabe",
    "calendar_invite": "Takvim Daveti",
}


# ────────────────────────────────────────────────────────────────────
#  MIME yardımcıları
# ────────────────────────────────────────────────────────────────────
def text_part(text: str, subtype: str = "plain", charset: str = "utf-8",
              cte: str = "base64") -> Message:
    """Charset ve Content-Transfer-Encoding'i AÇIKÇA seçilmiş text/* parçası.

    MIMEText kodlamayı kendisi seçer (utf-8 → base64); kodlama senaryolarında
    test edilen şey tam da bu seçim olduğu için parça elle kurulur.
    """
    part = MIMENonMultipart("text", subtype, charset=charset)
    raw = text.encode(charset)
    if cte == "base64":
        payload = base64.encodebytes(raw).decode("ascii")
    elif cte == "quoted-printable":
        payload = quopri.encodestring(raw).decode("ascii")
    elif cte == "8bit":
        # email paketi ham 8-bit gövdeyi surrogateescape'li str olarak taşır;
        # BytesGenerator kabloya özgün baytları yazar.
        payload = raw.decode("ascii", "surrogateescape")
    elif cte == "7bit":
        payload = raw.decode("ascii")
    else:  # pragma: no cover — programlama hatası
        raise ValueError(f"Bilinmeyen CTE: {cte}")
    part["Content-Transfer-Encoding"] = cte
    part.set_payload(payload)
    return part


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stamp(body: str, run_id: str) -> str:
    return f"{body}\n\n[Run ID: {run_id}]"


def _mime_filename(name: str):
    try:
        name.encode("ascii")
        return name
    except UnicodeEncodeError:
        return ("utf-8", "", name)


def _attachment_part(filename: str, data: bytes,
                     content_type: str = "application/octet-stream") -> Message:
    maintype, subtype = content_type.split("/", 1)
    part = MIMEBase(maintype, subtype)
    part.set_payload(data)
    encoders.encode_base64(part)
    part.set_param("name", _mime_filename(filename))
    part.add_header("Content-Disposition", "attachment", filename=_mime_filename(filename))
    return part


def _encodable(text: str, charset: str) -> str:
    """Metni hedef karakter setinde ifade edilebilir hâle getirir (ör. konu
    satırındaki '←' Latin-5'te yoktur)."""
    text = text.replace("←", "<-").replace("→", "->").replace("—", "-")
    return text.encode(charset, "ignore").decode(charset)


def _set_common(msg: Message, ctx: BuildContext, subject=None, to=None) -> None:
    msg["Subject"] = subject if subject is not None else ctx.subject
    msg["From"] = ctx.from_address
    msg["To"] = to if to is not None else ctx.to_address


def _read_or_default(path: str, default: bytes) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return default


# 1x1 PNG / minimal PDF — yapılandırılan dosya okunamazsa senaryo yine çalışsın.
_FALLBACK_PNG = bytes([
    137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82,
    0, 0, 0, 1, 0, 0, 0, 1, 8, 2, 0, 0, 0, 144, 119, 83, 222, 0, 0, 0,
    12, 73, 68, 65, 84, 8, 215, 99, 248, 207, 192, 0, 0, 0, 2, 0, 1,
    226, 33, 188, 51, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130])
_FALLBACK_PDF = b"%PDF-1.4\n% Mail Otomasyon test belgesi\n%%EOF\n"


# ────────────────────────────────────────────────────────────────────
#  Kodlama / karakter seti senaryoları
# ────────────────────────────────────────────────────────────────────
_ENCODING_BODY = (
    "Merhaba,\n\n"
    "Bu mesaj Content-Transfer-Encoding dönüşümlerini sınar.\n"
    f"Türkçe karakterler: {TR_CHARS}\n"
    "Eşittir işaretleri: a=b; x==y; 2+2=4 (quoted-printable kaçış karakteri)\n"
    "Para birimi ve semboller: 1.250,50 ₺ — %20 indirim — 5 € · © ® ™\n"
    "Uzun satır (yumuşak satır sonu sınaması): "
    + "çalışma güncelleme şifreleme ölçüm ışık üretim " * 8 + "\n"
    "Satır sonu boşluğu olan satır:   \n"
    "\tSekme ile başlayan satır\n"
    "Son satır."
)


def _encoding_builder(cte: str):
    def build(ctx: BuildContext) -> BuiltMessage:
        body = _stamp(_ENCODING_BODY, ctx.run_id)
        msg = text_part(body, charset="utf-8", cte=cte)
        _set_common(msg, ctx)
        return BuiltMessage(msg, {
            "subject": ctx.subject,
            "body_text": body,
            "declared": {"charset": "utf-8", "cte": cte},
        })
    return build


_LATIN5_BODY = (
    "Merhaba,\n\n"
    "Bu mesajın gövdesi ve konusu UTF-8 DEĞİL, ISO-8859-9 (Latin-5) ile kodlanmıştır.\n"
    "Eski istemciler ve bazı kurumsal sistemler hâlâ bu karakter setini üretir.\n"
    f"Türkçe karakterler: {TR_CHARS}\n"
    "Sözcükler: çalışma, güncelleme, şifreleme, ışık, ölçüm, üretim, İstanbul, Iğdır.\n"
    "Sunucu içeriği UTF-8'e çevirebilir; çözülen metin birebir aynı kalmalıdır."
)

_CP1254_BODY = (
    "Merhaba,\n\n"
    "Bu mesaj Windows-1254 (Türkçe Windows kod sayfası) ile kodlanmıştır.\n"
    f"Türkçe karakterler: {TR_CHARS}\n"
    "Yalnızca Windows-1254'te bulunan (ISO-8859-9'da olmayan) karakterler:\n"
    "“akıllı tırnak” ‘tek tırnak’ – kısa tire — uzun tire … üç nokta € avro ™ marka\n"
    "Bu karakterler soru işaretine veya kutuya dönüşmemelidir."
)


def _legacy_charset_builder(charset: str, body_template: str, cte: str):
    def build(ctx: BuildContext) -> BuiltMessage:
        body = _stamp(body_template, ctx.run_id)
        subject = _encodable(ctx.subject, charset) + " | Şifreli Öğrenci İşleri"
        msg = text_part(body, charset=charset, cte=cte)
        _set_common(msg, ctx, subject=Header(subject, charset, header_name="Subject"))
        return BuiltMessage(msg, {
            "subject": subject,
            "body_text": body,
            "declared": {"charset": charset, "cte": cte},
        })
    return build


_LONG_SUBJECT_TAIL = (" | Öğrenci İşleri Şube Müdürlüğü güz dönemi değerlendirme "
                      "çağrısı – ığüşöç İĞÜŞÖÇ – başlık katlama sınaması")
_LONG_FILENAME = ("Öğrenci İşleri Şube Müdürlüğü – 2026 Güz Dönemi "
                  "Değerlendirme Raporu (son sürüm).txt")
_TO_DISPLAY = "Ayşe Gül Çalışkan, Öğr. Gör."


def _build_header_encoding(ctx: BuildContext) -> BuiltMessage:
    body = _stamp(
        "Merhaba,\n\n"
        "Bu mesaj BAŞLIK kodlamalarını sınar:\n"
        "- Konu: RFC 2047 Q-kodlaması, birden fazla encoded-word'e katlanmış uzun başlık\n"
        "- Kime: virgül ve Türkçe karakter içeren görünen ad\n"
        "- Ek: RFC 2231 ile kodlanmış uzun, Türkçe karakterli dosya adı\n"
        f"Karakterler: {TR_CHARS}", ctx.run_id)
    subject = ctx.subject + _LONG_SUBJECT_TAIL
    q_utf8 = Charset("utf-8")
    q_utf8.header_encoding = QP           # varsayılan B (base64) yerine Q-kodlaması

    file_data = ("Uzun dosya adı sınaması.\n" + TR_CHARS + "\n").encode("utf-8")
    msg = MIMEMultipart("mixed")
    msg.attach(text_part(body))
    msg.attach(_attachment_part(_LONG_FILENAME, file_data, "text/plain"))
    _set_common(msg, ctx,
                subject=Header(subject, q_utf8, header_name="Subject"),
                to=formataddr((_TO_DISPLAY, ctx.to_address)))
    return BuiltMessage(msg, {
        "subject": subject,
        "body_text": body,
        "address_headers": {"To": [[_TO_DISPLAY, ctx.to_address]]},
        "attachments": [{"filename": _LONG_FILENAME, "sha256": _sha256(file_data),
                         "size": len(file_data)}],
    })


# ────────────────────────────────────────────────────────────────────
#  Öncelik / bayrak senaryoları
# ────────────────────────────────────────────────────────────────────
_PRIORITY_HEADERS = {
    "high": {"X-Priority": "1 (Highest)", "X-MSMail-Priority": "High",
             "Importance": "high", "Priority": "urgent"},
    "low": {"X-Priority": "5 (Lowest)", "X-MSMail-Priority": "Low",
            "Importance": "low", "Priority": "non-urgent"},
}


def _priority_builder(level: str):
    label = {"high": "YÜKSEK", "low": "DÜŞÜK"}[level]

    def build(ctx: BuildContext) -> BuiltMessage:
        body = _stamp(
            "Merhaba,\n\n"
            f"Bu mesaj {label} öncelikli olarak işaretlenmiştir.\n"
            "Öncelik bilgisi dört farklı başlıkla taşınır: X-Priority, "
            "X-MSMail-Priority, Importance ve Priority (RFC 2156).\n"
            "Sunucu bu başlıkları silerse alıcı istemci öncelik simgesini gösteremez.\n"
            f"Karakter sınaması: {TR_CHARS}", ctx.run_id)
        msg = text_part(body)
        _set_common(msg, ctx)
        for name, value in _PRIORITY_HEADERS[level].items():
            msg[name] = value
        return BuiltMessage(msg, {
            "subject": ctx.subject,
            "body_text": body,
            "priority_level": level,
        }, meta={"priority_level": level})
    return build


def _build_read_receipt(ctx: BuildContext) -> BuiltMessage:
    body = _stamp(
        "Merhaba,\n\n"
        "Bu mesaj okundu bilgisi (MDN, RFC 8098) ister ve gizlilik düzeyi taşır.\n"
        "- Disposition-Notification-To: okundu bilgisi isteği\n"
        "- Sensitivity: Company-Confidential (RFC 2156)\n"
        "Alıcı istemci okundu bilgisi göndermek isteyip istemediğinizi sorabilir.\n"
        f"Karakter sınaması: {TR_CHARS}", ctx.run_id)
    msg = text_part(body)
    _set_common(msg, ctx)
    msg["Disposition-Notification-To"] = ctx.from_address
    msg["Return-Receipt-To"] = ctx.from_address
    msg["Sensitivity"] = "Company-Confidential"
    return BuiltMessage(msg, {
        "subject": ctx.subject,
        "body_text": body,
        "address_headers": {"Disposition-Notification-To": [["", ctx.from_address]]},
        "headers_equal": {"Sensitivity": "Company-Confidential"},
    })


# ────────────────────────────────────────────────────────────────────
#  Adresleme / başlık senaryoları
# ────────────────────────────────────────────────────────────────────
_CC_DISPLAY = "Şükrü Öğretmenoğlu (Bilgi İşlem)"
_REPLY_DISPLAY = "Yanıt Masası – Çağrı Merkezi"


def _build_cc_reply_to(ctx: BuildContext) -> BuiltMessage:
    body = _stamp(
        "Merhaba,\n\n"
        "Bu mesaj adres başlıklarını sınar:\n"
        "- Kime: görünen adlı alıcı\n"
        "- Bilgi (Cc): gönderen hesabın kendisi\n"
        "- Yanıt adresi (Reply-To): gönderenden FARKLI görünen adla tanımlı\n"
        "İstemcide 'Yanıtla' denildiğinde Reply-To adresi seçilmelidir.\n"
        f"Karakter sınaması: {TR_CHARS}", ctx.run_id)
    msg = text_part(body)
    _set_common(msg, ctx, to=formataddr((_TO_DISPLAY, ctx.to_address)))
    msg["Cc"] = formataddr((_CC_DISPLAY, ctx.from_address))
    msg["Reply-To"] = formataddr((_REPLY_DISPLAY, ctx.from_address))
    return BuiltMessage(msg, {
        "subject": ctx.subject,
        "body_text": body,
        "address_headers": {
            "To": [[_TO_DISPLAY, ctx.to_address]],
            "Cc": [[_CC_DISPLAY, ctx.from_address]],
            "Reply-To": [[_REPLY_DISPLAY, ctx.from_address]],
        },
    }, extra_recipients=[ctx.from_address])


_DOC_NO = "2026/TEST-ŞĞİ-001"


def _build_custom_headers(ctx: BuildContext) -> BuiltMessage:
    body = _stamp(
        "Merhaba,\n\n"
        "Bu mesaj özel ve liste başlıklarının korunumunu sınar:\n"
        "- X-Kurum-Belge-No (ASCII dışı değerli özel başlık)\n"
        "- X-Ozel-Izleme (uygulamaya özgü izleme kimliği)\n"
        "- List-Id / List-Unsubscribe (RFC 2919 / RFC 2369)\n"
        "- Auto-Submitted: auto-generated (RFC 3834)\n"
        "İş akışı / belge yönetimi entegrasyonları bu başlıklara dayanır.\n"
        f"Karakter sınaması: {TR_CHARS}", ctx.run_id)
    trace = f"izleme-{ctx.run_id}"
    unsubscribe = "<mailto:liste-cikis@example.com?subject=cikis>, <https://example.com/cikis?u=42>"
    msg = text_part(body)
    _set_common(msg, ctx)
    msg["X-Kurum-Belge-No"] = Header(_DOC_NO, "utf-8", header_name="X-Kurum-Belge-No")
    msg["X-Ozel-Izleme"] = trace
    msg["List-Id"] = "Test Listesi <test-listesi.example.com>"
    msg["List-Unsubscribe"] = unsubscribe
    msg["Auto-Submitted"] = "auto-generated"
    return BuiltMessage(msg, {
        "subject": ctx.subject,
        "body_text": body,
        "headers_equal": {
            "X-Kurum-Belge-No": _DOC_NO,
            "X-Ozel-Izleme": trace,
            "List-Id": "Test Listesi <test-listesi.example.com>",
            "List-Unsubscribe": unsubscribe,
            "Auto-Submitted": "auto-generated",
        },
    })


# ────────────────────────────────────────────────────────────────────
#  İçerik / taşıma senaryoları
# ────────────────────────────────────────────────────────────────────
_TRANSPARENCY_BODY = (
    "Satir 1: normal satir (bu govde bilerek yalnizca ASCII ve 7bit'tir)\n"
    ".tek nokta ile baslayan satir\n"
    "..iki nokta ile baslayan satir\n"
    ".\n"
    "From ile baslayan satir (mbox From-munging: basina '>' eklenmemeli)\n"
    ">From zaten alintili satir\n"
    "\tsekme ile baslayan satir\n"
    "Uzun satir: " + "0123456789" * 95 + "\n"
    "\n"
    "\n"
    "Ust uste bos satirlardan sonraki satir\n"
    "-- \n"
    "Imza ayiricisindan (tire tire bosluk) sonraki satir"
)


def _build_smtp_transparency(ctx: BuildContext) -> BuiltMessage:
    body = _stamp(_TRANSPARENCY_BODY, ctx.run_id)
    msg = text_part(body, charset="us-ascii", cte="7bit")
    _set_common(msg, ctx)
    return BuiltMessage(msg, {
        "subject": ctx.subject,
        "body_text": body,
        "declared": {"charset": "us-ascii", "cte": "7bit"},
    })


def large_attachment_bytes(test_config: dict) -> int:
    """Büyük ek boyutu (bayt). ``test.large_attachment_mb`` — varsayılan 5 MB."""
    try:
        mb = float((test_config or {}).get("large_attachment_mb") or 5)
    except (TypeError, ValueError):
        mb = 5.0
    return int(max(0.001, min(mb, 100.0)) * 1024 * 1024)


def _build_large_attachment(ctx: BuildContext) -> BuiltMessage:
    size = large_attachment_bytes(ctx.test_config)
    data = os.urandom(size)               # sıkıştırılamaz: gerçek boyut sınırını zorlar
    filename = f"buyuk_ek_{size / (1024 * 1024):.1f}MB.bin".replace(".0MB", "MB")
    body = _stamp(
        "Merhaba,\n\n"
        f"Bu mesaj {size} baytlık ikili (rastgele içerikli) bir ek taşır.\n"
        "Ekin SHA-256 özeti gönderim anında kaydedilir; alınan ek bayt bayt aynı olmalıdır.\n"
        "Sunucunun mesaj boyutu sınırı düşürüldüyse bu senaryo ilk haber veren olur.\n"
        f"Karakter sınaması: {TR_CHARS}", ctx.run_id)
    msg = MIMEMultipart("mixed")
    msg.attach(text_part(body))
    msg.attach(_attachment_part(filename, data))
    _set_common(msg, ctx)
    return BuiltMessage(msg, {
        "subject": ctx.subject,
        "body_text": body,
        "attachments": [{"filename": filename, "sha256": _sha256(data), "size": size}],
    }, meta={"attachment_name": filename, "attachment_size": size, "attachment_count": 1})


def _build_nested_multipart(ctx: BuildContext) -> BuiltMessage:
    image = _read_or_default(ctx.image_path, _FALLBACK_PNG)
    pdf_path = ctx.attachment_paths[0] if ctx.attachment_paths else ""
    pdf = _read_or_default(pdf_path, _FALLBACK_PDF)
    pdf_name = "Toplantı Notları – Şubat.pdf"
    cid = f"gomulu_{uuid.uuid4().hex[:8]}@test"

    plain = _stamp(
        "Merhaba,\n\n"
        "Bu mesaj gerçek hayattaki en yaygın karma yapıyı taşır: düz metin + HTML "
        "gövde, HTML içinde gömülü görsel ve ayrıca bir PDF eki.\n"
        f"Karakter sınaması: {TR_CHARS}", ctx.run_id)
    html = (
        "<html><body>"
        "<p>Merhaba,</p>"
        "<p>Bu mesaj <b>düz metin + HTML</b> gövde, HTML içinde <i>gömülü görsel</i> "
        "ve ayrıca bir <u>PDF eki</u> taşır.</p>"
        f'<p><img src="cid:{cid}" alt="Gömülü görsel" style="max-width:300px"/></p>'
        f"<p>Karakter sınaması: {TR_CHARS}</p>"
        f'<p style="font-size:11px;color:#999">[Run ID: {ctx.run_id}]</p>'
        "</body></html>")

    alt = MIMEMultipart("alternative")
    alt.attach(text_part(plain))
    alt.attach(text_part(html, subtype="html"))
    img = MIMEImage(image, _subtype="png")
    img.add_header("Content-ID", f"<{cid}>")
    img.add_header("Content-Disposition", "inline", filename="gomulu.png")
    related = MIMEMultipart("related")
    related.attach(alt)
    related.attach(img)
    msg = MIMEMultipart("mixed")
    msg.attach(related)
    msg.attach(_attachment_part(pdf_name, pdf, "application/pdf"))
    _set_common(msg, ctx)
    return BuiltMessage(msg, {
        "subject": ctx.subject,
        "body_text": plain,
        "html_contains": [f"cid:{cid}", TR_CHARS, f"[Run ID: {ctx.run_id}]"],
        "attachments": [{"filename": pdf_name, "sha256": _sha256(pdf), "size": len(pdf)}],
        "inline": [{"cid": cid, "sha256": _sha256(image)}],
        "leaf_types": ["text/plain", "text/html", "image/png", "application/pdf"],
    }, meta={"cid": cid, "attachment_name": pdf_name, "attachment_count": 1})


_LINKS = (
    ("Sorgu parametreli bağlantı",
     "https://example.com/yol/sayfa?a=1&b=iki%20kelime&c=%C3%A7ay#bolum-2"),
    ("Düz HTTP bağlantısı", "http://example.org/"),
    ("E-posta bağlantısı", "mailto:destek@example.com?subject=Test%20Konusu"),
    ("Telefon bağlantısı", "tel:+903120000000"),
)


def _build_html_links(ctx: BuildContext) -> BuiltMessage:
    from html import escape
    plain = _stamp(
        "Merhaba,\n\nBu mesaj HTML bağlantılarının (href) değişmeden iletilip "
        "iletilmediğini sınar.\n"
        + "\n".join(f"- {text}: {url}" for text, url in _LINKS)
        + f"\nKarakter sınaması: {TR_CHARS}", ctx.run_id)
    items = "".join(f'<li><a href="{escape(url, quote=True)}">{escape(text)}</a></li>'
                    for text, url in _LINKS)
    html = (
        "<html><body><p>Merhaba,</p>"
        "<p>Aşağıdaki bağlantılar değişmeden iletilmelidir. Güvenlik geçidi "
        "bağlantıları yeniden yazıyorsa (URL koruması) bu senaryo bunu raporlar.</p>"
        f"<ul>{items}</ul>"
        f"<p>Karakter sınaması: {TR_CHARS}</p>"
        f'<p style="font-size:11px;color:#999">[Run ID: {ctx.run_id}]</p>'
        "</body></html>")
    msg = MIMEMultipart("alternative")
    msg.attach(text_part(plain))
    msg.attach(text_part(html, subtype="html", cte="quoted-printable"))
    _set_common(msg, ctx)
    return BuiltMessage(msg, {
        "subject": ctx.subject,
        "body_text": plain,
        "html_hrefs": [url for _, url in _LINKS],
        "leaf_types": ["text/plain", "text/html"],
    })


# ────────────────────────────────────────────────────────────────────
#  Katalog
# ────────────────────────────────────────────────────────────────────
_M_OPEN = "Mesaj alıcı istemcide gelen kutusunda görünüyor ve açılıyor mu?"
_M_TR = "Türkçe karakterler (ğüşıöç ĞÜŞİÖÇ) konu ve gövdede doğru görünüyor mu?"

_SPECS = [
    ScenarioSpec(
        "enc_quoted_printable", "Kodlama: Quoted-Printable Gövde", "QP Kodlama",
        "kodlama", "UTF-8, quoted-printable", _encoding_builder("quoted-printable"),
        ("Quoted-printable kodlu UTF-8 mesaj gönder",
         "Gövde metni çözüldüğünde birebir aynı mı?",
         "'=' karakterleri ve satır sonu boşlukları korundu mu?",
         "Uzun satır (yumuşak satır sonu) doğru birleştirildi mi?",
         "Konu (Subject) bozulmadan iletildi mi?"),
        (_M_OPEN, _M_TR,
         "Metinde '=C3=B6', '=20' gibi çözülmemiş kodlama kalıntısı YOK mu?",
         "Uzun satır ortasından bölünmeden / fazladan '=' olmadan görünüyor mu?"),
    ),
    ScenarioSpec(
        "enc_base64", "Kodlama: Base64 Gövde", "Base64 Kodlama",
        "kodlama", "UTF-8, base64", _encoding_builder("base64"),
        ("Base64 kodlu UTF-8 mesaj gönder",
         "Gövde metni çözüldüğünde birebir aynı mı?",
         "Semboller (₺ € © ™) korundu mu?",
         "Sunucu kodlamayı değiştirdiyse içerik yine aynı mı?",
         "Konu (Subject) bozulmadan iletildi mi?"),
        (_M_OPEN, _M_TR,
         "Gövde okunabilir metin olarak mı görünüyor (base64 harf yığını DEĞİL)?",
         "Semboller (₺ € © ™) kutu / soru işaretine dönüşmeden görünüyor mu?"),
    ),
    ScenarioSpec(
        "enc_8bit", "Kodlama: 8bit Gövde (8BITMIME)", "8bit Kodlama",
        "kodlama", "UTF-8, 8bit ham", _encoding_builder("8bit"),
        ("Ham 8bit UTF-8 gövdeli mesaj gönder",
         "Gövde metni birebir aynı mı?",
         "Sunucu 8bit → QP/base64 dönüşümü yaptıysa içerik korundu mu?",
         "Üst bit (8. bit) kırpılması yok mu? (ğ → g, ş → s gibi)",
         "Konu (Subject) bozulmadan iletildi mi?"),
        (_M_OPEN, _M_TR,
         "Türkçe harfler ASCII karşılıklarına (ğ→g, ş→s) ya da 'Ã¶' gibi çift "
         "kodlanmış biçime dönüşmeden görünüyor mu?"),
    ),
    ScenarioSpec(
        "charset_iso8859_9", "Karakter Seti: ISO-8859-9 (Latin-5)", "ISO-8859-9",
        "kodlama", "Latin-5 gövde + konu", _legacy_charset_builder(
            "iso-8859-9", _LATIN5_BODY, "quoted-printable"),
        ("ISO-8859-9 kodlu mesaj gönder (gövde + konu)",
         "Gövde çözüldüğünde Türkçe karakterler birebir aynı mı?",
         "Konu satırı (=?iso-8859-9?...?=) doğru çözülüyor mu?",
         "Sunucu charset'i dönüştürdüyse veri kaybı yok mu?",
         "ı/İ ve ğ/Ğ ayrımı korundu mu?"),
        (_M_OPEN,
         "Gövdedeki Türkçe karakterler doğru görünüyor mu? (Ý/ý, Þ/þ, Ð/ð "
         "görünüyorsa istemci Latin-1 sanmıştır — KALDI)",
         "Konu satırındaki 'Şifreli Öğrenci İşleri' ifadesi doğru görünüyor mu?"),
    ),
    ScenarioSpec(
        "charset_windows_1254", "Karakter Seti: Windows-1254", "Windows-1254",
        "kodlama", "cp1254 gövde + konu", _legacy_charset_builder(
            "windows-1254", _CP1254_BODY, "base64"),
        ("Windows-1254 kodlu mesaj gönder (gövde + konu)",
         "Gövde çözüldüğünde Türkçe karakterler birebir aynı mı?",
         "cp1254'e özgü karakterler (“ ” – — … € ™) korundu mu?",
         "Konu satırı doğru çözülüyor mu?",
         "Sunucu charset'i dönüştürdüyse veri kaybı yok mu?"),
        (_M_OPEN, _M_TR,
         "Akıllı tırnaklar (“ ”), uzun tire (—), üç nokta (…) ve € doğru görünüyor mu?"),
    ),
    ScenarioSpec(
        "header_encoding", "Başlık Kodlama (RFC 2047 / RFC 2231)", "Başlık Kodlama",
        "kodlama", "Q-kodlu uzun konu + uzun dosya adı", _build_header_encoding,
        ("Uzun Türkçe konu, görünen ad ve uzun dosya adlı mesaj gönder",
         "Çok parçalı (katlanmış) konu satırı eksiksiz çözülüyor mu?",
         "Virgüllü / Türkçe görünen ad (Kime) korundu mu?",
         "Uzun Türkçe dosya adı (RFC 2231) korundu mu?",
         "Ek içeriği bayt düzeyinde aynı mı?"),
        (_M_OPEN,
         "Uzun konu satırı tam ve doğru görünüyor mu? (kelime arasında kayıp "
         "boşluk, '=?utf-8?Q?' kalıntısı YOK)",
         "Alıcı adı 'Ayşe Gül Çalışkan, Öğr. Gör.' TEK kişi olarak mı görünüyor "
         "(virgülden ikiye bölünmeden)?",
         "Ekin dosya adı tam ve Türkçe karakterleriyle görünüyor mu? Ek açılıyor mu?"),
    ),
    ScenarioSpec(
        "priority_high", "Yüksek Öncelikli Mesaj", "Yüksek Öncelik",
        "oncelik", "X-Priority 1 / Importance high", _priority_builder("high"),
        ("Yüksek öncelikli mesaj gönder",
         "Öncelik başlıklarından en az biri (Importance / X-Priority) korundu mu?",
         "Korunan başlıklar hâlâ 'yüksek' düzeyini mi gösteriyor?",
         "Gövde metni birebir aynı mı?",
         "Konu (Subject) bozulmadan iletildi mi?"),
        (_M_OPEN,
         "Mesaj listesinde / mesajda YÜKSEK öncelik simgesi (kırmızı ünlem '!') "
         "görünüyor mu?",
         "Mesaj normal ya da düşük öncelikli gibi GÖRÜNMÜYOR, değil mi?"),
    ),
    ScenarioSpec(
        "priority_low", "Düşük Öncelikli Mesaj", "Düşük Öncelik",
        "oncelik", "X-Priority 5 / Importance low", _priority_builder("low"),
        ("Düşük öncelikli mesaj gönder",
         "Öncelik başlıklarından en az biri (Importance / X-Priority) korundu mu?",
         "Korunan başlıklar hâlâ 'düşük' düzeyini mi gösteriyor?",
         "Gövde metni birebir aynı mı?",
         "Konu (Subject) bozulmadan iletildi mi?"),
        (_M_OPEN,
         "Mesajda DÜŞÜK öncelik simgesi (aşağı ok '↓') görünüyor mu? "
         "(İstemci düşük önceliği hiç göstermiyorsa 'Uygulanamaz' seçin)",
         "Mesaj yanlışlıkla YÜKSEK öncelikli görünmüyor, değil mi?"),
    ),
    ScenarioSpec(
        "read_receipt", "Okundu Bilgisi ve Gizlilik Başlıkları", "Okundu Bilgisi",
        "oncelik", "MDN isteği + Sensitivity", _build_read_receipt,
        ("Okundu bilgisi isteyen, gizlilik düzeyli mesaj gönder",
         "Disposition-Notification-To başlığı korundu mu?",
         "Sensitivity (gizlilik) başlığı korundu mu?",
         "Gövde metni birebir aynı mı?",
         "Konu (Subject) bozulmadan iletildi mi?"),
        (_M_OPEN,
         "Mesaj açılınca istemci okundu bilgisi göndermeyi soruyor / gönderiyor mu? "
         "(İstemci MDN desteklemiyorsa 'Uygulanamaz')",
         "Mesajda 'Gizli / Confidential' ibaresi görünüyor mu? (Desteklemiyorsa "
         "'Uygulanamaz')"),
    ),
    ScenarioSpec(
        "cc_reply_to", "Cc / Reply-To / Görünen Ad", "Cc ve Reply-To",
        "adresleme", "Cc + farklı Reply-To", _build_cc_reply_to,
        ("Cc ve Reply-To içeren mesaj gönder",
         "Cc başlığı (adres + görünen ad) korundu mu?",
         "Reply-To başlığı (adres + görünen ad) korundu mu?",
         "Kime (To) görünen adı korundu mu?",
         "Gövde metni birebir aynı mı?"),
        (_M_OPEN,
         "Mesaj ayrıntılarında Bilgi (Cc) alanında 'Şükrü Öğretmenoğlu (Bilgi İşlem)' "
         "görünüyor mu?",
         "'Yanıtla'ya basınca alıcı alanına 'Yanıt Masası – Çağrı Merkezi' geliyor mu?",
         "'Tümünü Yanıtla' Cc'deki adresi de ekliyor mu?"),
    ),
    ScenarioSpec(
        "custom_headers", "Özel Başlık Korunumu (X- / List-*)", "Özel Başlıklar",
        "adresleme", "X- ve List-* başlıkları", _build_custom_headers,
        ("Özel (X-) ve liste başlıkları içeren mesaj gönder",
         "X- ile başlayan özel başlıklar korundu mu?",
         "ASCII dışı başlık değeri (belge no) doğru çözülüyor mu?",
         "List-Id / List-Unsubscribe korundu mu?",
         "Auto-Submitted başlığı korundu mu?"),
        (_M_OPEN,
         "İstemci 'Abonelikten çık / Unsubscribe' bağlantısı gösteriyor mu? "
         "(Göstermiyorsa 'Uygulanamaz')",
         "Mesaj gereksiz/spam klasörüne DÜŞMEDİ, değil mi?"),
    ),
    ScenarioSpec(
        "smtp_transparency", "SMTP Şeffaflık (Nokta / From / Uzun Satır)", "SMTP Şeffaflık",
        "icerik", "Nokta doldurma, From satırı, 950+ karakter satır",
        _build_smtp_transparency,
        ("Nokta / 'From ' ile başlayan ve çok uzun satırlar içeren mesaj gönder",
         "Nokta ile başlayan satırlar korundu mu? (dot-stuffing)",
         "'From ' ile başlayan satırın başına '>' eklenmedi mi?",
         "950+ karakterlik satır bölünmeden iletildi mi?",
         "Üst üste boş satırlar ve imza ayırıcısı korundu mu?"),
        (_M_OPEN,
         "'.tek nokta ile baslayan satir' satırı baştaki noktasıyla görünüyor mu?",
         "'From ile baslayan satir' satırının başında '>' YOK, değil mi?",
         "Uzun rakam satırı kesilmeden (sonu ...6789 ile biterek) görünüyor mu?"),
    ),
    ScenarioSpec(
        "large_attachment", "Büyük Ek (Boyut Sınırı)", "Büyük Ek",
        "icerik", "İkili ek, SHA-256 doğrulamalı", _build_large_attachment,
        ("Büyük ikili ek içeren mesaj gönder (varsayılan 5 MB)",
         "Mesaj boyut sınırına takılmadan teslim edildi mi?",
         "Ekin SHA-256 özeti gönderilenle aynı mı?",
         "Ek boyutu bayt düzeyinde aynı mı?",
         "Ek dosya adı korundu mu?"),
        (_M_OPEN,
         "Ek, doğru boyutla (yaklaşık yapılandırılan MB) listeleniyor mu?",
         "Ek indirilebiliyor mu? (mobil veride/yavaş ağda yarıda kesilmeden)"),
    ),
    ScenarioSpec(
        "nested_multipart", "Karma Yapı (HTML + Inline Görsel + Ek)", "Karma Yapı",
        "icerik", "mixed › related › alternative", _build_nested_multipart,
        ("Düz metin + HTML + gömülü görsel + PDF ekli mesaj gönder",
         "Tüm parçalar (text/plain, text/html, image, pdf) iletildi mi?",
         "Gömülü görsel (CID) ve PDF eki bayt düzeyinde aynı mı?",
         "HTML içindeki cid: referansı korundu mu?",
         "Türkçe karakterli ek dosya adı korundu mu?"),
        (_M_OPEN,
         "Gömülü görsel gövdenin İÇİNDE mi görünüyor? (kırık resim simgesi YOK)",
         "Gömülü görsel ayrıca ek listesinde İKİNCİ KEZ görünmüyor, değil mi?",
         "PDF eki 'Toplantı Notları – Şubat.pdf' adıyla görünüyor ve açılıyor mu?"),
    ),
    ScenarioSpec(
        "html_links", "HTML Bağlantı Bütünlüğü (URL Yeniden Yazma)", "HTML Bağlantılar",
        "icerik", "href / mailto / tel", _build_html_links,
        ("Çeşitli bağlantılar (https, mailto, tel) içeren HTML mesaj gönder",
         "Tüm href değerleri değişmeden iletildi mi?",
         "Sorgu parametreleri (&, %20, #) korundu mu?",
         "Güvenlik geçidi bağlantıları yeniden yazdı mı? (URL koruması)",
         "Düz metin alternatifi korundu mu?"),
        (_M_OPEN,
         "Bağlantılar tıklanabilir mi ve tıklayınca DOĞRU adres mi açılıyor?",
         "E-posta bağlantısı yeni ileti penceresini konu 'Test Konusu' ile açıyor mu?",
         "Telefon bağlantısı arama ekranını açıyor mu? (masaüstünde 'Uygulanamaz')"),
    ),
]

SPEC_SCENARIOS: dict[str, ScenarioSpec] = {s.key: s for s in _SPECS}

# Elle yazılmış senaryolar için alıcı istemcide bakılacak adımlar.
LEGACY_MANUAL_STEPS: dict[str, tuple] = {
    "plain_text": (_M_OPEN, _M_TR,
                   "İmza bloğu ('Saygılarımla, ...') satır satır düzgün görünüyor mu?"),
    "attachment": (_M_OPEN,
                   "Ek, doğru dosya adı ve simgesiyle görünüyor mu?",
                   "Eke dokununca / tıklayınca dosya açılıyor mu?"),
    "multi_attachment": (_M_OPEN,
                         "Eklerin TAMAMI listeleniyor mu? (adet konu satırında yazıyor)",
                         "Her ek kendi adı ve türüyle açılıyor mu?"),
    "inline_image": (_M_OPEN,
                     "Görsel gövdenin içinde görünüyor mu? (kırık resim simgesi YOK)",
                     "Görsel ayrıca ek olarak İKİNCİ KEZ listelenmiyor, değil mi?"),
    "smime": (_M_OPEN,
              "İstemci imzayı doğrulanmış (rozet / kilit simgesi) gösteriyor mu?",
              "İmzalayan sertifika ayrıntıları görüntülenebiliyor mu?"),
    "reply_chain": (_M_OPEN,
                    "Yanıt, orijinal mesajla AYNI konuşma (thread) altında mı gruplanmış?",
                    "Alıntı (>) bölümü alıntı olarak biçimlenmiş mi?"),
    "forward": (_M_OPEN,
                "İletilen orijinal mesajın Kimden / Tarih / Konu bilgileri okunuyor mu?",
                "'iletilen_mesaj.eml' eki açılabiliyor mu?"),
    "html_table": (_M_OPEN,
                   "Tablo satır/sütun yapısıyla (kenarlıklar, başlık rengi) görünüyor mu?",
                   "Dar ekranda tablo taşmadan / kaydırılarak okunabiliyor mu?"),
    "complex_html": (_M_OPEN,
                     "Renkli başlık / kart düzeni uygulanmış görünüyor mu?",
                     "Mobilde tek sütuna geçiyor mu? (masaüstünde pencereyi daraltın)",
                     "Karanlık modda metin okunabilir kalıyor mu?"),
    "i18n": (_M_OPEN,
             "Konu satırındaki emoji ve Türkçe karakterler doğru görünüyor mu?",
             "Arapça (﷽) ve CJK (漢字) karakterler kutu olmadan görünüyor mu?",
             "Emoji (🚀🔥🐞) renkli simge olarak görünüyor mu?"),
    "calendar_invite": (_M_OPEN,
                        "Mesaj davet olarak tanınıyor mu? (Kabul / Reddet düğmeleri)",
                        "Toplantı başlığı, tarihi, saati ve konumu doğru mu?",
                        "'Kabul' sonrası etkinlik takvime ekleniyor mu?"),
}
LEGACY_MANUAL_STEPS["forward_chain"] = LEGACY_MANUAL_STEPS["forward"]

_LEGACY_GROUP = "temel"


def manual_steps_for(scenario_key: str) -> list:
    spec = SPEC_SCENARIOS.get(scenario_key)
    if spec:
        return list(spec.manual_steps)
    return list(LEGACY_MANUAL_STEPS.get(scenario_key, (_M_OPEN, _M_TR)))


def scenario_label(scenario_key: str) -> str:
    spec = SPEC_SCENARIOS.get(scenario_key)
    if spec:
        return spec.label
    return LEGACY_SCENARIOS.get(scenario_key, scenario_key)


def group_of(scenario_key: str) -> str:
    spec = SPEC_SCENARIOS.get(scenario_key)
    return spec.group if spec else _LEGACY_GROUP


def catalog() -> list:
    """Arayüz için: tüm senaryolar (anahtar, etiket, grup)."""
    items = [{"key": k, "label": v, "group": _LEGACY_GROUP,
              "group_label": GROUP_LABELS[_LEGACY_GROUP], "deterministic": False}
             for k, v in LEGACY_SCENARIOS.items()]
    items += [{"key": s.key, "label": s.label, "group": s.group,
               "group_label": GROUP_LABELS[s.group], "deterministic": True}
              for s in _SPECS]
    return items


def expand_scenario_filter(value: Optional[str]) -> Optional[set]:
    """``--scenario`` değerini anahtar kümesine çevirir.

    'plain_text' · 'enc_base64,priority_high' · 'group:kodlama' biçimleri
    desteklenir. None/boş → filtre yok (None).
    """
    if not value:
        return None
    keys: set = set()
    for token in str(value).split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("group:"):
            group = token.split(":", 1)[1].strip().lower()
            keys.update(item["key"] for item in catalog() if item["group"] == group)
            if group == _LEGACY_GROUP:
                keys.add("forward_chain")
        else:
            keys.add(token)
    return keys


def is_valid_scenario_filter(value: str, supported: set) -> bool:
    """Arayüzden gelen filtre yalnızca bilinen anahtar/grup içeriyor mu?"""
    tokens = [t.strip() for t in str(value).split(",") if t.strip()]
    if not tokens:
        return False
    for token in tokens:
        if token.startswith("group:"):
            if token.split(":", 1)[1].strip().lower() not in GROUP_LABELS:
                return False
        elif token not in supported:
            return False
    return True


def llm_check_prompt(scenario_key: str) -> str:
    """Spec senaryosu için LLM'e verilecek kontrol listesi (checklist adımları)."""
    spec = SPEC_SCENARIOS.get(scenario_key)
    if not spec:
        return ""
    lines = "\n".join(f"{i}. {step}" for i, step in enumerate(spec.steps[1:], start=1))
    return ("\nKontrol edilecekler:\n" + lines +
            "\n\nNot: Bayt düzeyindeki karşılaştırmalar (gövde metni, ek özeti, başlık "
            "eşitliği) ayrıca deterministik olarak yapılmıştır; sonuçları 'Gönderim "
            "Metadata' altındaki deterministic_checks alanındadır. Onlarla çelişme; "
            "yalnızca orada görünmeyen protokol/yapı sorunlarını ek olarak raporla.\n")
