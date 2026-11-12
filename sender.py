"""
sender.py — SMTP üzerinden test mesajlarını gönderir.
Her senaryo tipi için ayrı metot: plain, attachment, inline_image, smime, reply.
"""
from __future__ import annotations

import re
import smtplib
import ssl
import uuid
import time
import os
import logging
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.message import MIMEMessage
from email import encoders
from email.header import Header
from email.utils import formatdate, getaddresses, make_msgid, quote
from email.policy import SMTPUTF8, compat32
import shutil
from typing import Optional

from auth_manager import generate_totp, mfa_manager
from mail_transport import Deadline, smtp_connect

# Kablo üzerinde CRLF — compat32 davranışı aynen korunur, yalnız satır sonu değişir.
SMTP_POLICY = compat32.clone(linesep="\r\n")
# ASCII dışı adresler (EAI, RFC 6531): header'lar ham UTF-8 gider, sunucu SMTPUTF8 ister.
SMTPUTF8_POLICY = SMTPUTF8.clone(linesep="\r\n")

logger = logging.getLogger(__name__)

# SMTP bağlantı + giriş için toplam süre sınırı (soket timeout'u yalnızca tek
# okumayı sınırlar; satır damlatan bir sunucu koşuyu süresiz bekletebiliyordu).
CONNECT_DEADLINE = 90.0


def _ascii_address(addr: str) -> str:
    """Yerel kısım ASCII, alan adı IDN ise alan adını A-label'a (xn--) çevirir.

    Böyle bir adres SMTPUTF8 gerektirmez; eskiden info@müller.de SMTPUTF8'siz
    sunucuda reddediliyordu. Yerel kısım ASCII değilse adres olduğu gibi kalır.
    """
    if "@" not in addr:
        return addr
    local, _, domain = addr.rpartition("@")
    if local.isascii() and not domain.isascii():
        try:
            return f"{local}@{domain.encode('idna').decode('ascii')}"
        except UnicodeError:
            return addr
    return addr


def parse_addresses(value) -> list:
    """Alıcı/gönderici alanını [(görünen ad, adres)] listesine çevirir.

    ``,`` ve (tırnak dışındaki) ``;`` ayraç kabul edilir; boş girdiler (sondaki
    virgül) atılır. Adres içermeyen ya da geçersiz bir girdi ValueError verir
    (eskiden ham metin tek bir bozuk RCPT olarak gidiyordu).
    """
    # Tırnak dışındaki , ve ; ile böl; boş parçalar (sondaki virgül) atılır.
    # Python 3.13'ün katı getaddresses'i sondaki virgülde tüm girdiyi reddeder.
    parts, cur, quoted = [], [], False
    for ch in str(value or ""):
        if ch == '"':
            quoted = not quoted
        if ch in ",;" and not quoted:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    parts = [x.strip() for x in parts if x.strip()]
    pairs = [(n, a.strip()) for n, a in (getaddresses([", ".join(parts)]) if parts else [])
             if n or a]
    if len(pairs) != len(parts):
        raise ValueError(f"Geçersiz e-posta adresi: {value!r}")
    for _, addr in pairs:
        local, at, domain = addr.rpartition("@")
        if not at or not local or not domain or any(c.isspace() for c in addr):
            raise ValueError(f"Geçersiz e-posta adresi: {value!r}")
    if not pairs:
        raise ValueError(f"Geçersiz e-posta adresi: {value!r}")
    return [(n, _ascii_address(a)) for n, a in pairs]


def format_address_header(value) -> str:
    """From/To header değeri: yalnızca görünen ad RFC 2047 ile kodlanır.

    Eskiden "Ünal Çelik <b@x>" tek bir encoded-word'e (adres dahil) dönüşüyor,
    istemciler adresi okuyamıyordu.
    """
    out = []
    for name, addr in parse_addresses(value):
        if not name:
            out.append(addr)
        elif not name.isascii():
            out.append(f"{Header(name, 'utf-8').encode()} <{addr}>")
        elif any(c in name for c in '()<>@,;:\\".[]'):
            out.append(f'"{quote(name)}" <{addr}>')
        else:
            out.append(f"{name} <{addr}>")
    return ", ".join(out)


def _interactive_mfa_enabled() -> bool:
    """Web arayüzü subprocess'i MFA_INTERACTIVE=1 ile başlatır; modal akışı
    yalnızca bu durumda devreye girer (yalın CLI'da 5 dk bloklamamak için)."""
    return os.environ.get("MFA_INTERACTIVE", "") == "1"


# ---------------------------------------------------------------------- #
#  iCalendar (RFC 5545) yardımcıları
# ---------------------------------------------------------------------- #
def _ics_escape(value: str) -> str:
    """RFC 5545 §3.3.11 — TEXT değerlerinde \\ ; , ve satır sonu kaçışlanır."""
    return (value.replace("\\", "\\\\")
                 .replace(";", "\\;")
                 .replace(",", "\\,")
                 .replace("\r\n", "\\n")
                 .replace("\n", "\\n")
                 .replace("\r", "\\n"))


def _ics_fold(line: str) -> str:
    """RFC 5545 §3.1 — satırlar 75 oktetten uzun olamaz; devam satırları
    tek boşlukla girintilenir. Katlama okteti bazında yapılır ki çok baytlı
    UTF-8 karakterler (ğ, 漢, emoji) ortadan bölünmesin."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, current = [], b""
    for char in line:
        encoded = char.encode("utf-8")
        limit = 75 if not chunks else 74      # devam satırlarında baştaki boşluk
        if len(current) + len(encoded) > limit:
            chunks.append(current)
            current = b""
        current += encoded
    if current:
        chunks.append(current)
    return "\r\n ".join(c.decode("utf-8") for c in chunks)


def _ics_party(prop: str, value: str, params: str) -> str:
    """ORGANIZER/ATTENDEE satırı: mailto'ya yalnızca adres yazılır, CN tırnaklanır.

    Eskiden görünen adlı ya da listeli adres "mailto:Ad <a@b>" gibi geçersiz
    bir URI üretiyordu.
    """
    try:
        name, addr = parse_addresses(value)[0]
    except ValueError:
        # Tırnaksız virgüllü görünen ad ("Çelik, Ünal <u@x>"): son adresi al
        found = re.findall(r'[^\s<>,;"]+@[^\s<>,;"]+', str(value))
        addr = found[-1] if found else str(value)
        name = str(value).replace(f"<{addr}>", "").strip().strip('",;').strip()
    cn = (name or addr).replace('"', "'")
    return f'{prop};CN="{cn}"{params}:mailto:{addr}'


def _build_ics(uid: str, summary: str, description: str, location: str,
               start, end, organizer: str, attendee: str) -> str:
    """METHOD:REQUEST içeren minimal ama standart uyumlu VEVENT üretir."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Mail Otomasyon//Test Suite//TR",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(location)}",
        _ics_party("ORGANIZER", organizer, ""),
        _ics_party("ATTENDEE", attendee, ";ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE"),
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    # RFC 5545 satır sonu CRLF'tir
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"


# Unicode blokları — i18n senaryosunda hangi alfabelerin taşındığını raporlar.
# Bir alfabe birden fazla blokta yaşayabilir (ör. ﷽ U+FDFD Arapça Sunum
# Formları-A içindedir, temel Arapça bloğunda değil).
_SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "latin_extended": ((0x0100, 0x017F),),                     # ğ ş İ ...
    "arabic":         ((0x0600, 0x06FF), (0x0750, 0x077F),
                       (0x08A0, 0x08FF), (0xFB50, 0xFDFF),
                       (0xFE70, 0xFEFF)),
    "cyrillic":       ((0x0400, 0x04FF),),
    "greek":          ((0x0370, 0x03FF),),
    "cjk":            ((0x4E00, 0x9FFF), (0x3040, 0x30FF)),    # Han + kana
    "emoji":          ((0x1F300, 0x1FAFF), (0x2600, 0x27BF)),
}


def _detect_scripts(text: str) -> list[str]:
    """Metinde geçen alfabe/simge gruplarını döndürür (analiz raporu için)."""
    code_points = {ord(ch) for ch in text}
    return [
        name for name, ranges in _SCRIPT_RANGES.items()
        if any(low <= cp <= high for cp in code_points for low, high in ranges)
    ]


def _mime_filename(name: str):
    """ASCII ad olduğu gibi; ASCII dışı ad RFC 2231 üçlüsü olarak döner."""
    try:
        name.encode("ascii")
        return name
    except UnicodeEncodeError:
        return ("utf-8", "", name)


class MailSender:
    def __init__(self, server_config: dict):
        self.config = server_config
        self.host = server_config["smtp_host"]
        self.port = server_config["smtp_port"]
        self.use_tls = server_config.get("smtp_use_tls", True)
        # YAML'da 'password: 123456' int olarak okunur; smtplib str ister.
        self.username = str(server_config.get("username") or "")
        self.password = str(server_config.get("password") or "")
        self.from_address = server_config["test_address"]
        self.auth_method = server_config.get("auth_method", "password")
        self.totp_secret = str(server_config.get("totp_secret") or "")

    def _login(self, smtp: smtplib.SMTP):
        """auth_method'a göre giriş yapar; TOTP secret varsa kodu otomatik üretir.

        Secret yoksa ve web arayüzünden başlatıldıysa (MFA_INTERACTIVE=1),
        kod kullanıcıdan modal üzerinden istenir (süreçler arası köprü).
        """
        if self.auth_method in ("totp_password", "otp_only"):
            code = generate_totp(self.totp_secret) if self.totp_secret else ""
            if not code and _interactive_mfa_enabled():
                label = self.config.get("label", self.host)
                code = mfa_manager.mfa_challenge(
                    server_key=label, server_label=label,
                    method=self.config.get("mfa_method", "totp"),
                ) or ""
            if code:
                if self.auth_method == "otp_only":
                    smtp.login(self.username, code)
                    return
                try:
                    smtp.login(self.username, self.password + code)
                    return
                except smtplib.SMTPException:
                    logger.warning("Şifre+TOTP girişi reddedildi, yalnızca şifre deneniyor.")
        smtp.login(self.username, self.password)

    def _connect(self) -> smtplib.SMTP:
        # TLS modu (465 SSL / STARTTLS / düz), iç CA ve doğrulama ayarları
        # mail_transport'ta — web arayüzünün bağlantı testi de aynı kodu kullanır.
        deadline = Deadline(CONNECT_DEADLINE)
        try:
            smtp = smtp_connect(self.config, timeout=30, deadline=deadline)
            try:
                # Kullanıcı adı yoksa kimlik doğrulamasız relay: giriş yapılmaz
                # (eskiden AUTH desteklemeyen relay'de SMTPNotSupportedError).
                if self.username or self.auth_method == "otp_only":
                    self._login(smtp)
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass
                raise
        except Exception as e:
            if deadline.expired:
                raise TimeoutError(f"SMTP bağlantısı {CONNECT_DEADLINE:.0f} sn içinde "
                                   f"kurulamadı ({self.host}:{self.port})") from e
            raise
        finally:
            deadline.close()   # bağlantı kuruldu: süre sınırı yalnızca bağlanma/giriş için
        logger.debug(f"SMTP bağlantısı kuruldu: {self.host}:{self.port}")
        return smtp

    def _base_headers(self, subject: str, to_address: str, msg_id: Optional[str] = None) -> dict:
        return {
            "Message-ID": msg_id or make_msgid(),
            "Date": formatdate(localtime=True),
            "Subject": subject,
            "From": format_address_header(self.from_address),
            "To": format_address_header(to_address),
            "X-Test-Automation": "mail-otomasyon-v1",
        }

    # ------------------------------------------------------------------ #
    #  Senaryo 1: Plain Text
    # ------------------------------------------------------------------ #
    def send_plain_text(self, to_address: str, subject: str, body: str) -> dict:
        """UTF-8 plain-text mesaj gönderir."""
        msg = MIMEText(body, "plain", "utf-8")
        msg_id = make_msgid()
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        sent_at = self._send(msg, to_address)
        logger.info(f"[PLAIN] Gönderildi → {to_address} | msg_id={msg_id}")
        return {"msg_id": msg_id, "sent_at": sent_at, "scenario": "plain_text"}

    # ------------------------------------------------------------------ #
    #  Senaryo 2: Attachment (tek veya çoklu)
    # ------------------------------------------------------------------ #
    def send_with_attachment(self, to_address: str, subject: str, body: str,
                             attachment_path: str | list[str]) -> dict:
        """Tek veya birden fazla dosya ekli mesaj gönderir."""
        paths = [attachment_path] if isinstance(attachment_path, str) else list(attachment_path)

        msg = MIMEMultipart("mixed")
        msg_id = make_msgid()
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        msg.attach(MIMEText(body, "plain", "utf-8"))

        attached_files: list[dict] = []
        for p in paths:
            if not os.path.exists(p):
                logger.warning(f"Ek dosya bulunamadı, atlanıyor: {p}")
                continue
            filename = os.path.basename(p)
            with open(p, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.replace_header("Content-Type", self._guess_mime(filename))
            # Türkçe/ASCII dışı dosya adı RFC 2231 (filename*=utf-8'') ile
            # kodlanır; eskiden tüm başlık tek bir encoded-word'e dönüşüyor ve
            # istemciler eki göremiyordu.
            part.set_param("name", _mime_filename(filename))
            part.add_header("Content-Disposition", "attachment", filename=_mime_filename(filename))
            msg.attach(part)
            attached_files.append({"name": filename, "size": os.path.getsize(p)})

        if not attached_files:
            logger.warning("Hiçbir ek dosya bulunamadı — mesaj eksiz gidiyor; analiz bunu FAIL sayacak.")

        sent_at = self._send(msg, to_address)
        names = ", ".join(a["name"] for a in attached_files)
        logger.info(f"[ATTACH] Gönderildi → {to_address} | dosyalar={names}")
        return {
            "msg_id": msg_id,
            "sent_at": sent_at,
            "scenario": "attachment",
            "attachment_name": names,
            # Analiz promptu boyutu bekliyordu ama hiç gönderilmiyordu ("? byte").
            "attachment_size": (attached_files[0]["size"] if len(attached_files) == 1
                                else ", ".join(str(a["size"]) for a in attached_files) or 0),
            "attachment_count": len(attached_files),
            "attachments": attached_files,
        }

    # ------------------------------------------------------------------ #
    #  Senaryo 3: Inline Image (Embedded HTML)
    # ------------------------------------------------------------------ #
    def send_inline_image(self, to_address: str, subject: str,
                          image_path: str, html_body: str | None = None) -> dict:
        """CID referanslı inline resim içeren HTML mesaj gönderir.

        html_body verilirse {{CID}} yer tutucusu otomatik doldurulur.
        Verilmezse varsayılan basit HTML kullanılır.
        """
        msg = MIMEMultipart("related")
        msg_id = make_msgid()
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        cid = f"inline_image_{uuid.uuid4().hex[:8]}@test"

        if html_body and "{{CID}}" in html_body:
            html_content = html_body.replace("{{CID}}", cid)
        else:
            html_content = (
                "<html><body>"
                "<p>Bu bir inline resim testidir.</p>"
                f'<img src="cid:{cid}" alt="Test Resmi" style="max-width:400px"/>'
                "<p>Resim yukarıda görünüyorsa CID referansı doğru çözümlenmiştir.</p>"
                "<p>Türkçe karakter: ğüşıöçĞÜŞİÖÇ</p>"
                "</body></html>"
            )

        alt_part = MIMEMultipart("alternative")
        alt_part.attach(MIMEText(
            "Inline resim testi — bu mesajı görüntülemek için HTML destekli istemci gereklidir. "
            "Türkçe: ğüşıöçĞÜŞİÖÇ",
            "plain", "utf-8",
        ))
        alt_part.attach(MIMEText(html_content, "html", "utf-8"))
        msg.attach(alt_part)

        with open(image_path, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=os.path.basename(image_path))
        msg.attach(img)

        sent_at = self._send(msg, to_address)
        logger.info(f"[INLINE] Gönderildi → {to_address} | cid={cid}")
        return {"msg_id": msg_id, "sent_at": sent_at, "scenario": "inline_image", "cid": cid}

    # ------------------------------------------------------------------ #
    #  Senaryo 4: S/MIME İmzalı Mesaj
    # ------------------------------------------------------------------ #
    def send_smime_signed(self, to_address: str, subject: str, body: str,
                          cert_path: str, key_path: str) -> dict:
        """
        S/MIME imzalı mesaj gönderir.
        Gereksinim: openssl kütüphanesi ve test sertifikası.
        cert_path: PEM formatında sertifika
        key_path: PEM formatında özel anahtar
        """
        from email import message_from_bytes

        # İmzalama openssl CLI ile yapılır; eskiden yalnızca kapı olarak
        # pyOpenSSL import ediliyordu (hiç kullanılmadan) ve kurulu değilse
        # geçerli sertifikayla bile S/MIME testi atlanıyordu.
        if not shutil.which("openssl"):
            logger.warning("openssl komutu bulunamadı. S/MIME testi atlanıyor.")
            return {"msg_id": None, "sent_at": None, "scenario": "smime", "skipped": True,
                    "skip_reason": "openssl komutu bulunamadı (PATH)"}

        # İmzalı MIME oluştur
        msg_id = make_msgid()
        inner = MIMEText(body, "plain", "utf-8")

        # openssl smime ile imzala
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".eml", delete=False, mode="w") as tmp:
            tmp.write(inner.as_string())
            tmp_path = tmp.name

        signed_path = tmp_path + ".signed"
        cmd = [
            "openssl", "smime", "-sign",
            "-in", tmp_path,
            "-signer", cert_path,
            "-inkey", key_path,
            "-out", signed_path,
            # -text YOK: girdi zaten MIME varlığı (Content-Type başlıklı). -text
            # ile openssl başına bir text/plain başlığı daha ekliyor, alıcı asıl
            # metin yerine başlıklar + base64 görüyordu.
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as e:   # openssl CLI yok vb. — eskiden imzasız .eml /tmp'de kalıyordu
            return {"msg_id": msg_id, "sent_at": None, "scenario": "smime",
                    "error": f"openssl çalıştırılamadı: {e}"}
        finally:
            os.unlink(tmp_path)

        if result.returncode != 0:
            logger.error(f"S/MIME imzalama hatası: {result.stderr}")
            try:
                os.unlink(signed_path)   # openssl boş çıktı dosyası bırakır
            except FileNotFoundError:
                pass
            return {"msg_id": msg_id, "sent_at": None, "scenario": "smime",
                    "error": result.stderr}

        with open(signed_path, "rb") as f:
            signed_content = f.read()
        os.unlink(signed_path)

        # openssl çıktısı yalnızca MIME gövdesini içerir; zarf header'ları
        # (From/To/Subject/Message-ID) eklenmezse alıcı mesajı eşleştiremez.
        signed_msg = message_from_bytes(signed_content)
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            if signed_msg.get(k) is None:
                signed_msg[k] = v

        sent_at = self._send(signed_msg, to_address)
        logger.info(f"[SMIME] İmzalı mesaj gönderildi → {to_address}")
        return {"msg_id": msg_id, "sent_at": sent_at, "scenario": "smime", "signed": True}

    # ------------------------------------------------------------------ #
    #  Senaryo 6: Zengin HTML (html_table / complex_html)
    # ------------------------------------------------------------------ #
    def send_html_message(self, to_address: str, subject: str, html_body: str,
                          plain_body: str, scenario: str = "complex_html") -> dict:
        """Resimsiz zengin HTML mesaj gönderir (multipart/alternative).

        HTML'i render edemeyen ya da engelleyen istemcide mesajın okunabilir
        kalması için text/plain bacağı zorunludur; bu yüzden ``plain_body``
        parametresi opsiyonel değildir.
        """
        msg = MIMEMultipart("alternative")
        msg_id = make_msgid()
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        # Sıra önemli: istemci en sondaki desteklenen bacağı gösterir (RFC 2046)
        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        sent_at = self._send(msg, to_address)
        logger.info(f"[HTML] Gönderildi → {to_address} | senaryo={scenario}")
        return {
            "msg_id": msg_id,
            "sent_at": sent_at,
            "scenario": scenario,
            "html_length": len(html_body),
            "has_plain_fallback": bool(plain_body.strip()),
        }

    # ------------------------------------------------------------------ #
    #  Senaryo 7: Uluslararası alfabe / emoji (i18n)
    # ------------------------------------------------------------------ #
    def send_i18n(self, to_address: str, subject: str, body: str) -> dict:
        """Çok alfabeli + emoji içerikli UTF-8 mesaj gönderir.

        Gövde kadar BAŞLIK da sınanır: ASCII dışı subject RFC 2047
        encoded-word olarak kodlanmalıdır (Python bunu otomatik yapar).
        """
        msg = MIMEText(body, "plain", "utf-8")
        msg_id = make_msgid()
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        sent_at = self._send(msg, to_address)
        logger.info(f"[I18N] Gönderildi → {to_address} | msg_id={msg_id}")
        return {
            "msg_id": msg_id,
            "sent_at": sent_at,
            "scenario": "i18n",
            "subject_is_ascii": subject.isascii(),
            "body_charsets": _detect_scripts(body),
        }

    # ------------------------------------------------------------------ #
    #  Senaryo 8: Takvim daveti (iTIP / ICS)
    # ------------------------------------------------------------------ #
    def send_calendar_invite(self, to_address: str, subject: str, body: str,
                             summary: str, start: Optional[datetime] = None,
                             duration_minutes: int = 30,
                             location: str = "Çevrimiçi Toplantı") -> dict:
        """RFC 5546 (iTIP) uyumlu toplantı daveti gönderir.

        Yapı — istemci uyumluluğu için hem gövdede hem ek olarak takvim verisi:
          multipart/mixed
            multipart/alternative
              text/plain                          (açıklama)
              text/calendar; method=REQUEST       (daveti gösteren bacak)
            application/ics                       (invite.ics eki)
        """
        start = start or (datetime.now() + timedelta(days=1)).replace(
            minute=0, second=0, microsecond=0)
        end = start + timedelta(minutes=duration_minutes)
        uid = f"{uuid.uuid4().hex}@mail-otomasyon"

        ics = _build_ics(
            uid=uid, summary=summary, description=body, location=location,
            start=start, end=end,
            organizer=self.from_address, attendee=to_address,
        )

        msg = MIMEMultipart("mixed")
        msg_id = make_msgid()
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))

        cal_part = MIMEText(ics, "calendar", "utf-8")
        cal_part.set_param("method", "REQUEST")
        cal_part.set_param("component", "VEVENT")
        alt.attach(cal_part)
        msg.attach(alt)

        ics_attachment = MIMEBase("application", "ics")
        ics_attachment.set_payload(ics.encode("utf-8"))
        encoders.encode_base64(ics_attachment)
        ics_attachment.add_header("Content-Disposition",
                                  'attachment; filename="invite.ics"')
        msg.attach(ics_attachment)

        sent_at = self._send(msg, to_address)
        logger.info(f"[CALENDAR] Davet gönderildi → {to_address} | uid={uid}")
        return {
            "msg_id": msg_id,
            "sent_at": sent_at,
            "scenario": "calendar_invite",
            "ics_uid": uid,
            "ics_method": "REQUEST",
            "event_summary": summary,
            "event_start": start.strftime("%Y-%m-%d %H:%M"),
            "ics_size": len(ics.encode("utf-8")),
        }

    # ------------------------------------------------------------------ #
    #  Senaryo 9: Mesaj iletme (Forward)
    # ------------------------------------------------------------------ #
    def send_forward(self, to_address: str, subject: str, intro_body: str,
                     original: dict) -> dict:
        """Orijinal mesajı message/rfc822 olarak kapsülleyip iletir.

        ``original`` sözlüğü: subject, from, to, date, msg_id, body.
        Gövdeye ayrıca klasik '--- İletilen Mesaj ---' başlık bloğu eklenir;
        message/rfc822 ekini açamayan istemcilerde bilgi kaybolmasın.
        """
        fwd_subject = subject if subject.lower().startswith("fwd:") else f"Fwd: {subject}"

        msg = MIMEMultipart("mixed")
        msg_id = make_msgid()
        for k, v in self._base_headers(fwd_subject, to_address, msg_id).items():
            msg[k] = v

        header_block = (
            "\n\n---------- İletilen Mesaj ----------\n"
            f"Kimden: {original.get('from', '')}\n"
            f"Tarih: {original.get('date', '')}\n"
            f"Konu: {original.get('subject', '')}\n"
            f"Kime: {original.get('to', '')}\n"
            f"Message-ID: {original.get('msg_id', '')}\n\n"
            f"{original.get('body', '')}\n"
        )
        msg.attach(MIMEText(intro_body + header_block, "plain", "utf-8"))

        inner = MIMEText(original.get("body", ""), "plain", "utf-8")
        for header, key in (("Subject", "subject"), ("From", "from"),
                            ("To", "to"), ("Date", "date"), ("Message-ID", "msg_id")):
            value = original.get(key)
            if value:
                inner[header] = value

        rfc822 = MIMEMessage(inner)
        rfc822.add_header("Content-Disposition", "attachment",
                          filename="iletilen_mesaj.eml")
        msg.attach(rfc822)

        sent_at = self._send(msg, to_address)
        logger.info(f"[FORWARD] Gönderildi → {to_address} | "
                    f"orijinal={original.get('msg_id', '?')}")
        return {
            "msg_id": msg_id,
            "sent_at": sent_at,
            "scenario": "forward",
            "original_msg_id": original.get("msg_id", ""),
            "original_subject": original.get("subject", ""),
        }

    # ------------------------------------------------------------------ #
    #  Senaryo 5: Reply Chain
    # ------------------------------------------------------------------ #
    def send_reply(self, to_address: str, original_subject: str,
                   original_msg_id: str, original_references: str,
                   reply_body: str) -> dict:
        """Orijinal mesaja thread zinciri korunarak cevap verir."""
        subject = f"Re: {original_subject}" if not original_subject.startswith("Re:") else original_subject
        msg = MIMEMultipart("alternative")
        msg_id = make_msgid()
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        # Thread headers
        msg["In-Reply-To"] = original_msg_id
        refs = f"{original_references} {original_msg_id}".strip() if original_references else original_msg_id
        msg["References"] = refs

        # Alıntılı gövde
        quoted_body = "\n".join([f"> {line}" for line in "Orijinal mesaj içeriği.".split("\n")])
        full_body = f"{reply_body}\n\n{quoted_body}"
        msg.attach(MIMEText(full_body, "plain", "utf-8"))

        sent_at = self._send(msg, to_address)
        logger.info(f"[REPLY] Gönderildi → {to_address} | in-reply-to={original_msg_id}")
        return {
            "msg_id": msg_id,
            "sent_at": sent_at,
            "scenario": "reply_chain",
            "in_reply_to": original_msg_id,
            "references": refs,
        }

    # ------------------------------------------------------------------ #
    #  İç yardımcılar
    # ------------------------------------------------------------------ #
    def _send(self, msg, to_address: str) -> float:
        # Zarf adresleri: görünen ad atılır, IDN alan adı A-label'a çevrilir;
        # "a@x, b@y" / "a@x; b@y" ayrı RCPT olur.
        recipients = [addr for _, addr in parse_addresses(to_address)]
        envelope_from = parse_addresses(self.from_address)[0][1]
        # Yalnızca ADRESLER ASCII dışıysa SMTPUTF8 gerekir; Türkçe görünen ad
        # header'da RFC 2047 ile kodlanır (eskiden gereksiz SMTPUTF8 isteniyordu).
        needs_utf8 = not (envelope_from + "".join(recipients)).isascii()
        with self._connect() as smtp:
            if needs_utf8:
                if not smtp.has_extn("smtputf8"):
                    raise smtplib.SMTPNotSupportedError(
                        "ASCII olmayan e-posta adresi (ör. ğüş@örnek.com) için sunucunun "
                        "SMTPUTF8 desteği gerekli; bu sunucu SMTPUTF8 bildirmiyor.")
                refused = smtp.sendmail(envelope_from, recipients,
                                        msg.as_bytes(policy=SMTPUTF8_POLICY),
                                        mail_options=["SMTPUTF8"])
            else:
                # CRLF satır sonları: compat32 yalın LF üretir; katı RFC 5321
                # sunucuları (aiosmtpd, Exim >= 4.97) mesajı reddeder.
                refused = smtp.sendmail(envelope_from, recipients,
                                        msg.as_bytes(policy=SMTP_POLICY))
        if isinstance(refused, dict) and refused:
            # Alıcılardan biri reddedildi: smtplib yalnızca HEPSİ reddedilirse
            # hata atar; eskiden kısmi ret "gönderildi" sayılıyordu.
            raise smtplib.SMTPRecipientsRefused(refused)
        return time.time()

    @staticmethod
    def _guess_mime(filename: str) -> str:
        ext = filename.lower().rsplit(".", 1)[-1]
        mapping = {
            "pdf": "application/pdf",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "txt": "text/plain",
            "csv": "text/csv",
            "ics": "text/calendar",
            "eml": "message/rfc822",
            "zip": "application/zip",
        }
        return mapping.get(ext, "application/octet-stream")
