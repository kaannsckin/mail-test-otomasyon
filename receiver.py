"""
receiver.py — IMAP üzerinden mesajı polling ile bekler ve ham içeriği döndürür.
"""

import imaplib
import email
import time
import logging
from email.header import decode_header
from typing import Optional

logger = logging.getLogger(__name__)


class MailReceiver:
    def __init__(self, server_config: dict):
        self.config = server_config
        self.host = server_config["imap_host"]
        self.port = server_config["imap_port"]
        self.use_ssl = server_config.get("imap_use_ssl", True)
        self.username = server_config["username"]
        self.password = server_config["password"]

    def _connect(self) -> imaplib.IMAP4_SSL:
        if self.use_ssl:
            imap = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            imap = imaplib.IMAP4(self.host, self.port)
        imap.login(self.username, self.password)
        imap.select("INBOX")
        return imap

    def wait_for_message(self, expected_msg_id: str, subject_prefix: str,
                         wait_seconds: int = 20, max_retries: int = 5,
                         retry_interval: int = 5) -> Optional[dict]:
        """
        Gelen kutusunu polling ile tarar. Beklenen Message-ID'li mesajı bulunca döndürür.
        Returns: Mesaj detayları içeren dict veya None (bulunamazsa)
        """
        logger.info(f"Mesaj bekleniyor | msg_id={expected_msg_id} | max_wait={wait_seconds}s")
        time.sleep(wait_seconds)  # İlk bekleme — mesajın gelmesi için

        for attempt in range(1, max_retries + 1):
            try:
                imap = self._connect()
                # Subject ile ara (Message-ID header'ı bazı sunucularda filtrelenebilir)
                _, data = imap.search(None, f'SUBJECT "{subject_prefix}"')
                mail_ids = data[0].split()

                for mail_id in reversed(mail_ids[-20:]):  # Son 20 mesaja bak
                    _, msg_data = imap.fetch(mail_id, "(RFC822)")
                    raw = msg_data[0][1]
                    parsed = email.message_from_bytes(raw)

                    if parsed.get("Message-ID", "").strip() == expected_msg_id.strip():
                        logger.info(f"✅ Mesaj bulundu (deneme {attempt}/{max_retries})")
                        imap.logout()
                        return self._extract_details(parsed, raw, mail_id.decode())

                imap.logout()
                logger.debug(f"Mesaj bulunamadı (deneme {attempt}/{max_retries}), {retry_interval}s bekleniyor...")
                time.sleep(retry_interval)

            except Exception as e:
                logger.error(f"IMAP hatası (deneme {attempt}): {e}")
                time.sleep(retry_interval)

        logger.warning(f"❌ Mesaj {max_retries} denemede bulunamadı | msg_id={expected_msg_id}")
        return None

    def _extract_details(self, msg: email.message.Message, raw: bytes, imap_id: str) -> dict:
        """Mesajdan tüm test için gerekli bilgileri çıkarır."""
        result = {
            "imap_id": imap_id,
            "raw_size": len(raw),
            "raw_bytes": raw,  # Claude API analizi için ham içerik
            "headers": {
                "message_id": msg.get("Message-ID", ""),
                "from": msg.get("From", ""),
                "to": msg.get("To", ""),
                "subject": self._decode_header(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
                "content_type": msg.get("Content-Type", ""),
                "in_reply_to": msg.get("In-Reply-To", ""),
                "references": msg.get("References", ""),
                "mime_version": msg.get("MIME-Version", ""),
                "x_mailer": msg.get("X-Mailer", ""),
                "content_transfer_encoding": msg.get("Content-Transfer-Encoding", ""),
            },
            "parts": [],
            "attachments": [],
            "inline_images": [],
            "is_multipart": msg.is_multipart(),
        }

        self._walk_parts(msg, result)
        return result

    def _walk_parts(self, msg, result: dict):
        """MIME ağacını dolaşarak part'ları kategorize eder."""
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = part.get("Content-Disposition", "")
            content_id = part.get("Content-ID", "")

            part_info = {
                "content_type": content_type,
                "charset": part.get_content_charset(),
                "content_id": content_id,
                "disposition": disposition,
                "transfer_encoding": part.get("Content-Transfer-Encoding", ""),
            }

            if "attachment" in disposition:
                filename = part.get_filename() or ""
                payload = part.get_payload(decode=True)
                result["attachments"].append({
                    **part_info,
                    "filename": self._decode_header(filename),
                    "size": len(payload) if payload else 0,
                    "payload_sample": payload[:64] if payload else None,
                })
            elif content_id and "inline" in disposition:
                payload = part.get_payload(decode=True)
                result["inline_images"].append({
                    **part_info,
                    "cid": content_id.strip("<>"),
                    "size": len(payload) if payload else 0,
                })
            else:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace") if payload else ""
                except Exception:
                    text = ""
                result["parts"].append({**part_info, "text_preview": text[:500]})

    @staticmethod
    def _decode_header(value: str) -> str:
        if not value:
            return ""
        decoded_parts = decode_header(value)
        parts = []
        for text, charset in decoded_parts:
            if isinstance(text, bytes):
                parts.append(text.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(text)
        return "".join(parts)
