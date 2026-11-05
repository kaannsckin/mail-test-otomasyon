"""
mail_transport.py — SMTP/IMAP bağlantısı kurma ve TLS ayarları (tek kaynak).

sender.py, receiver.py ve web arayüzünün bağlantı testi aynı kodu kullanır;
böylece "bağlantı testi geçti ama koşu düştü" tutarsızlığı oluşmaz.

Sunucu bölümü başına desteklenen anahtarlar (config.yaml):

  smtp_use_tls: true        # STARTTLS (587). 465 için gerekmez.
  smtp_use_ssl: true        # Doğrudan SSL/TLS (465). Port 465 ise belirtilmese
                            # de otomatik açılır; kapatmak için false yazın.
  imap_use_ssl: true        # IMAPS (993)
  imap_use_starttls: false  # Düz IMAP (143) üzerinde STARTTLS
  tls_verify: true          # Sertifika doğrulaması. Kurum içi self-signed
                            # sunucuda geçici çözüm olarak false (güvensiz).
  tls_ca_file: ""           # Kurum iç CA sertifikası (PEM). Doğrulamayı
                            # kapatmak yerine önerilen yöntem.
"""
from __future__ import annotations

import imaplib
import logging
import os
import smtplib
import socket
import ssl
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


def _as_bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "evet")
    return bool(value)


def resolve_path(raw: str) -> Optional[Path]:
    """Göreli yolu sırayla çalışma dizini, DATA_DIR ve repo köküne göre çözer."""
    if not raw:
        return None
    p = Path(os.path.expanduser(str(raw)))
    if p.is_absolute():
        return p
    roots = [Path.cwd()]
    if os.environ.get("DATA_DIR"):
        roots.append(Path(os.environ["DATA_DIR"]))
    roots.append(Path(__file__).parent)
    for root in roots:
        candidate = root / p
        if candidate.exists():
            return candidate
    return roots[0] / p


def ssl_context_for(server_config: dict) -> ssl.SSLContext:
    """Sunucu ayarlarına göre TLS bağlamı üretir."""
    ca_raw = (server_config.get("tls_ca_file") or "").strip()
    cafile = None
    if ca_raw:
        path = resolve_path(ca_raw)
        if not path or not path.is_file():
            raise FileNotFoundError(
                f"tls_ca_file bulunamadı: {ca_raw} — kurum CA sertifikasını (PEM) "
                "veri dizinine koyup yolunu doğru yazın.")
        cafile = str(path)
    ctx = ssl.create_default_context(cafile=cafile)
    if not _as_bool(server_config.get("tls_verify"), True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def smtp_mode(server_config: dict) -> str:
    """'ssl' (465, doğrudan TLS) | 'starttls' | 'plain'"""
    port = int(server_config.get("smtp_port") or 0)
    if _as_bool(server_config.get("smtp_use_ssl"), port == 465):
        return "ssl"
    if _as_bool(server_config.get("smtp_use_tls"), True):
        return "starttls"
    return "plain"


def connection_fingerprint(server_config: dict) -> tuple:
    """Kayıtlı bir şifrenin gönderileceği hedefi tanımlayan etkin bağlantı
    parametreleri: host, port, TLS modu, doğrulama ve CA dahil. Formda bunlardan
    biri değişirse kayıtlı şifre kullanılmaz (aynı host'ta şifresiz bir porta
    yönlendirip şifreyi düz metin yakalama dahil)."""
    sc = server_config or {}

    def norm(v):
        return str(v if v is not None else "").strip().lower()

    def port(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return norm(v)

    imap_ssl = _as_bool(sc.get("imap_use_ssl"), True)
    return (
        norm(sc.get("username")),
        norm(sc.get("smtp_host")), port(sc.get("smtp_port")),
        smtp_mode(sc) if sc.get("smtp_port") not in (None, "") else "",
        norm(sc.get("imap_host")), port(sc.get("imap_port")),
        imap_ssl, (not imap_ssl) and _as_bool(sc.get("imap_use_starttls"), False),
        _as_bool(sc.get("tls_verify"), True),
        norm(sc.get("tls_ca_file")),
    )


def smtp_connect(server_config: dict, timeout: float = DEFAULT_TIMEOUT) -> smtplib.SMTP:
    """Bağlanır, gerekiyorsa TLS'e geçer; giriş YAPMAZ."""
    host = server_config["smtp_host"]
    port = int(server_config["smtp_port"])
    mode = smtp_mode(server_config)
    if mode == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=timeout,
                                context=ssl_context_for(server_config))
        smtp.ehlo()
        return smtp
    smtp = smtplib.SMTP(host, port, timeout=timeout)
    try:
        smtp.ehlo()
        if mode == "starttls":
            if not smtp.has_extn("starttls"):
                raise smtplib.SMTPNotSupportedError(
                    f"{host}:{port} STARTTLS desteklemiyor. Port 465 ise smtp_use_ssl: true, "
                    "şifresiz bağlantı bilinçli tercihse smtp_use_tls: false kullanın.")
            smtp.starttls(context=ssl_context_for(server_config))
            smtp.ehlo()
    except Exception:
        try:
            smtp.close()
        except Exception:
            pass
        raise
    return smtp


def imap_connect(server_config: dict, timeout: float = DEFAULT_TIMEOUT) -> imaplib.IMAP4:
    """Bağlanır, gerekiyorsa TLS'e geçer; giriş YAPMAZ."""
    host = server_config["imap_host"]
    port = int(server_config["imap_port"])
    if _as_bool(server_config.get("imap_use_ssl"), True):
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl_context_for(server_config),
                                 timeout=timeout)
    imap = imaplib.IMAP4(host, port, timeout=timeout)
    if _as_bool(server_config.get("imap_use_starttls"), False):
        imap.starttls(ssl_context=ssl_context_for(server_config))
    return imap


def explain_connection_error(exc: BaseException, server_config: Optional[dict] = None) -> Optional[str]:
    """Sık görülen bağlantı hatalarına kullanıcıya yönelik açıklama üretir."""
    sc = server_config or {}
    text = str(exc).lower()
    chain, err = [], exc
    while err is not None and len(chain) < 5:
        chain.append(err)
        err = err.__cause__ or err.__context__

    for e in chain:
        if isinstance(e, ssl.SSLCertVerificationError) or "certificate verify failed" in str(e).lower():
            return ("Sunucunun TLS sertifikası doğrulanamadı (kendinden imzalı ya da kurum içi "
                    "CA). Önerilen: kurum CA sertifikasını tls_ca_file ile verin. Yalnızca test "
                    "ortamında: tls_verify: false.")
        if isinstance(e, ssl.SSLError) and ("wrong version number" in str(e).lower()
                                            or "unknown protocol" in str(e).lower()
                                            or "record layer failure" in str(e).lower()):
            return ("TLS modu ile port uyuşmuyor: 465/993 doğrudan SSL ister (smtp_use_ssl / "
                    "imap_use_ssl: true); 587/143 önce düz bağlanıp STARTTLS kullanır "
                    "(smtp_use_tls: true, smtp_use_ssl: false).")
        if isinstance(e, socket.gaierror):
            return ("Sunucu adı çözümlenemedi (DNS). Host adını kontrol edin; kurum içi bir "
                    "sunucuysa uygulamanın kurum ağına/VPN'e erişimi olmalı.")
        auth_failed = isinstance(e, smtplib.SMTPAuthenticationError) or (
            isinstance(e, imaplib.IMAP4.error)
            and any(w in str(e).lower() for w in ("auth", "login", "credential")))
        if auth_failed:
            hint = "Kullanıcı adı/şifre reddedildi."
            if "gmail" in str(sc.get("smtp_host", "")) + str(sc.get("imap_host", "")):
                hint += " Gmail için 2FA açık hesapta Uygulama Şifresi (App Password) gerekir."
            if sc.get("auth_method") in ("totp_password", "otp_only"):
                hint += " 2FA kodunun süresi dolmuş ya da yanlış olabilir."
            return hint
        if isinstance(e, smtplib.SMTPServerDisconnected) and int(sc.get("smtp_port") or 0) == 465:
            return "Port 465 doğrudan SSL ister — smtp_use_ssl: true olmalı."
        if isinstance(e, (socket.timeout, TimeoutError)):
            return ("Bağlantı zaman aşımına uğradı. Port bir güvenlik duvarınca sessizce "
                    "engelleniyor olabilir (kurum ağı ya da giden SMTP'yi kapatan bulut "
                    "sağlayıcısı) veya port/TLS modu yanlış.")
        if isinstance(e, ConnectionRefusedError):
            return "Bağlantı reddedildi — port kapalı ya da sunucu bu portu dinlemiyor."
    if "network is unreachable" in text or "no route to host" in text:
        return ("Sunucuya ağ yolu yok (Network is unreachable). Platform giden mail "
                "portlarını engelliyor olabilir — /api/diagnostics/network ile kontrol edin.")
    return None
