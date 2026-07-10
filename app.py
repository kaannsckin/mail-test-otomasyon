"""
app.py — Mail Otomasyon Web Arayüzü (Flask)
Çalıştır: python app.py  →  http://localhost:5000 (boşsa) / otomatik alternatif port
"""

import csv, hmac, json, logging, os, queue, socket, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template, request, send_file, send_from_directory
from auth_manager import mfa_manager, generate_totp, totp_remaining_seconds

BASE_DIR = Path(__file__).parent

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # tarayıcı bayat JS/CSS önbelleklemesin


def _data_dir() -> Path:
    """Yazılabilir veri dizini (config.yaml, reports/, logs/).

    Öncelik: DATA_DIR ortam değişkeni → yerelde/Railway'de repo kökü
    (README akışıyla uyumlu) → Vercel gibi salt-okunur FS'lerde /tmp.
    """
    override = os.environ.get("DATA_DIR")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d
    if os.environ.get("VERCEL") or not os.access(BASE_DIR, os.W_OK):
        d = Path(os.environ.get("TMPDIR", "/tmp")) / "mail_otomasyon"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return BASE_DIR


DATA_DIR      = _data_dir()
CONFIG_PATH   = DATA_DIR / os.environ.get("CONFIG_FILENAME", "config.yaml")
REPORTS_DIR   = DATA_DIR / "reports"
LOGS_DIR      = DATA_DIR / "logs"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

run_state = {
    "running": False,
    "process": None,
    "log_queue": queue.Queue(),
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
}

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


# ── AUTH (opsiyonel) ───────────────────────────────────────────
# UI_PASSWORD ayarlanırsa tüm istekler HTTP Basic Auth gerektirir.
# Ayarlanmazsa mevcut davranış korunur (yalnızca localhost'ta kullan).
@app.before_request
def _require_basic_auth():
    if request.path == "/api/health":
        return None  # platform healthcheck'leri kimlik bilgisi gönderemez
    password = os.environ.get("UI_PASSWORD", "")
    if not password:
        return None
    username = os.environ.get("UI_USERNAME", "admin")
    auth = request.authorization
    if (auth and auth.type == "basic"
            and hmac.compare_digest(auth.username or "", username)
            and hmac.compare_digest(auth.password or "", password)):
        return None
    return Response(
        "Kimlik doğrulama gerekli", 401,
        {"WWW-Authenticate": 'Basic realm="Mail Otomasyon"'},
    )

# ── CONFIG ──────────────────────────────────────────────────────
SECRET_MASK = "••••••••"

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

@app.route("/api/config", methods=["GET"])
def get_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return jsonify({"ok": True, "config": _mask_secrets(data)})
    return jsonify({"ok": True, "config": None})

@app.route("/api/config", methods=["POST"])
def save_config():
    data = request.json.get("config", {})
    existing = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    for srv in ["ems", "gmail", "outlook"]:
        if srv in data and srv in existing:
            for key in ("password", "totp_secret"):
                if _is_unset(data[srv].get(key)):
                    data[srv][key] = existing[srv].get(key, "")
    for section in ("anthropic", "gemini"):
        if section in data and isinstance(existing.get(section), dict):
            if _is_unset(data[section].get("api_key")):
                data[section]["api_key"] = existing[section].get("api_key", "")
    # UI'ın göndermediği bölümler (analysis, gemini, logging vb.) silinmesin
    for section, value in existing.items():
        if section not in data:
            data[section] = value
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/config/import", methods=["POST"])
def import_config():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Dosya bulunamadı"}), 400
    f = request.files["file"]
    if not f.filename.endswith((".yaml", ".yml")):
        return jsonify({"ok": False, "error": "Yalnızca .yaml / .yml dosyaları kabul edilir"}), 400
    try:
        content = f.read().decode("utf-8")
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Geçersiz YAML yapısı"}), 400
        with open(CONFIG_PATH, "w", encoding="utf-8") as out:
            yaml.dump(data, out, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return jsonify({"ok": True, "config": _mask_secrets(data)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/config/export", methods=["GET"])
def export_config():
    if not CONFIG_PATH.exists():
        return jsonify({"ok": False, "error": "Henüz kaydedilmiş config yok"}), 404
    return send_file(CONFIG_PATH, as_attachment=True, download_name="config.yaml", mimetype="text/yaml")

@app.route("/api/config/test-connection", methods=["POST"])
def test_connection():
    server_key    = request.json.get("server")
    mfa_code      = request.json.get("mfa_code", "")
    body_sc       = request.json.get("server_config") or {}
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            sc = config.get(server_key, {})
        else:
            sc = {}
        # Form değerleri kaydedilmeden test edilebilir; secret alanı boş/maskeli
        # geldiyse dosyadaki kayıtlı değer kullanılır.
        if body_sc:
            merged = dict(body_sc)
            for key in ("password", "totp_secret"):
                if _is_unset(merged.get(key)) and sc.get(key):
                    merged[key] = sc[key]
            sc = merged
        if not sc:
            return jsonify({"ok": False, "error": f"'{server_key}' için config bulunamadı — önce ayarları kaydedin"})

        auth_method = sc.get("auth_method", "password")
        totp_secret = sc.get("totp_secret", "")
        mfa_method  = sc.get("mfa_method", "totp")

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
                    "server_label": sc.get("label", server_key.upper()),
                    "error": "2FA kodu gerekli",
                })

        import smtplib
        smtp = smtplib.SMTP(sc["smtp_host"], sc["smtp_port"], timeout=10)
        smtp.ehlo()
        if sc.get("smtp_use_tls"): smtp.starttls()
        pwd = sc["password"]
        if auth_method == "totp_password" and code:
            try: smtp.login(sc["username"], pwd + code)
            except smtplib.SMTPException: smtp.login(sc["username"], pwd)
        elif auth_method == "otp_only" and code:
            smtp.login(sc["username"], code)
        else:
            smtp.login(sc["username"], pwd)
        smtp.quit()
        return jsonify({"ok": True, "message": f"✅ {server_key.upper()} bağlantısı başarılı"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── 2FA / MFA ───────────────────────────────────────────────────
@app.route("/api/mfa/status", methods=["GET"])
def mfa_status():
    pending = mfa_manager.get_pending()
    return jsonify({"pending": pending is not None, "challenge": pending})

@app.route("/api/mfa/submit", methods=["POST"])
def mfa_submit():
    code = request.json.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "Kod boş olamaz"})
    ok = mfa_manager.submit_code(code)
    return jsonify({"ok": ok})

@app.route("/api/mfa/cancel", methods=["POST"])
def mfa_cancel():
    return jsonify({"ok": mfa_manager.cancel()})

@app.route("/api/mfa/totp-preview", methods=["POST"])
def totp_preview():
    secret = request.json.get("secret", "")
    if not secret:
        return jsonify({"ok": False, "error": "Secret boş"})
    code = generate_totp(secret)
    return jsonify({"ok": bool(code), "code": code, "remaining": totp_remaining_seconds()})

# ── COMBINATIONS ────────────────────────────────────────────────
@app.route("/api/combinations", methods=["GET"])
def get_combinations():
    try:
        from csv_parser import parse_csv
        csv_path = _get_csv_path()
        if not os.path.exists(csv_path):
            return jsonify({"ok": False, "error": f"CSV bulunamadı: {csv_path}"})
        combos = parse_csv(csv_path)
        result = [{"index":i,"label":c.label,
                   "receiver_server":c.receiver_server,"receiver_client":c.receiver_client,
                   "sender_server":c.sender_server,"sender_client":c.sender_client,
                   "scenarios":list(c.scenarios.keys()),
                   "step_count":sum(len(s.steps) for s in c.scenarios.values())}
                  for i,c in enumerate(combos)]
        return jsonify({"ok": True, "combinations": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

def _get_csv_path():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        raw = cfg.get("test", {}).get("csv_input", "mail_test_checklist.csv")
    else:
        raw = "mail_test_checklist.csv"

    # Resolve relative paths against repo root and fall back to a likely CSV
    repo_root = Path(__file__).parent
    p = Path(raw)
    candidates = [p]
    if not p.is_absolute():
        candidates += [repo_root / p, repo_root / p.name]
    candidates.append(repo_root / "mail_test_checklist.csv")
    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return str(c)
        except OSError:
            continue

    csvs = [x for x in repo_root.glob("*.csv") if x.is_file()]
    if csvs:
        preferred = sorted(
            csvs,
            key=lambda x: (
                0 if "checklist" in x.name.lower() else 1,
                0 if "mail" in x.name.lower() else 1,
                len(x.name),
            ),
        )[0]
        return str(preferred)

    return raw

# ── HEALTH ──────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"ok": True, "status": "healthy", "serverless": _is_serverless()})

# ── RUNNER ──────────────────────────────────────────────────────
@app.route("/api/run/start", methods=["POST"])
def start_run():
    if _is_serverless():
        return jsonify({
            "ok": False,
            "error": ("Bu platform (Vercel/serverless) uzun süreli test koşusunu "
                      "desteklemiyor — istekler 60 sn ile sınırlı ve arka plan "
                      "süreci yaşatılamıyor. Konfigürasyon arayüzü çalışır; test "
                      "koşusu için Railway, Render veya Docker kullanın "
                      "(README → Web'de Yayınlama)."),
        }), 400
    if run_state["running"]:
        return jsonify({"ok": False, "error": "Zaten bir test çalışıyor"}), 400
    body      = request.json or {}
    cmd       = [sys.executable, str(BASE_DIR / "main.py"), "--config", str(CONFIG_PATH)]
    if body.get("combo") is not None: cmd += ["--combo", str(body["combo"])]
    if body.get("scenario"):          cmd += ["--scenario", body["scenario"]]
    if body.get("dry_run"):           cmd += ["--dry-run"]
    run_state.update({"log_queue": queue.Queue(), "running": True,
                      "started_at": datetime.now().isoformat(),
                      "finished_at": None, "exit_code": None})
    mfa_manager.clear_bridge()  # önceki çalışmadan kalan challenge dosyaları
    log_file = LOGS_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    run_state["log_file"] = str(log_file)

    def _run():
        try:
            # MFA_INTERACTIVE=1: subprocess'teki sender/receiver, TOTP secret yoksa
            # kodu köprü üzerinden arayüz modal'ından ister.
            env = {**os.environ, "MFA_INTERACTIVE": "1"}
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", cwd=BASE_DIR, env=env)
            run_state["process"] = proc
            with open(log_file, "w", encoding="utf-8") as lf:
                for line in proc.stdout:
                    line = line.rstrip()
                    run_state["log_queue"].put(line)
                    lf.write(line + "\n")
                    lf.flush()
            proc.wait(); run_state["exit_code"] = proc.returncode
        except Exception as e:
            err = f"[HATA] {e}"
            run_state["log_queue"].put(err)
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write(err + "\n")
        finally:
            run_state.update({"running": False, "finished_at": datetime.now().isoformat()})
            run_state["log_queue"].put("__DONE__")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "cmd": " ".join(cmd)})

@app.route("/api/run/stop", methods=["POST"])
def stop_run():
    mfa_manager.cancel()
    if run_state.get("process"):
        run_state["process"].terminate()
        run_state["running"] = False
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Çalışan süreç yok"})

@app.route("/api/run/status", methods=["GET"])
def run_status():
    return jsonify({"running": run_state["running"], "started_at": run_state["started_at"],
                    "finished_at": run_state["finished_at"], "exit_code": run_state["exit_code"]})

@app.route("/api/run/logs")
def poll_logs():
    """Log polling endpoint — SSE yerine kullanılır (Vercel uyumlu)."""
    offset = int(request.args.get("offset", 0))
    log_file = run_state.get("log_file")
    lines = []
    next_offset = offset

    if log_file and Path(log_file).exists():
        with open(log_file, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        new_lines = all_lines[offset:]
        lines = [l.rstrip() for l in new_lines]
        next_offset = len(all_lines)

    done = (not run_state["running"]) and (next_offset == offset or not run_state.get("log_file"))
    return jsonify({"lines": lines, "next_offset": next_offset, "done": done})

# ── REPORTS ─────────────────────────────────────────────────────
@app.route("/api/reports")
def list_reports():
    files = []
    for ext in ("*.html","*.csv"):
        for f in sorted(REPORTS_DIR.glob(ext), key=os.path.getmtime, reverse=True):
            st = f.stat()
            files.append({"name":f.name,"size":st.st_size,"type":f.suffix[1:],
                          "mtime":datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")})
    return jsonify({"ok": True, "reports": files})

@app.route("/api/reports/<path:filename>")
def get_report(filename):
    # send_from_directory path traversal'ı engeller (../ ile dizin dışına çıkılamaz)
    try:
        return send_from_directory(REPORTS_DIR, filename)
    except Exception:
        return jsonify({"ok": False}), 404

@app.route("/api/results/latest")
def latest_results():
    files = sorted(REPORTS_DIR.glob("*.csv"), key=os.path.getmtime, reverse=True)
    if not files: return jsonify({"ok":False,"error":"Henüz sonuç yok"})
    rows = []
    with open(files[0],"r",encoding="utf-8-sig") as f:
        for row in csv.DictReader(f): rows.append(dict(row))
    return jsonify({"ok":True,"results":rows,"file":files[0].name})

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
            except OSError:
                # If IPv6 is unavailable or already bound, treat as not free.
                return False

        return True

    port = desired_port
    if env_port is None:
        while port < desired_port + 20 and not _is_port_free(port):
            port += 1

    print(f"\n🚀 Mail Otomasyon Arayüzü: http://localhost:{port}\n")
    app.run(debug=debug, host=host, port=port, threaded=True)
