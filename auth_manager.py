"""
auth_manager.py — 2FA / MFA Akış Yöneticisi
=============================================
Test çalışırken sunucu 2FA gerektirdiğinde:
  1. Backend → mfa_challenge() çağırır → frontend'e sinyal gönderilir
  2. Frontend → kullanıcıya modal gösterir
  3. Kullanıcı kodu girer → submit_code() çağrılır
  4. Backend bekleyen thread devam eder

TOTP desteği: Eğer secret kayıtlıysa otomatik üretir, sorulmaz.

Süreçler arası köprü: Web arayüzü testi ayrı bir subprocess'te (main.py)
çalıştırdığı için Event tabanlı akış tek başına yetmez. bridge_dir verilirse
challenge/response JSON dosyaları üzerinden Flask süreci ile CLI süreci
haberleşir (challenge.json → modal, response.json → kod).
"""

import json
import threading
import time
import logging
import hmac
import hashlib
import struct
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Flask süreci ile main.py subprocess'inin ortak kullandığı köprü dizini.
DEFAULT_BRIDGE_DIR = Path(__file__).parent / "logs" / "mfa_bridge"

# Aynı sunucu için kısa süre içinde tekrar kod istenirse (ör. IMAP polling
# yeniden bağlanmaları) kullanıcıyı tekrar rahatsız etmemek için önbellek süresi.
CODE_CACHE_SECONDS = 20


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

    bridge_dir verilirse süreçler arası da çalışır: challenge isteği
    challenge.json'a yazılır, yanıt response.json'dan okunur. Böylece
    subprocess'te koşan main.py, Flask arayüzündeki modal'dan kod alabilir.
    """
    CHALLENGE_TIMEOUT = 300      # saniye
    STALE_AFTER = 330            # bundan eski challenge dosyaları yok sayılır

    def __init__(self, bridge_dir: Optional[Path] = None):
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._challenge: Optional[MFAChallenge] = None
        self.bridge_dir = Path(bridge_dir) if bridge_dir else None
        self._last_code: Optional[str] = None
        self._last_code_at = 0.0
        self._last_server: Optional[str] = None

    # ── Köprü dosyası yardımcıları ────────────────────────────
    def _challenge_file(self) -> Optional[Path]:
        return self.bridge_dir / "challenge.json" if self.bridge_dir else None

    def _response_file(self) -> Optional[Path]:
        return self.bridge_dir / "response.json" if self.bridge_dir else None

    @staticmethod
    def _write_json(path: Path, data: dict):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def clear_bridge(self):
        """Önceki çalışmadan kalan köprü dosyalarını temizler."""
        if not self.bridge_dir:
            return
        for p in (self._challenge_file(), self._response_file()):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ── Test thread'i / CLI subprocess'i çağırır ──────────────
    def mfa_challenge(self, server_key: str, server_label: str,
                      method: str = "totp", totp_secret: str = "") -> Optional[str]:
        """
        2FA kodu ister.
        - TOTP secret varsa → otomatik üretir, kullanıcıya sormaz.
        - Yoksa → frontend'i bekletir (Event + köprü dosyası), kullanıcı girişini bekler.
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

        # Aynı sunucu için az önce alınan kodu tekrar kullan (yeniden bağlanmalar)
        if (self._last_code and self._last_server == server_key
                and time.time() - self._last_code_at < CODE_CACHE_SECONDS):
            logger.info(f"[MFA] Önbellekteki kod yeniden kullanılıyor: {server_key}")
            return self._last_code

        # Manuel giriş — frontend'i beklet
        prompts = {
            "totp": f"{server_label} için Authenticator uygulamasındaki 6 haneli kodu girin.",
            "sms": f"{server_label} için SMS ile gelen doğrulama kodunu girin.",
            "email_otp": f"{server_label} için e-posta ile gelen doğrulama kodunu girin.",
            "push": f"{server_label} için push bildirimine onay verin, ardından kodu girin.",
        }
        prompt = prompts.get(method, f"{server_label} için 2FA kodunu girin.")

        with self._lock:
            self._challenge = MFAChallenge(
                server_key=server_key,
                server_label=server_label,
                method=method,
                prompt=prompt,
            )
            self._event.clear()

        challenge_file = self._challenge_file()
        if challenge_file:
            self.bridge_dir.mkdir(parents=True, exist_ok=True)
            response_file = self._response_file()
            response_file.unlink(missing_ok=True)
            self._write_json(challenge_file, {
                "server_key": server_key,
                "server_label": server_label,
                "method": method,
                "prompt": prompt,
                "created_at": time.time(),
            })

        logger.info(f"[MFA] Kullanıcı girişi bekleniyor: {server_key} / {method}")

        code: Optional[str] = None
        cancelled = False
        deadline = time.time() + self.CHALLENGE_TIMEOUT
        try:
            while time.time() < deadline:
                # Aynı süreçte submit_code/cancel çağrıldıysa
                if self._event.wait(timeout=0.25):
                    with self._lock:
                        if self._challenge is not None:
                            code = self._challenge.code
                            cancelled = self._challenge.cancelled
                    break
                # Köprü üzerinden (Flask sürecinden) yanıt geldiyse
                if challenge_file:
                    resp = self._read_json(self._response_file())
                    if resp is not None:
                        code = resp.get("code")
                        cancelled = bool(resp.get("cancelled"))
                        break
        finally:
            with self._lock:
                self._challenge = None
            if challenge_file:
                self.clear_bridge()

        if cancelled or not code:
            logger.warning(f"[MFA] İptal edildi veya timeout: {server_key}")
            return None

        self._last_code = code
        self._last_code_at = time.time()
        self._last_server = server_key
        logger.info(f"[MFA] Kod alındı: {server_key}")
        return code

    # ── Flask thread'i çağırır ────────────────────────────────
    def _bridge_pending(self) -> Optional[dict]:
        """Köprü dosyasındaki (başka süreçten gelen) challenge'ı okur."""
        challenge_file = self._challenge_file()
        if not challenge_file or not challenge_file.exists():
            return None
        data = self._read_json(challenge_file)
        if not data:
            return None
        if time.time() - data.get("created_at", 0) > self.STALE_AFTER:
            self.clear_bridge()  # sahipsiz kalmış eski challenge
            return None
        return data

    def submit_code(self, code: str) -> bool:
        """Kullanıcının girdiği kodu iletir, bekleyen thread'i/süreci serbest bırakır."""
        with self._lock:
            if self._challenge is not None:
                self._challenge.code = code
                self._challenge.resolved = True
                self._event.set()
                return True
        if self._bridge_pending() is not None:
            self._write_json(self._response_file(), {"code": code})
            return True
        return False

    def cancel(self) -> bool:
        """Bekleyen 2FA challenge'ı iptal eder."""
        with self._lock:
            if self._challenge is not None:
                self._challenge.cancelled = True
                self._event.set()
                return True
        if self._bridge_pending() is not None:
            self._write_json(self._response_file(), {"cancelled": True})
            return True
        return False

    def get_pending(self) -> Optional[dict]:
        """Frontend için bekleyen challenge bilgisini döndürür."""
        with self._lock:
            if self._challenge is not None and not self._challenge.resolved:
                return {
                    "server_key": self._challenge.server_key,
                    "server_label": self._challenge.server_label,
                    "method": self._challenge.method,
                    "prompt": self._challenge.prompt,
                }
        bridged = self._bridge_pending()
        if bridged:
            return {
                "server_key": bridged.get("server_key", ""),
                "server_label": bridged.get("server_label", ""),
                "method": bridged.get("method", "totp"),
                "prompt": bridged.get("prompt", ""),
            }
        return None


# Global singleton — hem Flask hem CLI aynı köprü dizinini kullanır
mfa_manager = MFAManager(bridge_dir=DEFAULT_BRIDGE_DIR)
