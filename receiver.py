"""
receiver.py — IMAP üzerinden mesajı polling ile bekler ve ham içeriği döndürür.
"""

import imaplib
import email
import ssl
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

    def _make_ssl_ctx(self) -> ssl.SSLContext:
        """Self-signed / internal cert desteği. imap_verify_ssl: false → doğrulama atla."""
        verify = self.config.get("imap_verify_ssl", False)
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _connect(self):
        if self.use_ssl:
            imap = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=self._make_ssl_ctx())
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
        UID tabanlı komutlar kullanır (sequence number'lar Gmail'de unstable olabilir).
        Returns: Mesaj detayları içeren dict veya None (bulunamazsa)
        """
        logger.info(f"Mesaj bekleniyor | msg_id={expected_msg_id} | max_wait={wait_seconds}s")
        time.sleep(wait_seconds)  # İlk bekleme — mesajın gelmesi için

        # Message-ID'den açılı parantezleri temizle (arama için)
        clean_msg_id = expected_msg_id.strip().strip("<>")

        for attempt in range(1, max_retries + 1):
            try:
                imap = self._connect()

                # Strateji 1: Tam Message-ID header araması (en kesin)
                _, uid_data = imap.uid("search", None, f'HEADER "Message-ID" "<{clean_msg_id}>"')
                uids = uid_data[0].split() if uid_data and uid_data[0] else []

                # Strateji 2: Subject prefix ile ara (boş değilse)
                if not uids and subject_prefix:
                    _, uid_data2 = imap.uid("search", None, f'SUBJECT "{subject_prefix}"')
                    all_uids = uid_data2[0].split() if uid_data2 and uid_data2[0] else []
                    uids = all_uids[-20:]

                # Strateji 3: Son 30 mesajı doğrudan tara (bazı sunucular
                # HEADER aramasını multipart mesajlarda düzgün desteklemez)
                if not uids:
                    _, uid_data3 = imap.uid("search", None, "ALL")
                    all_uids = uid_data3[0].split() if uid_data3 and uid_data3[0] else []
                    uids = all_uids[-30:]  # Son 30 mesaj

                found = None
                for uid in reversed(uids):
                    _, msg_data = imap.uid("fetch", uid, "(RFC822)")
                    if not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
                        continue
                    raw = msg_data[0][1]
                    if not isinstance(raw, bytes):
                        continue
                    parsed = email.message_from_bytes(raw)

                    if parsed.get("Message-ID", "").strip() == expected_msg_id.strip():
                        logger.info(f"✅ Mesaj bulundu (deneme {attempt}/{max_retries})")
                        found = self._extract_details(parsed, raw, uid.decode())
                        break

                imap.logout()
                if found:
                    return found

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

            if "attachment" in disposition.lower():
                filename = part.get_filename() or ""
                payload = part.get_payload(decode=True)
                result["attachments"].append({
                    **part_info,
                    "filename": self._decode_header(filename),
                    "size": len(payload) if payload else 0,
                    "payload_sample": payload[:64] if payload else None,
                })
            elif content_id and (
                # Content-ID + inline disposition (ideal case)
                "inline" in disposition.lower()
                # Content-ID + image type but server stripped Content-Disposition (Gmail etc.)
                or content_type.startswith("image/")
                # Content-ID with no disposition set at all
                or (not disposition and content_id)
            ):
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
