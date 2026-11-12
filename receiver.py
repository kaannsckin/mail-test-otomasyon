"""
receiver.py — IMAP üzerinden mesajı polling ile bekler ve ham içeriği döndürür.
"""

import imaplib
import os
import email
import time
import logging
from datetime import datetime, timedelta
from email.header import decode_header
from email.parser import BytesHeaderParser
from typing import Optional

from auth_manager import generate_totp, mfa_manager
from mail_transport import imap_connect

logger = logging.getLogger(__name__)


def _interactive_mfa_enabled() -> bool:
    """Web arayüzü subprocess'i MFA_INTERACTIVE=1 ile başlatır; modal akışı
    yalnızca bu durumda devreye girer (yalın CLI'da 5 dk bloklamamak için)."""
    return os.environ.get("MFA_INTERACTIVE", "") == "1"


class MailReceiver:
    def __init__(self, server_config: dict):
        self.config = server_config
        self.host = server_config["imap_host"]
        self.port = server_config["imap_port"]
        self.use_ssl = server_config.get("imap_use_ssl", True)
        # YAML'da 'password: 123456' int olarak okunur; imaplib str ister.
        self.username = str(server_config.get("username") or "")
        self.password = str(server_config.get("password") or "")
        self.auth_method = server_config.get("auth_method", "password")
        self.totp_secret = str(server_config.get("totp_secret") or "")
        # Son bekleme turundaki hata (bulunamadı ≠ bağlanılamadı ayrımı için)
        self.last_error: Optional[BaseException] = None

    def _login(self, imap):
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
                    imap.login(self.username, code)
                    return
                try:
                    imap.login(self.username, self.password + code)
                    return
                except imaplib.IMAP4.error:
                    logger.warning("Şifre+TOTP girişi reddedildi, yalnızca şifre deneniyor.")
        imap.login(self.username, self.password)

    def _connect(self) -> imaplib.IMAP4:
        imap = imap_connect(self.config, timeout=30)
        try:
            self._login(imap)
            status, _ = imap.select("INBOX")
            if status != "OK":
                raise imaplib.IMAP4.error("INBOX seçilemedi")
        except Exception:
            try:
                imap.logout()
            except Exception:
                pass
            raise
        return imap

    # IMAP SEARCH SINCE tarihi yerel ayardan bağımsız İngilizce ay adı ister.
    _MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    # Message-ID araması başarısız olursa taranacak en fazla yeni mesaj
    FALLBACK_SCAN_LIMIT = 500

    @staticmethod
    def is_auth_error(exc: Optional[BaseException]) -> bool:
        if exc is None:
            return False
        text = str(exc).lower()
        return isinstance(exc, imaplib.IMAP4.error) and any(
            w in text for w in ("authenticationfailed", "authentication failed",
                                "invalid credentials", "login failed", "auth"))

    def _fetch_full(self, imap, mail_id: bytes) -> Optional[bytes]:
        # BODY.PEEK[]: RFC822'den farklı olarak mesajı \Seen işaretlemez.
        typ, msg_data = imap.fetch(mail_id, "(BODY.PEEK[])")
        if typ != "OK" or not msg_data:
            return None
        for item in msg_data:
            if isinstance(item, tuple) and len(item) >= 2:
                return item[1]
        return None

    def _candidates_by_header(self, imap, core_id: str) -> list:
        """Sunucu tarafında Message-ID araması (hızlı, posta kutusu boyutundan bağımsız)."""
        try:
            typ, data = imap.search(None, "HEADER", "Message-ID", f'"{core_id}"')
        except imaplib.IMAP4.error as e:
            logger.debug(f"HEADER araması desteklenmiyor: {e}")
            return []
        if typ != "OK" or not data or not data[0]:
            return []
        return list(reversed(data[0].split()))

    def _candidates_recent(self, imap, expected: str) -> list:
        """Yedek yol: son 2 günün mesajlarının yalnızca Message-ID başlığını tarar."""
        since = datetime.now() - timedelta(days=2)
        since_str = f"{since.day:02d}-{self._MONTHS[since.month - 1]}-{since.year}"
        typ, data = imap.search(None, "SINCE", since_str)
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[-self.FALLBACK_SCAN_LIMIT:]
        typ, fetched = imap.fetch(b",".join(ids), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if typ != "OK":
            return []
        parser = BytesHeaderParser()
        matches = []
        for item in fetched or []:
            if not (isinstance(item, tuple) and len(item) >= 2):
                continue
            seq = item[0].split()[0]
            header = parser.parsebytes(item[1] or b"")
            if self._unfold(header.get("Message-ID", "")) == expected:
                matches.append(seq)
        return list(reversed(matches))

    def wait_for_message(self, expected_msg_id: str, subject_prefix: str,
                         wait_seconds: int = 20, max_retries: int = 5,
                         retry_interval: int = 5) -> Optional[dict]:
        """
        Gelen kutusunu polling ile tarar. Beklenen Message-ID'li mesajı bulunca döndürür.
        Returns: Mesaj detayları içeren dict veya None (bulunamazsa).
        None döndüğünde ``self.last_error`` doluysa sebep bir bağlantı/kimlik
        hatasıdır (mesaj gelmemiş olmayabilir); boşsa mesaj gerçekten bulunamadı.

        ``subject_prefix`` geriye uyumluluk için tutulur; eşleştirme Message-ID ile
        yapılır (konu ön eki Türkçe karakter içerse de IMAP araması bozulmaz).
        """
        expected = self._unfold(expected_msg_id)
        core_id = expected.strip("<>")
        self.last_error = None
        logger.info(f"Mesaj bekleniyor | msg_id={expected} | max_wait={wait_seconds}s")
        time.sleep(wait_seconds)  # İlk bekleme — mesajın gelmesi için

        for attempt in range(1, max_retries + 1):
            imap = None
            try:
                imap = self._connect()
                candidates = self._candidates_by_header(imap, core_id)
                if not candidates:
                    candidates = self._candidates_recent(imap, expected)

                for mail_id in candidates:
                    raw = self._fetch_full(imap, mail_id)
                    if not raw:
                        continue
                    parsed = email.message_from_bytes(raw)
                    if self._unfold(parsed.get("Message-ID", "")) == expected:
                        logger.info(f"✅ Mesaj bulundu (deneme {attempt}/{max_retries})")
                        self.last_error = None
                        mid = mail_id.decode() if isinstance(mail_id, bytes) else str(mail_id)
                        return self._extract_details(parsed, raw, mid)

                self.last_error = None
                logger.debug(f"Mesaj bulunamadı (deneme {attempt}/{max_retries}), {retry_interval}s bekleniyor...")
            except Exception as e:  # noqa: BLE001 — her deneme bağımsız
                self.last_error = e
                logger.error(f"IMAP hatası (deneme {attempt}): {e}")
                if self.is_auth_error(e):
                    # Yanlış şifreyle tekrar tekrar denemek kurumsal hesabı kilitler.
                    logger.error("IMAP kimlik doğrulaması reddedildi — yeniden denenmeyecek.")
                    return None
            finally:
                if imap is not None:
                    try:
                        imap.logout()
                    except Exception:
                        pass
            if attempt < max_retries:
                time.sleep(retry_interval)

        if self.last_error is not None:
            logger.warning(f"❌ Mesaj kontrol edilemedi (IMAP hatası: {self.last_error}) | msg_id={expected}")
        else:
            logger.warning(f"❌ Mesaj {max_retries} denemede bulunamadı | msg_id={expected}")
        return None

    def _extract_details(self, msg: email.message.Message, raw: bytes, imap_id: str) -> dict:
        """Mesajdan tüm test için gerekli bilgileri çıkarır."""
        get = lambda name: self._unfold(msg.get(name, ""))  # noqa: E731
        result = {
            "imap_id": imap_id,
            "raw_size": len(raw),
            "raw_bytes": raw,  # Claude API analizi için ham içerik
            "headers": {
                "message_id": get("Message-ID"),
                "from": get("From"),
                "to": get("To"),
                "subject": self._unfold(self._decode_header(msg.get("Subject", ""))),
                "date": get("Date"),
                "content_type": get("Content-Type"),
                "in_reply_to": get("In-Reply-To"),
                "references": get("References"),
                "mime_version": get("MIME-Version"),
                "x_mailer": get("X-Mailer"),
                "content_transfer_encoding": get("Content-Transfer-Encoding"),
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
            # Ham 8-bit değerlerde .get() Header nesnesi döndürebilir — metne çevir.
            disposition = self._unfold(part.get("Content-Disposition", ""))
            content_id = self._unfold(part.get("Content-ID", ""))

            part_info = {
                "content_type": content_type,
                "charset": part.get_content_charset(),
                "content_id": content_id,
                "disposition": disposition,
                "transfer_encoding": self._unfold(part.get("Content-Transfer-Encoding", "")),
            }

            if content_type == "message/rfc822":
                # İletilen mesaj (forward): iç başlıklar analize taşınır; eskiden
                # boyutu 0 ve başlıksız görünüyordu.
                inner = part.get_payload()
                inner_msg = inner[0] if isinstance(inner, list) and inner else None
                inner_headers = {}
                if inner_msg is not None:
                    inner_headers = {k: self._unfold(self._decode_header(inner_msg.get(k, "")))
                                     for k in ("From", "To", "Subject", "Date", "Message-ID")}
                result["attachments"].append({
                    **part_info,
                    "filename": self._decode_header(part.get_filename() or ""),
                    "size": len(inner_msg.as_bytes()) if inner_msg is not None else 0,
                    "encapsulated_headers": inner_headers,
                })
                continue

            if "attachment" in disposition:
                filename = part.get_filename() or ""
                payload = part.get_payload(decode=True)
                result["attachments"].append({
                    **part_info,
                    "filename": self._decode_header(filename),
                    "size": len(payload) if payload else 0,
                    # İlk baytlar (dosya imzası, ör. %PDF) — JSON'a uygun hex metin
                    "payload_head_hex": payload[:16].hex() if payload else "",
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
    def _unfold(value: str) -> str:
        """RFC 5322 §2.2.3 — katlanmış header değerini tek satıra indirger.

        78 oktetten uzun header'lar CRLF + boşluk ile katlanır; uzun bir
        Message-ID (uzun host adı) ya da uzun bir thread'in References'ı bunu
        rutin olarak tetikler. Ham değer saklanırsa hem eşleştirme bozulur hem
        de analiz promptuna satır başlarıyla gider ve LLM bunu 'header bozulmuş'
        diye raporlayabilir.
        """
        if not value:
            return ""
        if not isinstance(value, str):
            # Ham 8-bit header'larda compat32 str yerine email.header.Header
            # döndürür; .split() AttributeError verip mesajı kaybettiriyordu.
            value = MailReceiver._decode_header(value)
        return " ".join(value.split())

    @staticmethod
    def _decode_header(value: str) -> str:
        """RFC 2047 header'ını metne çevirir; hiçbir girdide exception atmaz.

        Gerçek sunucularda görülen ama geçerli/yarı geçerli değerler eskiden
        mesaj ayrıştırmayı çökertip teslim edilmiş mesajı "IMAP kontrol
        edilemedi" diye FAIL'e çeviriyordu: ham 8-bit UTF-8 (charset
        'unknown-8bit'), bilinmeyen charset (=?foo?q?x?=), Header nesnesi.
        """
        if not value:
            return ""
        try:
            decoded_parts = decode_header(value)
        except Exception:  # noqa: BLE001 — bozuk encoded-word: ham metni koru
            return str(value)
        parts = []
        for text, charset in decoded_parts:
            if isinstance(text, bytes):
                try:
                    parts.append(text.decode(charset or "utf-8", errors="replace"))
                except LookupError:  # bilinmeyen / unknown-8bit charset
                    parts.append(text.decode("utf-8", errors="replace"))
            else:
                parts.append(text)
        return "".join(parts)
