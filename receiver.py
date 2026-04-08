"""
receiver.py — Farklı protokoller (IMAP, EWS, Graph API) üzerinden mesaj bekleyen erişim adaptörleri.
"""

import imaplib
import email
import ssl
import time
import logging
import requests
from email.header import decode_header
from typing import Optional
from abc import ABC, abstractmethod
from oauth_manager import get_oauth_manager

logger = logging.getLogger(__name__)

def create_receiver(server_config: dict):
    """Konfigürasyona göre doğru erişim protokolünü seçer ve receiver sınıfını üretir."""
    protocol = server_config.get("protocol", "imap").lower()
    
    if protocol == "ews":
        return EwsReceiver(server_config)
    elif protocol == "graph":
        return GraphReceiver(server_config)
    else:
        return ImapReceiver(server_config)


class BaseReceiver(ABC):
    def __init__(self, server_config: dict):
        self.config = server_config
        self.username = server_config.get("username", "")
        self.password = server_config.get("password", "")

    @abstractmethod
    def wait_for_message(self, expected_msg_id: str, subject_prefix: str,
                         wait_seconds: int = 20, max_retries: int = 5,
                         retry_interval: int = 5) -> Optional[dict]:
        """Gelen kutusunu tarar. Beklenen mesajı bulursa parçalarına ayırıp (extract) döner."""
        pass

    def _extract_details(self, msg: email.message.Message, raw: bytes, msg_id_str: str) -> dict:
        """Mesajdan tüm test için gerekli bilgileri çıkarır."""
        result = {
            "imap_id": msg_id_str, # IMAP uid veya EWS itemId
            "raw_size": len(raw),
            "raw_bytes": raw,  # Claude/Gemini API analizi
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
                "inline" in disposition.lower()
                or content_type.startswith("image/")
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
                result["parts"].append({**part_info, "text_preview": text[:500], "full_text": text})

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


class ImapReceiver(BaseReceiver):
    """Mevcut IMAP erişim sınıfımız."""
    def __init__(self, server_config: dict):
        super().__init__(server_config)
        self.host = server_config["imap_host"]
        self.port = server_config["imap_port"]
        self.use_ssl = server_config.get("imap_use_ssl", True)

    def _make_ssl_ctx(self) -> ssl.SSLContext:
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
            
        auth_method = self.config.get("auth_method", "password")
        if auth_method == "oauth2":
            self._authenticate_oauth2(imap)
        else:
            imap.login(self.username, self.password)
            
        imap.select("INBOX")
        return imap

    def _authenticate_oauth2(self, imap: imaplib.IMAP4):
        """IMAP XOAUTH2 kimlik doğrulaması yapar."""
        mgr = get_oauth_manager()
        token = None
        server_key = "outlook" if "office365" in self.host.lower() else "gmail" if "gmail" in self.host.lower() else None
        
        if server_key == "outlook":
            oauth_cfg = self.config.get("oauth2", {})
            token = mgr.get_ms_token(oauth_cfg.get("client_id"), oauth_cfg.get("tenant_id", "common"))
        elif server_key == "gmail":
            token = mgr.get_google_token()

        if not token:
            raise ConnectionError(f"OAuth2 Token alınamadı ({server_key}). Lütfen yetkilendirme akışını tamamlayın.")

        auth_str = mgr.generate_xoauth2_string(self.username, token)
        imap.authenticate('XOAUTH2', lambda x: auth_str)

    def wait_for_message(self, expected_msg_id: str, subject_prefix: str,
                         wait_seconds: int = 20, max_retries: int = 5,
                         retry_interval: int = 5) -> Optional[dict]:
        logger.info(f"[IMAP] Mesaj bekleniyor | msg_id={expected_msg_id}")
        time.sleep(wait_seconds)
        clean_msg_id = expected_msg_id.strip().strip("<>")

        for attempt in range(1, max_retries + 1):
            try:
                imap = self._connect()
                _, uid_data = imap.uid("search", None, f'HEADER "Message-ID" "<{clean_msg_id}>"')
                uids = uid_data[0].split() if uid_data and uid_data[0] else []

                if not uids and subject_prefix:
                    _, uid_data2 = imap.uid("search", None, f'SUBJECT "{subject_prefix}"')
                    all_uids = uid_data2[0].split() if uid_data2 and uid_data2[0] else []
                    uids = all_uids[-20:]

                if not uids:
                    _, uid_data3 = imap.uid("search", None, "ALL")
                    all_uids = uid_data3[0].split() if uid_data3 and uid_data3[0] else []
                    uids = all_uids[-30:]

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
                        found = self._extract_details(parsed, raw, uid.decode())
                        break

                imap.logout()
                if found:
                    return found

                time.sleep(retry_interval)
            except Exception as e:
                logger.error(f"[IMAP] hatası (deneme {attempt}): {e}")
                time.sleep(retry_interval)
        return None

class EwsReceiver(BaseReceiver):
    """Exchange Web Services üzerinden alıcı simülasyonu stub."""
    def wait_for_message(self, expected_msg_id: str, subject_prefix: str,
                         wait_seconds: int = 20, max_retries: int = 5,
                         retry_interval: int = 5) -> Optional[dict]:
        logger.info(f"[EWS] Exchange Web Services üzerinden mesaj bekleniyor: {expected_msg_id}")
        # exchangelib veya pyews entegrasyon bölgesi
        # TODO: Implement EWS fetch
        logger.warning("[EWS] Adaptörü henüz tam entegre edilmedi (Mock modu).")
        return None

class GraphReceiver(BaseReceiver):
    """Microsoft Graph API (OAuth) üzerinden mesajları çeken adaptör."""
    def wait_for_message(self, expected_msg_id: str, subject_prefix: str,
                         wait_seconds: int = 20, max_retries: int = 5,
                         retry_interval: int = 5) -> Optional[dict]:
        logger.info(f"[Graph API] Mesaj aranıyor: {expected_msg_id}")
        time.sleep(wait_seconds)
        
        mgr = get_oauth_manager()
        oauth_cfg = self.config.get("oauth2", {})
        
        for attempt in range(1, max_retries + 1):
            token = mgr.get_ms_token(oauth_cfg.get("client_id"), oauth_cfg.get("tenant_id", "common"))
            if not token:
                logger.error("[Graph API] Token alınamadı!")
                return None

            # Graph API filter query
            # internetMessageId sistem tarafından üretilen message-id header'ıdır
            clean_id = expected_msg_id.strip()
            url = f"https://graph.microsoft.com/v1.0/me/messages?$filter=internetMessageId eq '{clean_id}'&$select=id,subject,internetMessageId,from,toRecipients,receivedDateTime,hasAttachments"
            
            try:
                headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
                resp = requests.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json().get("value", [])
                    if data:
                        # Mesaj bulundu, şimdi ham (MIME) içeriğini alalım
                        msg_id = data[0]["id"]
                        content_url = f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}/$value"
                        raw_resp = requests.get(content_url, headers=headers)
                        if raw_resp.status_code == 200:
                            raw = raw_resp.content
                            parsed = email.message_from_bytes(raw)
                            return self._extract_details(parsed, raw, msg_id)
                else:
                    logger.warning(f"[Graph API] Hata {resp.status_code}: {resp.text}")
            except Exception as e:
                logger.error(f"[Graph API] İstek hatası: {e}")

            time.sleep(retry_interval)
        return None
