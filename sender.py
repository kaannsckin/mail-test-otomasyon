"""
sender.py — SMTP üzerinden test mesajlarını gönderir.
Her senaryo tipi için ayrı metot: plain, attachment, inline_image, smime, reply.
"""
from __future__ import annotations

import smtplib
import ssl
import uuid
import time
import os
import base64
import logging
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from email.utils import formatdate, make_msgid
from email.headerregistry import Address
from typing import Optional

logger = logging.getLogger(__name__)


class MailSender:
    def __init__(self, server_config: dict):
        self.config = server_config
        self.host = server_config["smtp_host"]
        self.port = server_config["smtp_port"]
        self.use_tls = server_config.get("smtp_use_tls", True)
        self.username = server_config["username"]
        self.password = server_config["password"]
        self.from_address = server_config.get("test_address") or server_config.get("username", "")

    def _make_ssl_ctx(self) -> ssl.SSLContext:
        """Self-signed / internal cert desteği. smtp_verify_ssl: false → doğrulama atla."""
        verify = self.config.get("smtp_verify_ssl", False)
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _connect(self) -> smtplib.SMTP:
        # smtp_use_ssl: true  → Implicit SSL (SMTP_SSL) — sadece config'de açıkça belirtilince
        # smtp_use_ssl: false → STARTTLS veya plain — port 465 bile olsa
        use_ssl = self.config.get("smtp_use_ssl", False)
        ssl_ctx = self._make_ssl_ctx()

        if use_ssl:
            # Implicit SSL (SMTP_SSL) — sunucu direkt TLS bekliyor
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=60, context=ssl_ctx)
            smtp.ehlo()
        else:
            # STARTTLS veya plain — port 465 bile olsa
            smtp = smtplib.SMTP(self.host, self.port, timeout=60)
            smtp.ehlo()
            if self.use_tls:
                smtp.starttls(context=ssl_ctx)
                smtp.ehlo()

        # Bazı EMS sunucuları SIZE parametresini EHLO'da ilan edip
        # MAIL FROM'da reddeder (555 hatası). SIZE'ı kaldırarak smtplib'in
        # otomatik "size=N" eklemesini engelle.
        smtp.esmtp_features.pop("size", None)

        smtp.login(self.username, self.password)
        logger.debug(f"SMTP bağlantısı kuruldu: {self.host}:{self.port} "
                     f"({'implicit SSL' if use_ssl else 'STARTTLS' if self.use_tls else 'plain'})")
        return smtp

    def _base_headers(self, subject: str, to_address: str, msg_id: Optional[str] = None) -> dict:
        return {
            "Message-ID": msg_id or make_msgid(),
            "Date": formatdate(localtime=True),
            "Subject": subject,
            "From": self.from_address,
            "To": to_address,
            "X-Test-Automation": "mail-otomasyon-v1",
        }

    # ------------------------------------------------------------------ #
    #  Senaryo 1: Plain Text
    # ------------------------------------------------------------------ #
    def send_plain_text(self, to_address: str, subject: str, body: str, sender_client: str = None) -> dict:
        """UTF-8 plain-text mesaj gönderir."""
        msg = MIMEText(body, "plain", "utf-8")
        msg_id = make_msgid()
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        sent_at = self._send(msg, to_address, sender_client=sender_client)
        logger.info(f"[PLAIN] Gönderildi → {to_address} | msg_id={msg_id}")
        return {"msg_id": msg_id, "sent_at": sent_at, "scenario": "plain_text"}

    # ------------------------------------------------------------------ #
    #  Senaryo 2: Attachment (tek veya çoklu)
    # ------------------------------------------------------------------ #
    def send_with_attachment(self, to_address: str, subject: str, body: str,
                             attachment_path: str | list[str], sender_client: str = None) -> dict:
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
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            part.add_header("Content-Type", self._guess_mime(filename))
            msg.attach(part)
            attached_files.append({"name": filename, "size": os.path.getsize(p)})

        sent_at = self._send(msg, to_address, sender_client=sender_client)
        names = ", ".join(a["name"] for a in attached_files)
        logger.info(f"[ATTACH] Gönderildi → {to_address} | dosyalar={names}")
        return {
            "msg_id": msg_id,
            "sent_at": sent_at,
            "scenario": "attachment",
            "attachment_name": names,
            "attachment_count": len(attached_files),
            "attachments": attached_files,
        }

    # ------------------------------------------------------------------ #
    #  Senaryo 3: Inline Image (Embedded HTML)
    # ------------------------------------------------------------------ #
    def send_inline_image(self, to_address: str, subject: str,
                          image_path: str, html_body: str | None = None, sender_client: str = None) -> dict:
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
        plain_part = MIMEText(
            "Inline resim testi — bu mesajı görüntülemek için HTML destekli istemci gereklidir. "
            "Türkçe: ğüşıöçĞÜŞİÖÇ",
            "plain", "utf-8",
        )
        html_part = MIMEText(html_content, "html", "utf-8")
        # EMS sunucusu base64'ü yanlış yorumlamaması için QP encoding zorla
        html_part.replace_header("Content-Transfer-Encoding", "quoted-printable")
        import quopri
        encoded = quopri.encodestring(html_content.encode("utf-8")).decode("ascii")
        html_part.set_payload(encoded)
        alt_part.attach(plain_part)
        alt_part.attach(html_part)
        msg.attach(alt_part)

        with open(image_path, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=os.path.basename(image_path))
        msg.attach(img)

        sent_at = self._send(msg, to_address, sender_client=sender_client)
        logger.info(f"[INLINE] Gönderildi → {to_address} | cid={cid}")
        return {"msg_id": msg_id, "sent_at": sent_at, "scenario": "inline_image", "cid": cid}

    # ------------------------------------------------------------------ #
    #  Senaryo 4: S/MIME İmzalı Mesaj
    # ------------------------------------------------------------------ #
    def send_smime_signed(self, to_address: str, subject: str, body: str,
                          cert_path: str, key_path: str, sender_client: str = None) -> dict:
        """
        S/MIME imzalı mesaj gönderir.
        Gereksinim: openssl kütüphanesi ve test sertifikası.
        cert_path: PEM formatında sertifika
        key_path: PEM formatında özel anahtar
        """
        try:
            from OpenSSL import crypto
            from email import message_from_bytes
        except ImportError:
            logger.warning("pyOpenSSL kurulu değil. S/MIME testi atlanıyor.")
            return {"msg_id": None, "sent_at": None, "scenario": "smime", "skipped": True,
                    "skip_reason": "pyOpenSSL kurulu değil"}

        # İmzalı MIME oluştur
        msg_id = make_msgid()
        inner = MIMEText(body, "plain", "utf-8")
        inner["Content-Type"] = "text/plain; charset=utf-8"
        
        if sender_client:
            from client_profiles import apply_client_profile
            inner = apply_client_profile(inner, sender_client)

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
            "-text"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        os.unlink(tmp_path)

        if result.returncode != 0:
            logger.error(f"S/MIME imzalama hatası: {result.stderr}")
            return {"msg_id": msg_id, "sent_at": None, "scenario": "smime",
                    "error": result.stderr}

        with open(signed_path, "rb") as f:
            signed_content = f.read()
        os.unlink(signed_path)

        with self._connect() as smtp:
            smtp.sendmail(self.from_address, [to_address], signed_content)

        sent_at = time.time()
        logger.info(f"[SMIME] İmzalı mesaj gönderildi → {to_address}")
        return {"msg_id": msg_id, "sent_at": sent_at, "scenario": "smime", "signed": True}

    # ------------------------------------------------------------------ #
    #  Senaryo 5: Reply Chain
    # ------------------------------------------------------------------ #
    def send_reply(self, to_address: str, original_subject: str,
                   original_msg_id: str, original_references: str,
                   reply_body: str, sender_client: str = None) -> dict:
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

        sent_at = self._send(msg, to_address, sender_client=sender_client)
        logger.info(f"[REPLY] Gönderildi → {to_address} | in-reply-to={original_msg_id}")
        return {
            "msg_id": msg_id,
            "sent_at": sent_at,
            "scenario": "reply_chain",
            "in_reply_to": original_msg_id,
            "references": refs,
        }

    # ------------------------------------------------------------------ #
    #  Yeni Senaryolar: Calendar, Rich HTML, Forward
    # ------------------------------------------------------------------ #
    def send_calendar_invite(self, to_address: str, subject: str, body: str, sender_client: str = None) -> dict:
        """vCalendar (iTIP) daveti gönderir."""
        msg = MIMEMultipart("alternative")
        msg_id = make_msgid()
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        msg.attach(MIMEText(body, "plain", "utf-8"))

        # Benzersiz ICS UID ve Zaman Mühürleri oluştur
        # Start time: Yarın saat 10:00 (basit UTC zulu zamanı)
        dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dtstart = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=10, minute=0, second=0).strftime("%Y%m%dT%H%M%SZ")
        dtend = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=11, minute=0, second=0).strftime("%Y%m%dT%H%M%SZ")
        uid = f"invite_{uuid.uuid4().hex}@test.local"

        ics_content = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Mail Otomasyon//TR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:{dtstamp}\r\nDTSTART:{dtstart}\r\nDTEND:{dtend}\r\nSUMMARY:{subject}\r\nDESCRIPTION:{body}\r\nORGANIZER;CN=Test Gönderici:mailto:{self.from_address}\r\nATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{to_address}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"

        cal_part = MIMEText(ics_content, "calendar", "utf-8")
        cal_part.set_param("method", "REQUEST")
        msg.attach(cal_part)

        sent_at = self._send(msg, to_address, sender_client=sender_client)
        logger.info(f"[CALENDAR] Takvim Daveti Gönderildi → {to_address}")
        return {"msg_id": msg_id, "sent_at": sent_at, "scenario": "calendar_invite"}

    def send_complex_html(self, to_address: str, subject: str, html_body: str, sender_client: str = None) -> dict:
        """Geniş CSS ve I18N destekli kompleks HTML yollar."""
        msg = MIMEMultipart("alternative")
        msg_id = make_msgid()
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        # Fallback düz metin
        text_body = "Bu mesaj zengin HTML içeriğine sahiptir. Lütfen destekleyen bir istemci kullanın.\n\n"
        plain_part = MIMEText(text_body, "plain", "utf-8")
        html_part = MIMEText(html_body, "html", "utf-8")
        # EMS sunucusu base64'ü yanlış yorumlamaması için QP encoding zorla
        html_part.replace_header("Content-Transfer-Encoding", "quoted-printable")
        import quopri
        encoded = quopri.encodestring(html_body.encode("utf-8")).decode("ascii")
        html_part.set_payload(encoded)
        msg.attach(plain_part)
        msg.attach(html_part)

        sent_at = self._send(msg, to_address, sender_client=sender_client)
        logger.info(f"[COMPLEX_HTML] Zengin Formatlı HTML Gönderildi → {to_address}")
        return {"msg_id": msg_id, "sent_at": sent_at, "scenario": "complex_html"}

    def send_forward(self, to_address: str, original_subject: str, original_from: str, forward_body: str, original_body: str, sender_client: str = None) -> dict:
        """Forwarded (İletilmiş) mesaj senaryosu."""
        msg = MIMEMultipart("alternative")
        msg_id = make_msgid()
        # İletilen mesajlarda genel konvansiyon başa Fwd: (veya FW:) eklenmesidir
        subject = f"Fwd: {original_subject}"
        for k, v in self._base_headers(subject, to_address, msg_id).items():
            msg[k] = v

        orig_date = datetime.now().strftime("%a, %d %b %Y %H:%M")
        
        # Standart bir istemcinin Forward string formasyonunu taklit et
        forward_wrapper = (
            f"{forward_body}\n\n"
            "---------- Forwarded message ---------\n"
            f"From: {original_from}\n"
            f"Date: {orig_date}\n"
            f"Subject: {original_subject}\n"
            f"To: {to_address}\n\n"
            f"{original_body}"
        )

        msg.attach(MIMEText(forward_wrapper, "plain", "utf-8"))
        
        sent_at = self._send(msg, to_address, sender_client=sender_client)
        logger.info(f"[FORWARD] İletilmiş Mesaj Gönderildi → {to_address}")
        return {"msg_id": msg_id, "sent_at": sent_at, "scenario": "forward"}

    def send_html_table(self, to_address: str, subject: str, html_body: str, sender_client: str = None) -> dict:
        """Tablo içeren HTML mesajı gönderir. Complex HTML ile benzerdir ancak senaryo ayrımı sağlar."""
        return self.send_complex_html(to_address, subject, html_body, sender_client=sender_client)

    # ------------------------------------------------------------------ #
    #  İç yardımcılar
    # ------------------------------------------------------------------ #
    def _send(self, msg, to_address: str, max_retries: int = 2, sender_client: str = None) -> float:
        """Mesaj gönder; hata olursa max_retries kez yeniden dene."""
        if sender_client:
            from client_profiles import apply_client_profile
            msg = apply_client_profile(msg, sender_client)
            
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                with self._connect() as smtp:
                    # as_bytes() EMS'de base64 scan hatasına yol açıyor;
                    # as_string() → encode daha güvenli
                    smtp.sendmail(
                        self.from_address, [to_address],
                        msg.as_string().encode("utf-8", "replace")
                    )
                return time.time()
            except smtplib.SMTPServerDisconnected as e:
                last_err = e
                logger.warning(f"SMTP bağlantısı koptu (deneme {attempt+1}/{max_retries+1}), {2**attempt}s sonra yeniden deneniyor: {e}")
                time.sleep(2 ** attempt)  # 1s, 2s, ...
            except smtplib.SMTPException as e:
                # Auth/reject hataları — yeniden deneme faydasız
                err_str = str(e).lower()
                flags = []
                if "spam" in err_str or "policy" in err_str or "reject" in err_str or "blocked" in err_str:
                    flags.append("[SPAM/REJECT]")
                if "relay" in err_str:
                    flags.append("[RELAY_DENIED]")
                
                if flags:
                    logger.error(f"🚨 SPOOF TRACE {' '.join(flags)}: Sahte istemci ({sender_client or 'Varsayılan'}) SMTP filtrelerine takıldı: {e}")
                raise
            except OSError as e:
                last_err = e
                logger.warning(f"Ağ hatası (deneme {attempt+1}/{max_retries+1}): {e}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
        raise last_err  # type: ignore

    @staticmethod
    def _guess_mime(filename: str) -> str:
        ext = filename.lower().rsplit(".", 1)[-1]
        mapping = {
            "pdf": "application/pdf",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "txt": "text/plain",
            "zip": "application/zip",
        }
        return mapping.get(ext, "application/octet-stream")
