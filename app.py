"""
app.py — Mail Otomasyon Web Arayüzü (Flask)
Çalıştır: python app.py  →  http://localhost:5000 (boşsa) / otomatik alternatif port
"""

import csv, errno, hmac, ipaddress, json, logging, os, signal, socket, subprocess, sys, threading, time, uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from flask import Flask, Response, jsonify, render_template, request, send_file, send_from_directory

# Türkçe Windows konsolu (cp1254) emoji içeren print'lerde çökmesin.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = Path(__file__).parent

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # tarayıcı bayat JS/CSS önbelleklemesin
# Config içe aktarma ve JSON gövdeleri için üst sınır (aşılırsa 413).
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


def _writable_dir(path: Path) -> bool:
    """Dizini oluşturmayı ve içine gerçekten yazmayı dener."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _data_dir() -> Path:
    """Yazılabilir veri dizini (config.yaml, reports/, logs/).

    Öncelik: DATA_DIR ortam değişkeni → yerelde/Railway'de repo kökü
    (README akışıyla uyumlu) → /tmp. Hiçbir aday uygulamayı ÇÖKERTMEZ:
    yazılamayan aday (ör. Render ücretsiz planda disk yokken DATA_DIR=/data)
    uyarı loglanıp bir sonrakine geçilir.
    """
    candidates = []
    override = os.environ.get("DATA_DIR")
    if override:
        candidates.append(Path(override))
    if not os.environ.get("VERCEL"):
        candidates.append(BASE_DIR)
    candidates.append(Path(os.environ.get("TMPDIR", "/tmp")) / "mail_otomasyon")

    for candidate in candidates:
        if _writable_dir(candidate):
            return candidate
        print(f"⚠️  Veri dizini yazılamıyor, sonraki adaya geçiliyor: {candidate}")
    # /tmp bile yazılamıyorsa mkdir'lar aşağıda hatayı açıkça verir
    return candidates[-1]


DATA_DIR      = _data_dir()
CONFIG_PATH   = DATA_DIR / os.environ.get("CONFIG_FILENAME", "config.yaml")
REPORTS_DIR   = DATA_DIR / "reports"
LOGS_DIR      = DATA_DIR / "logs"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
print(f"📁 Veri dizini: {DATA_DIR}")

# main.py alt süreci ve MFA köprüsü aynı veri dizinini kullansın
# (çözümlenmiş değer: DATA_DIR yazılamadığı için /tmp'ye düşülmüş olabilir).
os.environ["DATA_DIR"] = str(DATA_DIR)
from auth_manager import mfa_manager, generate_totp, totp_remaining_seconds  # noqa: E402

run_state = {
    "running": False,
    "process": None,
    "run_id": None,
    "stopped": False,
    "pump_done": True,      # log okuyucu bu koşunun son satırını dosyaya yazdı mı
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
}
# start/stop/bitiş geçişleri tek kilit altında — çift tıklama veya
# stop→start yarışında iki koşu aynı anda başlayamasın.
_run_lock = threading.Lock()
# config.yaml oku-birleştir-yaz işlemleri sıraya girer: kilitsizken eşzamanlı
# kayıtlar (iki sekme, çift tık, her koşudan önceki otomatik kayıt) birbirinin
# bölümlerini — secret'lar dahil — siliyor ve ortak .tmp dosyası yüzünden 500
# veriyordu. Uygulama tek süreçte çalışır (python app.py), thread kilidi yeterli.
_config_lock = threading.RLock()
RUN_PID_FILE = LOGS_DIR / "run.pid"

SUPPORTED_SCENARIO_KEYS = None  # csv_parser'dan tembel yüklenir


def _supported_scenarios() -> set:
    global SUPPORTED_SCENARIO_KEYS
    if SUPPORTED_SCENARIO_KEYS is None:
        from csv_parser import SUPPORTED_SCENARIOS
        SUPPORTED_SCENARIO_KEYS = set(SUPPORTED_SCENARIOS)
    return SUPPORTED_SCENARIO_KEYS

# ── PLATFORM ────────────────────────────────────────────────────
def _is_serverless() -> bool:
    """Vercel gibi serverless ortamlar: arka plan süreci yaşatamaz."""
    return bool(os.environ.get("VERCEL"))


# Railway/Render/Heroku/Fly/Cloud Run gibi PaaS'larda dış trafik için
# 0.0.0.0'a bind gerekir; bu platformlar kendilerini env ile belli eder.
_PAAS_MARKERS = ("RAILWAY_ENVIRONMENT", "RENDER", "DYNO", "FLY_APP_NAME", "K_SERVICE")


def _default_host() -> str:
    if any(os.environ.get(m) for m in _PAAS_MARKERS):
        return "0.0.0.0"
    return "127.0.0.1"


# ── AUTH & İSTEK GÜVENLİĞİ ─────────────────────────────────────
# UI_PASSWORD ayarlanırsa tüm istekler HTTP Basic Auth gerektirir.
# Ayarlanmazsa arayüz YALNIZCA bu makineden (loopback) kullanılabilir;
# ağdan/PaaS proxy'sinden gelen istekler reddedilir. Bilinçli olarak
# parolasız ağ erişimi isteyenler ALLOW_NO_AUTH=1 ayarlayabilir.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _is_loopback_addr(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address((addr or "").split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)   # HOST=:: iken ::ffff:127.0.0.1
    return ip.is_loopback or bool(mapped and mapped.is_loopback)


# Ters proxy (nginx proxy_pass, ngrok --host-header=rewrite) arkasında istek
# loopback'ten ve 'Host: localhost' ile gelir ama aslında uzaktaki bir
# istemciye aittir. Parolasız modda bu başlıkları taşıyan istek reddedilir.
_PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP", "Forwarded")


def _hostname_of(netloc: str) -> str:
    """'host:port' / '[::1]:5000' → 'host' / '::1' (küçük harf)."""
    try:
        return (urlsplit("//" + (netloc or "")).hostname or "").lower()
    except ValueError:
        return ""


def _secure_equals(given: str, expected: str) -> bool:
    # compare_digest str ile yalnızca ASCII kabul eder; Türkçe karakterli
    # parolada TypeError → 500 olmasın diye bayt karşılaştırılır.
    return hmac.compare_digest((given or "").encode("utf-8"), (expected or "").encode("utf-8"))


def _cross_site_request() -> bool:
    """Durum değiştiren istek başka bir siteden mi geliyor? (CSRF)"""
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return True
    origin = request.headers.get("Origin")
    if origin is None:
        return False  # tarayıcı dışı istemci (curl, testler) — Origin göndermez
    if origin == "null":
        return True   # sandbox iframe / file:// sayfası
    try:
        origin_host = urlsplit(origin).netloc.lower()
    except ValueError:
        return True
    # Host'u yeniden yazan ters proxy arkasında tarayıcının gördüğü adres
    # X-Forwarded-Host'tadır. Tarayıcı bu başlığı başka siteden preflight
    # olmadan ekleyemez; bu yüzden karşılaştırmada güvenle kullanılabilir.
    allowed = {(request.host or "").lower()}
    fwd = request.headers.get("X-Forwarded-Host", "")
    allowed.update(h.strip().lower() for h in fwd.split(",") if h.strip())
    return origin_host not in allowed


# ── Basic Auth deneme sınırı ────────────────────────────────────
# Eskiden parola sınırsız denenebiliyordu (200 yanlış deneme, hiç yavaşlama yok).
AUTH_MAX_FAILURES = 10            # pencere içinde izin verilen yanlış deneme
AUTH_WINDOW_SECONDS = 300
_auth_failures: dict = {}         # istemci → [zaman damgaları]
_auth_lock = threading.Lock()


def _client_key() -> str:
    """Deneme sayacının anahtarı. PaaS'ta istemci adresi, proxy'nin EKLEDİĞİ
    (en sağdaki) X-Forwarded-For girdisidir — soldakiler istemcinin yazabildiği
    değerlerdir ve sayacı atlatmak için kullanılabilirdi."""
    if any(os.environ.get(m) for m in _PAAS_MARKERS):
        fwd = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
        if fwd:
            return fwd[-1]
    return request.remote_addr or "?"


def _auth_blocked(key: str, now: float) -> float:
    """Kilitliyse kalan saniye, değilse 0."""
    with _auth_lock:
        recent = [t for t in _auth_failures.get(key, []) if now - t < AUTH_WINDOW_SECONDS]
        if recent:
            _auth_failures[key] = recent
        else:
            _auth_failures.pop(key, None)
        if len(recent) >= AUTH_MAX_FAILURES:
            return AUTH_WINDOW_SECONDS - (now - recent[0])
        return 0.0


def _note_auth_failure(key: str, now: float) -> None:
    with _auth_lock:
        if len(_auth_failures) > 10000:            # bellek sınırı: en eskileri at
            _auth_failures.clear()
        _auth_failures.setdefault(key, []).append(now)


@app.after_request
def _security_headers(resp):
    # Clickjacking: arayüz başka bir sitede çerçevelenemez (CSRF kapısı Origin'e
    # dayandığı için çerçeveleme onu atlatabiliyordu). Rapor önizlemesi aynı
    # kaynaktan iframe ile açıldığından 'self' izinli.
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.before_request
def _security_gate():
    if request.path == "/api/health":
        return None  # platform healthcheck'leri kimlik bilgisi gönderemez

    password = os.environ.get("UI_PASSWORD", "")
    if password:
        username = os.environ.get("UI_USERNAME", "admin")
        key, now = _client_key(), time.time()
        wait = _auth_blocked(key, now)
        if wait:
            return Response(
                "Çok fazla hatalı giriş denemesi — biraz sonra tekrar deneyin.", 429,
                {"Retry-After": str(int(wait) + 1), "Content-Type": "text/plain; charset=utf-8"})
        auth = request.authorization
        if not (auth and auth.type == "basic"
                and _secure_equals(auth.username, username)
                and _secure_equals(auth.password, password)):
            if auth is not None:   # tarayıcının ilk (kimliksiz) isteği deneme sayılmaz
                _note_auth_failure(key, now)
            return Response(
                "Kimlik doğrulama gerekli", 401,
                {"WWW-Authenticate": 'Basic realm="Mail Otomasyon", charset="UTF-8"'},
            )
    elif not _env_flag("ALLOW_NO_AUTH"):
        # Parolasız mod yalnızca yerel kullanım içindir. Host başlığı da
        # kontrol edilir: aksi halde DNS rebinding ile kötü niyetli bir site
        # localhost'taki arayüzden config'i (şifreler dahil) okuyabilir.
        if (not _is_loopback_addr(request.remote_addr)
                or _hostname_of(request.host) not in _LOOPBACK_HOSTNAMES
                or any(h in request.headers for h in _PROXY_HEADERS)):
            return Response(
                "Arayüz parolasız çalışıyor ve yalnızca bu makineden (localhost) "
                "erişilebilir. Ağdan erişim için UI_PASSWORD ortam değişkenini "
                "ayarlayın.", 403, {"Content-Type": "text/plain; charset=utf-8"},
            )

    if request.method not in _SAFE_METHODS and _cross_site_request():
        return jsonify({"ok": False, "error": "Başka bir siteden gelen istek reddedildi"}), 403
    return None


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"ok": False, "error": "İstek çok büyük (en fazla 2 MB)"}), 413


# ── CONFIG ──────────────────────────────────────────────────────
SECRET_MASK = "••••••••"
TEST_CONNECTION_DEADLINE = 45.0
LOG_POLL_MAX_LINES = 2000         # /api/run/logs yanıtı başına en fazla satır   # sn — bağlantı testinin toplam süre sınırı
SERVER_KEYS = ("ems", "gmail", "outlook")


class ConfigError(ValueError):
    """config.yaml okunamıyor ya da beklenen yapıda değil."""


class _NoAliasLoader(yaml.SafeLoader):
    """YAML anchor/alias (&a / *a) reddedilir.

    Alias'lar iç içe çoğaltılarak birkaç yüz baytlık bir dosya gigabaytlarca
    belleğe açılabilir ("billion laughs"). Config dosyasında alias'a ihtiyaç yok.
    """

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(
                None, None, "YAML alias (*) desteklenmiyor", event.start_mark)
        return super().compose_node(parent, index)


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def _yaml_load(text: str):
    return yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 — SafeLoader türevi


def _yaml_dump(data: dict, fh) -> None:
    yaml.dump(data, fh, Dumper=_NoAliasDumper, allow_unicode=True,
              default_flow_style=False, sort_keys=False)


def _normalize_config(data) -> dict:
    """Kök sözlük olmalı; bölümler sözlük ya da boş olmalı (boş → {})."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("Config kökü bir sözlük (anahtar: değer) olmalı")
    out = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ConfigError(f"Geçersiz bölüm adı: {key!r}")
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ConfigError(f"'{key}' bölümü anahtar: değer listesi olmalı")
        out[key] = value
    return out


PORT_KEYS = ("smtp_port", "imap_port")


def _safe_port(value):
    """Geçerli bir TCP portu ise int, değilse None (hiçbir girdide hata atmaz)."""
    if isinstance(value, bool):
        return None
    try:
        port = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return port if 0 < port < 65536 else None


def _validate_ports(cfg: dict) -> dict:
    """Formdan/import'tan gelen port alanlarını doğrular ve int'e çevirir.

    Eskiden "abc" gibi bir port kaydediliyor, sonra /api/diagnostics/network
    ve /api/config/test-connection int() hatasıyla 500 veriyordu.
    """
    for section, values in cfg.items():
        for key in PORT_KEYS:
            if key not in values or values[key] in (None, ""):
                continue
            port = _safe_port(values[key])
            if port is None:
                raise ConfigError(f"'{section}.{key}' geçerli bir port olmalı (1-65535): "
                                  f"{values[key]!r}")
            values[key] = port
    return cfg


def _read_config() -> dict:
    """config.yaml'ı güvenle okur; yoksa {} döner, bozuksa ConfigError."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        return _normalize_config(_yaml_load(text))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as e:
        # RecursionError: aşırı iç içe YAML ([[[[...]]]]) — eskiden yakalanmıyor, 500 veriyordu
        raise ConfigError(f"config.yaml okunamadı: {type(e).__name__}: {e}") from e


def _salvage_config() -> tuple:
    """Bozuk config.yaml'ın yedeğini alır ve kurtarılabilen bölümleri döndürür.

    Eskiden dosya bozuksa kayıt existing={} ile yapılıyor, formda olmayan her
    bölüm (API key, diğer sunucuların şifreleri) yedeksiz siliniyordu.
    Returns: (kurtarılan bölümler, yedek dosya adı)
    """
    backup = CONFIG_PATH.with_name(
        f"{CONFIG_PATH.name}.bozuk-{datetime.now().strftime('%Y%m%d-%H%M%S')}-"
        f"{uuid.uuid4().hex[:6]}.bak")
    raw = CONFIG_PATH.read_bytes()
    backup.write_bytes(raw)
    salvaged = {}
    try:
        data = _yaml_load(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 — sözdizimi bozuk: kurtarılacak bölüm yok
        data = None
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, dict):
                salvaged[key] = value
            elif isinstance(key, str) and value is None:
                salvaged[key] = {}
    return salvaged, backup.name


def _write_config(data: dict) -> None:
    # Her yazım kendi geçici dosyasını kullanır; ortak ".tmp" adı eşzamanlı
    # yazımlarda birinin dosyasını diğerinin os.replace'ine kaptırıyordu.
    tmp = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        with _config_lock:
            with open(tmp, "w", encoding="utf-8") as f:
                _yaml_dump(data, f)
            os.replace(tmp, CONFIG_PATH)  # yarım yazılmış config kalmasın
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

def _is_unset(value) -> bool:
    """Boş ya da maskeden ibaret (UI'dan dokunulmadan dönen) secret değeri."""
    if not value:
        return True
    return isinstance(value, str) and set(value) == {"•"}

def _mask_secrets(data: dict) -> dict:
    """Secret'lar tarayıcıya düz metin gitmesin — UI yalnızca dolu/boş bilgisine bakar."""
    for srv in ("ems", "gmail", "outlook"):
        if isinstance(data.get(srv), dict):
            for key in ("password", "totp_secret"):
                if data[srv].get(key):
                    data[srv][key] = SECRET_MASK
    for section in ("anthropic", "gemini"):
        if isinstance(data.get(section), dict) and data[section].get("api_key"):
            data[section]["api_key"] = SECRET_MASK
    return data

def _json_body() -> dict:
    """JSON gövdeyi sözlük olarak döndürür; bozuk/eksik gövde → {}."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@app.route("/api/config", methods=["GET"])
def get_config():
    try:
        data = _read_config()
    except ConfigError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not CONFIG_PATH.exists():
        return jsonify({"ok": True, "config": None})
    return jsonify({"ok": True, "config": _mask_secrets(data)})

@app.route("/api/config", methods=["POST"])
def save_config():
    try:
        incoming = _normalize_config(_json_body().get("config"))
    except ConfigError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not incoming:
        return jsonify({"ok": False, "error": "Kaydedilecek config boş"}), 400
    try:
        _validate_ports(incoming)
    except ConfigError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    with _config_lock:
        return _merge_and_save(incoming)


def _merge_and_save(incoming: dict):
    """save_config'in oku-birleştir-yaz kısmı; _config_lock altında çağrılır."""
    warning = None
    try:
        existing = _read_config()
    except ConfigError as e:
        # Bozuk dosya: önce yedeklenir, geçerli bölümleri korunur, formdan
        # gelen config ile onarılır.
        try:
            existing, backup_name = _salvage_config()
        except OSError as oe:
            return jsonify({"ok": False, "error": f"Bozuk config yedeklenemedi: {oe}"}), 500
        warning = (f"Mevcut config.yaml bozuktu ({e}); yedeği {backup_name} olarak "
                   f"alındı ve geçerli bölümleri ({', '.join(existing) or 'yok'}) korundu.")
        app.logger.warning(warning)

    # Alan bazında birleştir: formda olmayan anahtarlar (calendar_*, smime_*,
    # smtp_use_ssl, tls_* ...) silinmesin; boş/maskeli secret'lar korunur.
    merged = {k: dict(v) for k, v in existing.items()}
    for section, values in incoming.items():
        old = existing.get(section, {})
        new = {**old, **values}
        secret_keys = (("password", "totp_secret") if section in SERVER_KEYS
                       else ("api_key",) if section in ("anthropic", "gemini") else ())
        for key in secret_keys:
            if key in values and values[key] is None:
                new[key] = ""                      # formdan açıkça silindi
            elif _is_unset(values.get(key)):
                new[key] = old.get(key, "")
        merged[section] = new
    try:
        _write_config(merged)
        return jsonify({"ok": True, **({"warning": warning} if warning else {})})
    except RecursionError:
        return jsonify({"ok": False, "error": "Config çok derin iç içe değer içeriyor"}), 400
    except OSError as e:
        return jsonify({"ok": False, "error": f"Config yazılamadı: {e}"}), 500

@app.route("/api/config/import", methods=["POST"])
def import_config():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Dosya bulunamadı"}), 400
    f = request.files["file"]
    if not (f.filename or "").lower().endswith((".yaml", ".yml")):
        return jsonify({"ok": False, "error": "Yalnızca .yaml / .yml dosyaları kabul edilir"}), 400
    try:
        data = _validate_ports(_normalize_config(_yaml_load(f.read().decode("utf-8-sig"))))
    except RecursionError:
        return jsonify({"ok": False, "error": "YAML çok derin iç içe yapı içeriyor"}), 400
    except UnicodeDecodeError:
        return jsonify({"ok": False, "error": "Dosya UTF-8 kodlamalı olmalı"}), 400
    except yaml.YAMLError as e:
        return jsonify({"ok": False, "error": f"YAML okunamadı: {e}"}), 400
    except ConfigError as e:
        return jsonify({"ok": False, "error": f"Geçersiz YAML yapısı: {e}"}), 400
    if not data:
        return jsonify({"ok": False, "error": "Dosya boş"}), 400
    try:
        _write_config(data)
    except RecursionError:
        return jsonify({"ok": False, "error": "YAML çok derin iç içe yapı içeriyor"}), 400
    except OSError as e:
        return jsonify({"ok": False, "error": f"Config yazılamadı: {e}"}), 500
    return jsonify({"ok": True, "config": _mask_secrets(data)})

@app.route("/api/config/export", methods=["GET"])
def export_config():
    if not CONFIG_PATH.exists():
        return jsonify({"ok": False, "error": "Henüz kaydedilmiş config yok"}), 404
    return send_file(CONFIG_PATH, as_attachment=True, download_name="config.yaml", mimetype="text/yaml")

def _same_target(form: dict, saved: dict) -> bool:
    """Formdaki sunucu/kullanıcı kayıtlı olanla aynı mı?

    Kayıtlı şifre yalnızca kayıtlı host+kullanıcı için kullanılır; aksi halde
    formda host'u değiştirip şifreyi boş bırakan biri, kayıtlı şifreyi kendi
    sunucusuna gönderterek ele geçirebilirdi.
    """
    from mail_transport import connection_fingerprint
    return connection_fingerprint(form) == connection_fingerprint(saved)


def _login_with_code(do_login, auth_method: str, username: str, password: str, code: str,
                     fallback_errors: tuple):
    if auth_method == "otp_only" and code:
        do_login(username, code)
    elif auth_method == "totp_password" and code:
        try:
            do_login(username, password + code)
        except fallback_errors:
            do_login(username, password)
    else:
        do_login(username, password)


@app.route("/api/config/test-connection", methods=["POST"])
def test_connection():
    import imaplib, smtplib
    from mail_transport import Deadline, explain_connection_error, imap_connect, smtp_connect

    body       = _json_body()
    server_key = str(body.get("server") or "")
    mfa_code   = str(body.get("mfa_code") or "").strip()
    body_sc    = body.get("server_config") if isinstance(body.get("server_config"), dict) else {}
    try:
        saved = _read_config().get(server_key, {})
    except ConfigError as e:
        return jsonify({"ok": False, "error": str(e)})

    # Form değerleri kaydedilmeden test edilebilir; secret alanı boş/maskeli
    # geldiyse — ve hedef aynıysa — dosyadaki kayıtlı değer kullanılır.
    sc = dict(saved)
    if body_sc:
        sc = dict(body_sc)
        for key in ("password", "totp_secret"):
            if _is_unset(sc.get(key)):
                sc[key] = saved.get(key, "") if _same_target(body_sc, saved) else ""
    if not sc:
        return jsonify({"ok": False, "error": f"'{server_key}' için config bulunamadı — önce ayarları kaydedin"})

    auth_method = sc.get("auth_method", "password")
    totp_secret = sc.get("totp_secret", "")
    mfa_method  = sc.get("mfa_method", "totp")
    label       = sc.get("label") or server_key.upper()

    code = ""
    if auth_method in ("totp_password", "otp_only"):
        if totp_secret and mfa_method == "totp":
            code = generate_totp(totp_secret)
        elif mfa_code:
            code = mfa_code
        else:
            return jsonify({
                "ok": False, "needs_mfa": True,
                "mfa_method": mfa_method,
                "server_label": label,
                "error": "2FA kodu gerekli",
            })

    username, password = sc.get("username", ""), sc.get("password", "")
    if not sc.get("smtp_host") or not sc.get("smtp_port"):
        return jsonify({"ok": False, "error": "SMTP sunucu adı ve portu gerekli"})
    if not password and auth_method != "otp_only":
        return jsonify({"ok": False, "error": "Şifre boş — formda şifreyi girin"})

    stage = "SMTP"
    # Toplam süre sınırı: satır damlatan bir sunucu isteği süresiz tutamasın.
    deadline = Deadline(TEST_CONNECTION_DEADLINE)
    try:
        smtp = smtp_connect(sc, timeout=15, deadline=deadline)
        try:
            _login_with_code(smtp.login, auth_method, username, password, code,
                             (smtplib.SMTPException,))
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

        imap_note = ""
        if sc.get("imap_host") and sc.get("imap_port"):
            if auth_method == "otp_only" and not totp_secret:
                imap_note = " (IMAP, tek kullanımlık kod yeniden kullanılamayacağı için test edilmedi)"
            else:
                stage = "IMAP"
                imap = imap_connect(sc, timeout=15, deadline=deadline)
                try:
                    _login_with_code(imap.login, auth_method, username, password, code,
                                     (imaplib.IMAP4.error,))
                    status, _ = imap.select("INBOX", readonly=True)
                    if status != "OK":
                        raise imaplib.IMAP4.error("INBOX açılamadı")
                finally:
                    try:
                        imap.logout()
                    except Exception:
                        pass
                imap_note = " (SMTP + IMAP)"
        from mail_transport import _as_bool, smtp_mode
        # Arayüzdeki güvenlik rozetleri gerçek bağlantı moduna göre çizilir;
        # eskiden düz SMTP / tls_verify:false için de "Güvenli" gösteriliyordu.
        security = {"smtp_mode": smtp_mode(sc),
                    "tls_verify": _as_bool(sc.get("tls_verify"), True)}
        return jsonify({"ok": True, "message": f"✅ {label} bağlantısı başarılı{imap_note}",
                        "security": security})
    except Exception as e:  # noqa: BLE001 — her hata kullanıcıya açıklanır
        if deadline.expired:
            return jsonify({"ok": False, "stage": stage, "error": (
                f"{stage}: sunucu {deadline.seconds:.0f} sn içinde yanıtlamayı tamamlamadı — "
                "bağlantı kesildi. Host/port ve TLS modunu kontrol edin.")})
        hint = explain_connection_error(e, sc)
        msg = f"{stage}: {e}"
        return jsonify({"ok": False, "error": f"{msg} — {hint}" if hint else msg, "stage": stage})
    finally:
        deadline.close()

# ── 2FA / MFA ───────────────────────────────────────────────────
@app.route("/api/mfa/status", methods=["GET"])
def mfa_status():
    pending = mfa_manager.get_pending()
    return jsonify({"pending": pending is not None, "challenge": pending})

@app.route("/api/mfa/submit", methods=["POST"])
def mfa_submit():
    raw = _json_body().get("code")
    code = str(raw).strip() if isinstance(raw, (str, int)) and not isinstance(raw, bool) else ""
    if not code:
        return jsonify({"ok": False, "error": "Kod boş olamaz"})
    if len(code) > 32 or any(ch.isspace() for ch in code):
        return jsonify({"ok": False, "error": "Geçersiz kod biçimi"})
    ok = mfa_manager.submit_code(code)
    return jsonify({"ok": ok, **({} if ok else {"error": "Bekleyen 2FA isteği yok"})})

@app.route("/api/mfa/cancel", methods=["POST"])
def mfa_cancel():
    return jsonify({"ok": mfa_manager.cancel()})

@app.route("/api/mfa/totp-preview", methods=["POST"])
def totp_preview():
    secret = _json_body().get("secret")
    if not isinstance(secret, str) or not secret.strip():
        return jsonify({"ok": False, "error": "Secret boş"})
    code = generate_totp(secret)
    return jsonify({"ok": bool(code), "code": code, "remaining": totp_remaining_seconds()})

# ── COMBINATIONS ────────────────────────────────────────────────
@app.route("/api/combinations", methods=["GET"])
def get_combinations():
    try:
        from csv_parser import parse_csv
        csv_path = _get_csv_path()
        resolved, warning = _resolve_csv_path()
        if resolved != csv_path:
            warning = None
        if not os.path.exists(csv_path):
            return jsonify({"ok": False, "error": f"CSV bulunamadı: {csv_path}"})
        combos = parse_csv(csv_path)
        if not combos:
            warning = ((warning + " ") if warning else "") + \
                f"{Path(csv_path).name} içinde test kombinasyonu bulunamadı."
        result = [{"index":i,"label":c.label,
                   "receiver_server":c.receiver_server,"receiver_client":c.receiver_client,
                   "sender_server":c.sender_server,"sender_client":c.sender_client,
                   "scenarios":list(c.scenarios.keys()),
                   "step_count":sum(len(s.steps) for s in c.scenarios.values())}
                  for i,c in enumerate(combos)]
        return jsonify({"ok": True, "combinations": result, "csv": Path(csv_path).name,
                        **({"warning": warning} if warning else {})})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

def _get_csv_path() -> str:
    return _resolve_csv_path()[0]


def _resolve_csv_path() -> tuple:
    """(kullanılacak CSV yolu, uyarı). Yapılandırılan dosya bulunamazsa varsayılana
    düşülür ama bu artık uyarıyla bildirilir (eskiden sessizdi)."""
    configured = None
    try:
        configured = (_read_config().get("test") or {}).get("csv_input")
    except ConfigError:
        pass
    raw = str(configured or "mail_test_checklist.csv")

    repo_root = Path(__file__).parent
    p = Path(raw)
    exact = [p] if p.is_absolute() else [p, repo_root / p, repo_root / p.name]
    for c in exact:
        try:
            if c.exists() and c.is_file():
                return str(c), None
        except OSError:
            continue

    fallbacks = [repo_root / "mail_test_checklist.csv"] + sorted(
        (x for x in repo_root.glob("*.csv") if x.is_file()),
        key=lambda x: (0 if "checklist" in x.name.lower() else 1,
                       0 if "mail" in x.name.lower() else 1, len(x.name)))
    for c in fallbacks:
        if c.exists() and c.is_file():
            warning = (f"Yapılandırılan test CSV'si bulunamadı ({raw}); "
                       f"bunun yerine {c.name} gösteriliyor.") if configured else None
            return str(c), warning
    return raw, None

# ── HEALTH ──────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"ok": True, "status": "healthy", "serverless": _is_serverless()})

# ── NETWORK DIAGNOSTICS ─────────────────────────────────────────
# Tarayıcıdan açılabilir: hangi SMTP/IMAP portlarının bu sunucudan
# erişilebilir olduğunu gösterir. Render/Heroku gibi platformlar giden
# SMTP portlarını kapatır → "Network is unreachable" hatasının teşhisi.
_DIAG_TARGETS = [
    ("Gmail SMTP (STARTTLS)",   "smtp.gmail.com",         587),
    ("Gmail SMTP (SSL)",        "smtp.gmail.com",         465),
    ("Gmail IMAP",              "imap.gmail.com",         993),
    ("Outlook SMTP (STARTTLS)", "smtp.office365.com",     587),
    ("Outlook IMAP",            "outlook.office365.com",  993),
    ("HTTPS (kontrol)",         "api.github.com",         443),
]

def _classify_socket_error(e: OSError) -> str:
    if isinstance(e, socket.gaierror):
        return "dns"
    if isinstance(e, (socket.timeout, TimeoutError)) or getattr(e, "errno", None) == errno.ETIMEDOUT:
        return "timeout"
    if isinstance(e, ConnectionRefusedError):
        return "refused"
    return "unreachable"


@app.route("/api/diagnostics/network")
def diagnostics_network():
    results = []
    targets = list(_DIAG_TARGETS)
    # Kayıtlı sunucuların kendi portları da denenir (ör. kurum içi EMS).
    try:
        cfg = _read_config()
    except ConfigError:
        cfg = {}
    for key in SERVER_KEYS:
        sc = cfg.get(key) or {}
        for proto, host_key, port_key in (("SMTP", "smtp_host", "smtp_port"),
                                          ("IMAP", "imap_host", "imap_port")):
            host, port = sc.get(host_key), _safe_port(sc.get(port_key))
            # Geçersiz port (elle düzenlenmiş dosya) hedef dışı bırakılır — eskiden 500
            if host and port and (str(host), port) not in {(h, p) for _, h, p in targets}:
                targets.append((f"{sc.get('label') or key.upper()} {proto} (config)", str(host), port))

    for label, host, port in targets:
        entry = {"label": label, "host": host, "port": port}
        start = time.time()
        try:
            with socket.create_connection((host, port), timeout=8):
                pass
            entry["reachable"] = True
            entry["ms"] = round((time.time() - start) * 1000)
        except OSError as e:
            entry["reachable"] = False
            entry["reason"] = _classify_socket_error(e)
            entry["error"] = f"{type(e).__name__}: {e}"
        results.append(entry)

    smtp_entries = [r for r in results if r["port"] in (25, 465, 587)]
    any_smtp_open = any(r["reachable"] for r in smtp_entries)
    https_ok = any(r["reachable"] for r in results if r["port"] == 443)
    smtp_reasons = {r.get("reason") for r in smtp_entries if not r["reachable"]}
    if any_smtp_open:
        verdict = "SMTP portları açık — bu sunucudan mail gönderilebilir."
    elif smtp_reasons == {"dns"}:
        verdict = ("Mail sunucularının adları çözümlenemiyor (DNS). Sunucunun DNS/dış ağ "
                   "erişimi kısıtlı ya da yalnızca bir HTTP proxy üzerinden çıkabiliyor — "
                   "SMTP/IMAP için doğrudan ağ erişimi gerekir.")
    elif https_ok:
        verdict = ("Bu sunucu internete çıkabiliyor ANCAK giden SMTP portları "
                   "(587/465) engelli. Platform (ör. Render ücretsiz plan, bazı VPS "
                   "sağlayıcılarında yeni hesaplar) mail göndermeye izin vermiyor — "
                   "SMTP çıkışı açık bir host gerekir ya da sağlayıcıdan port açılması "
                   "istenmeli.")
    else:
        verdict = "Sunucunun dış ağ erişimi kısıtlı görünüyor."

    return jsonify({"ok": True, "verdict": verdict,
                    "smtp_available": any_smtp_open, "targets": results})

# ── RUNNER ──────────────────────────────────────────────────────
def _parse_combos(value):
    """combo: int | "3" | [1, 4] | "1,4" → sıralı benzersiz int listesi (geçersizse ValueError)."""
    if value is None or value == "" or value == []:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    out = []
    for item in items:
        if isinstance(item, bool):
            raise ValueError
        n = int(str(item).strip())
        if n < 0:
            raise ValueError
        if n not in out:
            out.append(n)
    return out


def _kill_tree(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """main.py'yi önce nazikçe (SIGTERM), olmazsa zorla sonlandırır."""
    try:
        proc.terminate()
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=grace)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


def _reap_orphan_run() -> None:
    """Önceki app sürecinden kalan (app öldürülmüş ama main.py yaşayan) koşuyu temizler."""
    try:
        pid = int(RUN_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        if b"main.py" in cmdline:
            os.kill(pid, signal.SIGTERM)
            print(f"⚠️  Önceki çalıştırmadan kalan test süreci sonlandırıldı (pid {pid})")
    except (OSError, AttributeError):
        pass  # süreç yok / /proc yok (Windows, macOS) / yetki yok
    try:
        RUN_PID_FILE.unlink()
    except OSError:
        pass


@app.route("/api/run/start", methods=["POST"])
def start_run():
    if _is_serverless():
        return jsonify({
            "ok": False,
            "error": ("Bu platform (Vercel/serverless) uzun süreli test koşusunu "
                      "desteklemiyor — istekler 60 sn ile sınırlı ve arka plan "
                      "süreci yaşatılamıyor. Konfigürasyon arayüzü çalışır; test "
                      "koşusu için Railway, Fly.io, VPS veya Docker kullanın "
                      "(README → Web'de Yayınlama)."),
        }), 400

    body = _json_body()
    try:
        combos = _parse_combos(body.get("combo", body.get("combos")))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Geçersiz kombinasyon seçimi"}), 400
    scenario = body.get("scenario")
    if scenario is not None and (not isinstance(scenario, str)
                                 or (scenario and scenario not in _supported_scenarios())):
        return jsonify({"ok": False, "error": "Geçersiz senaryo tipi"}), 400

    cmd = [sys.executable, str(BASE_DIR / "main.py"), "--config", str(CONFIG_PATH)]
    if combos:
        cmd += ["--combo", ",".join(str(c) for c in combos)]
    if scenario:
        cmd += ["--scenario", scenario]
    if body.get("dry_run"):
        cmd += ["--dry-run"]

    with _run_lock:
        if run_state["running"]:
            return jsonify({"ok": False, "error": "Zaten bir test çalışıyor"}), 400
        mfa_manager.clear_bridge()  # önceki çalışmadan kalan challenge dosyaları
        run_id = uuid.uuid4().hex
        # run_id eki: aynı saniyede başlayan iki koşu ("w" kipinde) aynı dosyayı paylaşıyordu
        log_file = LOGS_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}.log"
        # MFA_INTERACTIVE=1: subprocess'teki sender/receiver, TOTP secret yoksa
        # kodu köprü üzerinden arayüz modal'ından ister. UTF-8 zorlanır ki Türkçe
        # Windows'ta (cp1254) emoji/ğüş içeren loglar okuyucu thread'i öldürmesin.
        env = {**os.environ, "MFA_INTERACTIVE": "1", "PYTHONUTF8": "1",
               "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
               "DATA_DIR": str(DATA_DIR)}
        try:
            # cwd=DATA_DIR: main.py reports/ logs/ test_files/ dizinlerini çalışma
            # dizinine yazar; arayüz de raporları DATA_DIR/reports'tan okur.
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", cwd=str(DATA_DIR), env=env,
            )
        except OSError as e:
            return jsonify({"ok": False, "error": f"Test süreci başlatılamadı: {e}"}), 500
        run_state.update({"pump_done": False, "running": True, "process": proc,
                          "run_id": run_id, "stopped": False, "log_file": str(log_file),
                          "started_at": datetime.now().isoformat(),
                          "finished_at": None, "exit_code": None})
        try:
            RUN_PID_FILE.write_text(str(proc.pid))
        except OSError:
            pass

    def _pump():
        # Satırlar yalnızca dosyaya yazılır; eskiden ayrıca hiç okunmayan bir
        # kuyruğa da konuyor, koşunun tüm çıktısı bellekte birikiyordu.
        try:
            with open(log_file, "w", encoding="utf-8") as lf:
                for line in proc.stdout:
                    lf.write(line.rstrip() + "\n")
                    lf.flush()
        except Exception as e:  # noqa: BLE001 — log okuyucu asla sessizce ölmesin
            err = f"[HATA] Log okunamadı: {e}"
            try:
                with open(log_file, "a", encoding="utf-8") as lf:
                    lf.write(err + "\n")
            except OSError:
                pass
        finally:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
            with _run_lock:
                if run_state.get("run_id") == run_id:  # yeni bir koşuyu ezme
                    run_state.update({"running": False, "process": None,
                                      "exit_code": proc.returncode,
                                      "finished_at": datetime.now().isoformat()})
                    try:
                        RUN_PID_FILE.unlink()
                    except OSError:
                        pass
                if run_state.get("run_id") == run_id:
                    run_state["pump_done"] = True

    threading.Thread(target=_pump, daemon=True).start()
    return jsonify({"ok": True, "cmd": " ".join(cmd)})

@app.route("/api/run/stop", methods=["POST"])
def stop_run():
    mfa_manager.cancel()
    with _run_lock:
        proc = run_state.get("process")
        run_id = run_state.get("run_id")
        if not run_state["running"] or proc is None:
            return jsonify({"ok": False, "error": "Çalışan süreç yok"})
        run_state["stopped"] = True
    _kill_tree(proc)
    mfa_manager.clear_bridge()  # öldürülen süreç bekleyen 2FA isteğini temizleyemez
    with _run_lock:
        # Süreç bitti; okuyucu thread'i beklemeden durumu güncelle ki arayüz
        # hemen yeni koşu başlatabilsin. run_id kontrolü: araya yeni koşu
        # girdiyse onun durumuna dokunma.
        if run_state.get("run_id") == run_id:
            run_state.update({"running": False, "process": None,
                              "exit_code": getattr(proc, "returncode", None),
                              "finished_at": datetime.now().isoformat()})
    return jsonify({"ok": True})

@app.route("/api/run/status", methods=["GET"])
def run_status():
    return jsonify({"running": run_state["running"], "started_at": run_state["started_at"],
                    "finished_at": run_state["finished_at"], "exit_code": run_state["exit_code"],
                    "stopped": run_state.get("stopped", False)})

@app.route("/api/run/logs")
def poll_logs():
    """Log polling endpoint — SSE yerine kullanılır (Vercel uyumlu).

    Yanıt en fazla LOG_POLL_MAX_LINES satır içerir (istemci next_offset ile
    devam eder); eskiden tüm log tek yanıtta dönüyordu. İstemcinin offset'i
    dosyadan büyükse (yeni koşu başladı, eski sekme) baştan gönderilir ve
    ``reset: true`` döner; eskiden yeni koşu boyunca hiç satır gelmiyordu.
    """
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    log_file = run_state.get("log_file")
    lines, total, reset = [], 0, False

    if log_file and Path(log_file).exists():
        for _ in range(2):
            lines, total = [], 0
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                for total, line in enumerate(f, 1):
                    if offset < total <= offset + LOG_POLL_MAX_LINES:
                        lines.append(line.rstrip())
            if offset <= total:
                break
            offset, reset = 0, True
    next_offset = offset + len(lines)

    # Okuyucu thread'i son satırları yazmadan (ör. Durdur sonrası) "bitti" denmesin.
    finished = not run_state["running"] and run_state.get("pump_done", True)
    # Satır dönen poll'da done=False: istemci kalanları boşaltmadan durmasın.
    done = finished and not lines and next_offset >= total
    return jsonify({"lines": lines, "next_offset": next_offset, "done": done,
                    "reset": reset, "run_id": run_state.get("run_id")})

# ── REPORTS ─────────────────────────────────────────────────────
def _inside_reports(path: Path) -> bool:
    """Dosya (sembolik bağ çözüldükten sonra) raporlar dizininde mi?"""
    try:
        return path.resolve(strict=True).is_relative_to(REPORTS_DIR.resolve())
    except (OSError, RuntimeError):
        return False


def _report_files(pattern: str) -> list:
    """(yol, stat) listesi, yeniden eskiye. Kırık sembolik bağ, dizin dışına
    işaret eden bağ ve listeleme sırasında silinen dosya atlanır (eskiden 500)."""
    out = []
    for f in REPORTS_DIR.glob(pattern):
        if not _inside_reports(f):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if f.is_file():
            out.append((f, st))
    out.sort(key=lambda x: x[1].st_mtime, reverse=True)
    return out


@app.route("/api/reports")
def list_reports():
    files = []
    for ext in ("*.html", "*.csv"):
        for f, st in _report_files(ext):
            files.append({"name": f.name, "size": st.st_size, "type": f.suffix[1:],
                          "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")})
    return jsonify({"ok": True, "reports": files})

@app.route("/api/reports/<path:filename>")
def get_report(filename):
    # send_from_directory path traversal'ı engeller (../ ile dizin dışına çıkılamaz);
    # sembolik bağ ile dizin dışına çıkış da reddedilir.
    if not _inside_reports(REPORTS_DIR / filename):
        return jsonify({"ok": False}), 404
    try:
        return send_from_directory(REPORTS_DIR, filename)
    except Exception:
        return jsonify({"ok": False}), 404

def _results_csv_name() -> str:
    try:
        raw = (_read_config().get("test") or {}).get("results_csv")
    except ConfigError:
        raw = None
    return Path(str(raw or "test_results.csv")).name

@app.route("/api/results/latest")
def latest_results():
    # Önce yapılandırılmış sonuç dosyası; yoksa en yeni CSV (eskiden raporlar
    # dizinindeki HERHANGİ bir en yeni CSV seçiliyordu).
    preferred = REPORTS_DIR / _results_csv_name()
    files = [f for f, _ in _report_files("*.csv")]
    if preferred in files:
        files.remove(preferred)
        files.insert(0, preferred)
    if not files:
        return jsonify({"ok": False, "error": "Henüz sonuç yok"})
    rows = []
    try:
        with open(files[0], "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.DictReader(f, restkey="_fazla", restval="")
            for row in reader:
                row.pop("_fazla", None)          # fazla sütunlu satır 500'e yol açıyordu
                rows.append({str(k): v for k, v in row.items() if k is not None})
    except (OSError, csv.Error) as e:
        return jsonify({"ok": False, "error": f"Sonuç dosyası okunamadı ({files[0].name}): {e}"})
    return jsonify({"ok": True, "results": rows, "file": files[0].name})

@app.route("/")
def index():
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp

if __name__ == "__main__":
    env_port = os.environ.get("PORT")
    desired_port = int(env_port) if env_port else 5000
    # Yerelde varsayılan 127.0.0.1 (güvenli); PaaS'ta otomatik 0.0.0.0.
    # Elle geçersiz kılmak için HOST env değişkeni.
    host = os.environ.get("HOST", _default_host())
    # Werkzeug debugger uzaktan kod çalıştırmaya izin verir — varsayılan kapalı.
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")

    def _is_port_free(p: int) -> bool:
        # Sunucunun bind edeceği host üzerinde test bind yapar.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s4:
                s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s4.bind((host if host != "::" else "0.0.0.0", p))
        except OSError:
            return False

        if host == "0.0.0.0":
            try:
                with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s6:
                    s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s6.bind(("::", p))
            except OSError as e:
                # Yalnızca "port kullanımda" meşgul sayılır; IPv6'sı olmayan
                # makinede (EAFNOSUPPORT/EADDRNOTAVAIL) port yine de boştur.
                if e.errno == errno.EADDRINUSE:
                    return False

        return True

    port = desired_port
    if env_port is None:
        while port < desired_port + 20 and not _is_port_free(port):
            port += 1

    _reap_orphan_run()
    if not os.environ.get("UI_PASSWORD"):
        if _env_flag("ALLOW_NO_AUTH"):
            print("⚠️  UYARI: UI_PASSWORD yok ve ALLOW_NO_AUTH=1 — arayüz ağdaki herkese "
                  "parolasız açık (kayıtlı şifreler dahil). Yalnızca güvenilir ağda kullanın.")
        elif host not in ("127.0.0.1", "localhost", "::1"):
            print("⚠️  UI_PASSWORD ayarlı değil: arayüz yalnızca bu makineden (localhost) "
                  "kullanılabilir, ağdan gelen istekler 403 alır. İnternete/ağa açmak için "
                  "UI_PASSWORD ortam değişkenini ayarlayın.")

    print(f"\n🚀 Mail Otomasyon Arayüzü: http://localhost:{port}\n")
    app.run(debug=debug, host=host, port=port, threaded=True)
