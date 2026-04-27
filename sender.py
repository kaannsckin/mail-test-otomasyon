"""
sender.py — SMTP üzerinden test mesajlarını gönderir.
Her senaryo tipi için ayrı metot: plain, attachment, inline_image, smime, reply.
"""
from __future__ import annotations

import smtplib
import uuid
import time
import os
import base64
import logging
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
        self.from_address = server_config["test_address"]

    def _connect(self) -> smtplib.SMTP:
        smtp = smtplib.SMTP(self.host, self.port, timeout=30)
        smtp.ehlo()
        if self.use_tls:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(self.username, self.password)
        logger.debug(f"SMTP bağlantısı kuruldu: {self.host}:{self.port}")
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
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            part.replace_header("Content-Type", self._guess_mime(filename))
            msg.attach(part)
            attached_files.append({"name": filename, "size": os.path.getsize(p)})

        sent_at = self._send(msg, to_address)
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

        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.ehlo()
            if self.use_tls:
                smtp.starttls()
            smtp.login(self.username, self.password)
            smtp.sendmail(self.from_address, [to_address], signed_content)

        sent_at = time.time()
        logger.info(f"[SMIME] İmzalı mesaj gönderildi → {to_address}")
        return {"msg_id": msg_id, "sent_at": sent_at, "scenario": "smime", "signed": True}

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
        with self._connect() as smtp:
            smtp.sendmail(self.from_address, [to_address], msg.as_bytes())
        return time.time()

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
