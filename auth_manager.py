"""
auth_manager.py — 2FA / MFA Akış Yöneticisi
=============================================
Test çalışırken sunucu 2FA gerektirdiğinde:
  1. Backend → mfa_challenge() çağırır → frontend'e sinyal gönderilir
  2. Frontend → kullanıcıya modal gösterir
  3. Kullanıcı kodu girer → submit_code() çağrılır
  4. Backend bekleyen thread devam eder

TOTP desteği: Eğer secret kayıtlıysa otomatik üretir, sorulmaz.
"""

import threading
import time
import logging
import hmac
import hashlib
import struct
import base64
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
#  TOTP (RFC 6238) — pyotp olmadan minimal implementasyon
# ──────────────────────────────────────────────────────────────
def generate_totp(secret_b32: str, digits: int = 6, interval: int = 30) -> str:
    """Base32 secret'tan geçerli TOTP kodu üretir."""
    try:
        # Base32 decode — padding düzelt
        secret = secret_b32.upper().replace(" ", "")
        padding = (8 - len(secret) % 8) % 8
        secret += "=" * padding
        key = base64.b32decode(secret)

        # Zaman sayacı
        counter = int(time.time()) // interval
        msg = struct.pack(">Q", counter)

        # HMAC-SHA1
        mac = hmac.new(key, msg, hashlib.sha1).digest()
        offset = mac[-1] & 0x0F
        code = struct.unpack(">I", mac[offset:offset+4])[0] & 0x7FFFFFFF
        return str(code % (10 ** digits)).zfill(digits)
    except Exception as e:
        logger.error(f"TOTP üretme hatası: {e}")
        return ""


def totp_remaining_seconds(interval: int = 30) -> int:
    """Mevcut TOTP kodunun kaç saniye geçerli kalacağı."""
    return interval - (int(time.time()) % interval)


# ──────────────────────────────────────────────────────────────
#  MFA Challenge State
# ──────────────────────────────────────────────────────────────
@dataclass
class MFAChallenge:
    server_key: str           # 'ems', 'gmail', 'outlook'
    server_label: str         # Kullanıcıya gösterilen isim
    method: str               # 'totp', 'sms', 'email_otp', 'push'
    prompt: str               # Kullanıcıya gösterilecek mesaj
    code: Optional[str] = None
    resolved: bool = False
    cancelled: bool = False


class MFAManager:
    """
    Test thread'i ile Flask thread'i arasında 2FA köprüsü.
    Thread-safe: Event + Lock kullanır.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._challenge: Optional[MFAChallenge] = None

    # ── Test thread'i çağırır ─────────────────────────────────
    def mfa_challenge(self, server_key: str, server_label: str,
                      method: str = "totp", totp_secret: str = "") -> Optional[str]:
        """
        2FA kodu ister.
        - TOTP secret varsa → otomatik üretir, kullanıcıya sormaz.
        - Yoksa → frontend'i bekletir, kullanıcı girişini bekler.
        Döndürür: kod stringi veya None (iptal/timeout)
        """
        # Otomatik TOTP
        if method == "totp" and totp_secret:
            code = generate_totp(totp_secret)
            if code:
                remaining = totp_remaining_seconds()
                logger.info(f"[MFA] TOTP otomatik üretildi: {server_key} | kod=***{code[-2:]} | {remaining}s geçerli")
                return code
            logger.warning(f"[MFA] TOTP üretilemedi, manuel isteniyor: {server_key}")

        # Manuel giriş — frontend'i beklet
        prompts = {
            "totp": f"{server_label} için Authenticator uygulamasındaki 6 haneli kodu girin.",
            "sms": f"{server_label} için SMS ile gelen doğrulama kodunu girin.",
            "email_otp": f"{server_label} için e-posta ile gelen doğrulama kodunu girin.",
            "push": f"{server_label} için push bildirimine onay verin, ardından kodu girin.",
        }

        with self._lock:
            self._challenge = MFAChallenge(
                server_key=server_key,
                server_label=server_label,
                method=method,
                prompt=prompts.get(method, f"{server_label} için 2FA kodunu girin."),
            )
            self._event.clear()

        logger.info(f"[MFA] Kullanıcı girişi bekleniyor: {server_key} / {method}")

        # 5 dakika timeout
        got_code = self._event.wait(timeout=300)

        with self._lock:
            challenge = self._challenge
            self._challenge = None

        if not got_code or challenge is None or challenge.cancelled:
            logger.warning(f"[MFA] İptal edildi veya timeout: {server_key}")
            return None

        logger.info(f"[MFA] Kod alındı: {server_key}")
        return challenge.code

    # ── Flask thread'i çağırır ────────────────────────────────
    def submit_code(self, code: str) -> bool:
        """Kullanıcının girdiği kodu iletir, bekleyen thread'i serbest bırakır."""
        with self._lock:
            if self._challenge is None:
                return False
            self._challenge.code = code
            self._challenge.resolved = True
        self._event.set()
        return True

    def cancel(self) -> bool:
        """Bekleyen 2FA challenge'ı iptal eder."""
        with self._lock:
            if self._challenge is None:
                return False
            self._challenge.cancelled = True
        self._event.set()
        return True

    def get_pending(self) -> Optional[dict]:
        """Frontend için bekleyen challenge bilgisini döndürür."""
        with self._lock:
            if self._challenge is None or self._challenge.resolved:
                return None
            return {
                "server_key": self._challenge.server_key,
                "server_label": self._challenge.server_label,
                "method": self._challenge.method,
                "prompt": self._challenge.prompt,
            }


# Global singleton
mfa_manager = MFAManager()
