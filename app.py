"""
app.py — Mail Otomasyon Web Arayüzü (Flask)
Çalıştır: python app.py  →  http://localhost:5000 (boşsa) / otomatik alternatif port
"""

import csv, json, logging, os, socket, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template, request, send_file, send_from_directory, stream_with_context
from werkzeug.utils import secure_filename
from oauth_manager import get_oauth_manager
from auth_manager import mfa_manager, generate_totp, totp_remaining_seconds
from apscheduler.schedulers.background import BackgroundScheduler
from notifications import send_alert

app = Flask(__name__)
# APP_ROOT: Kodların bulunduğu dizin (main.py, app.py vb.)
_APP_ROOT          = Path(__file__).parent.resolve()
# CONFIG_DIR: Verilerin/konfigürasyonun saklandığı dizin (Docker volume mapping için)
_CONFIG_DIR        = Path(os.environ.get("MAIL_AUTO_CONFIG_DIR", "")).resolve() \
                     if os.environ.get("MAIL_AUTO_CONFIG_DIR") else _APP_ROOT

CONFIG_PATH        = _CONFIG_DIR / "config.yaml"
REPORTS_DIR        = _CONFIG_DIR / "reports"
LOGS_DIR           = _CONFIG_DIR / "logs"
VERIFICATION_DIR   = _CONFIG_DIR / ".verifications"
ATTACHMENTS_DIR    = _CONFIG_DIR / "attachments"
REPORTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
VERIFICATION_DIR.mkdir(exist_ok=True)

# ── SCHEDULER ───────────────────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.start()

run_state = {
    "running": False,
    "process": None,
    "log_buffer": [],   # All log lines; "__DONE__" sentinel at end; SSE clients replay from here
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
}

# ── CONFIG ──────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def get_config():
    DOMAIN_DEFAULTS = {"ems": "meb.gov.tr", "gmail": "gmail.com", "outlook": "hotmail.com"}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        # İlk kullanım — boş şablon döndür, hata verme
        data = {}

    # Eski format: test_address varsa mail_addresses'e sentezle (backward compat)
    if "mail_addresses" not in data:
        ma = {}
        for srv in ["ems", "gmail", "outlook"]:
            sc = data.get(srv, {})
            addr = sc.get("test_address", "")
            if addr and "@" in addr:
                user, domain = addr.rsplit("@", 1)
                ma[srv] = {"username": user, "domain": domain, "address": addr}
            else:
                ma[srv] = {"username": "", "domain": DOMAIN_DEFAULTS.get(srv, ""), "address": ""}
        data["mail_addresses"] = ma
    return jsonify({"ok": True, "config": data})

@app.route("/api/config", methods=["POST"])
def save_config():
    data = request.json.get("config", {})
    existing = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    for srv in ["ems", "gmail", "outlook"]:
        if srv in data and srv in existing:
            if not data[srv].get("password"):
                data[srv]["password"] = existing[srv].get("password", "")
            if not data[srv].get("totp_secret"):
                data[srv]["totp_secret"] = existing[srv].get("totp_secret", "")
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/config/test-connection", methods=["POST"])
def test_connection():
    server_key = request.json.get("server")
    mfa_code   = request.json.get("mfa_code", "")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        sc = config.get(server_key, {})
        if not sc or not sc.get("smtp_host"):
            return jsonify({"ok": False, "error": f"'{server_key}' için SMTP adresi girilmemiş. Önce konfigürasyonu doldurup kaydedin."})

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
            except: smtp.login(sc["username"], pwd)
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

# ── PASSIVE VERIFICATION (Dosya-tabanlı IPC) ───────────────────
@app.route("/api/verification/pending")
def verification_pending():
    """Bekleyen manuel doğrulama varsa döndür."""
    pending = []
    if VERIFICATION_DIR.exists():
        for f in sorted(VERIFICATION_DIR.glob("pending_*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                pending.append(data)
            except (json.JSONDecodeError, IOError):
                continue
    return jsonify({"ok": True, "pending": pending})

@app.route("/api/verification/respond", methods=["POST"])
def verification_respond():
    """Manuel doğrulama yanıtı yaz — analyzer bunu okuyup devam edecek."""
    body = request.json or {}
    verify_id = body.get("id", "")
    approved = body.get("approved", False)
    issues = body.get("issues", [])

    if not verify_id:
        return jsonify({"ok": False, "error": "Verification ID eksik"}), 400

    VERIFICATION_DIR.mkdir(exist_ok=True)
    response_file = VERIFICATION_DIR / f"response_{verify_id}.json"
    pending_file = VERIFICATION_DIR / f"pending_{verify_id}.json"
    try:
        with open(response_file, "w", encoding="utf-8") as f:
            json.dump({"approved": approved, "issues": issues}, f)
        # Pending dosyasını da sil — eski testlerden kalan stale item'ları temizler
        pending_file.unlink(missing_ok=True)
        return jsonify({"ok": True, "id": verify_id, "approved": approved})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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

# ── RUNNER ──────────────────────────────────────────────────────
def _trigger_run(params: dict = None, is_autonomous: bool = False):
    """
    Subprocess ile main.py'yi çalıştıran merkezi motor.
    """
    if run_state["running"]:
        proc = run_state.get("process")
        if proc and proc.poll() is not None:
            run_state.update({"running": False, "finished_at": datetime.now().isoformat(),
                              "exit_code": proc.returncode})
        else:
            return False, "Zaten çalışan bir test var."

    params = params or {}
    main_py_path = _APP_ROOT / "main.py"
    cmd = [sys.executable, str(main_py_path)]
    if params.get("combo") is not None: cmd += ["--combo", str(params["combo"])]
    if params.get("scenario"):          cmd += ["--scenario", params["scenario"]]
    if params.get("dry_run"):           cmd += ["--dry-run"]
    if params.get("mode"):              cmd += ["--mode", params["mode"]]
    if params.get("spoof_sender"):      cmd += ["--spoof-sender", params["spoof_sender"]]
    if params.get("save_to_sent"):      cmd += ["--save-to-sent"]

    run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}{'_auto' if is_autonomous else ''}"
    run_state.update({
        "log_buffer": [],
        "running": True,
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "exit_code": None,
        "process": None
    })

    def _run_thread():
        try:
            env = os.environ.copy()
            env["MAIL_AUTO_APP_MODE"] = "1"
            env["MAIL_AUTO_RUN_ID"] = run_id
            proj_dir = str(_APP_ROOT)
            env["PYTHONPATH"] = proj_dir + os.pathsep + env.get("PYTHONPATH", "")
            
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", cwd=_APP_ROOT,
                                    env=env)
            run_state["process"] = proc
            for line in proc.stdout:
                run_state["log_buffer"].append(line.rstrip())
            proc.wait()
            run_state["exit_code"] = proc.returncode
        except Exception as e:
            run_state["log_buffer"].append(f"[HATA] {e}")
        finally:
            run_state.update({"running": False, "finished_at": datetime.now().isoformat()})
            run_state["log_buffer"].append("__DONE__")
            if is_autonomous:
                _check_and_notify(run_id, run_state["exit_code"])

    threading.Thread(target=_run_thread, daemon=True).start()
    return True, cmd

@app.route("/api/run/start", methods=["POST"])
def start_run():
    ok, result = _trigger_run(request.json)
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    return jsonify({"ok": True, "cmd": " ".join(result)})

# ── AUTOMATION LOGIC ───────────────────────────────────────────
def autonomous_run_wrapper():
    """Scheduler tarafından çağrılan sarmalayıcı."""
    logging.info("🤖 Otonom test döngüsü tetiklendi.")
    try:
        if not CONFIG_PATH.exists(): return
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if not config.get("automation", {}).get("enabled", False):
            return
        # Otonom test tüm senaryoları kapsar (params boşsa tümü çalışır)
        _trigger_run(is_autonomous=True)
    except Exception as e:
        logging.error(f"Otonom test hatası: {e}")

def _check_and_notify(run_id, exit_code):
    """Test bitiminde sonuçları kontrol eder ve gerekirse bildirim atar."""
    try:
        if not CONFIG_PATH.exists(): return
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        passed = (exit_code == 0)
        auto_cfg = config.get("automation", {})
        notif_cfg = auto_cfg.get("notifications", {})
        
        # Sadece hata durumunda bildirim ayarı kontrolü
        if not passed or not notif_cfg.get("notify_on_fail_only", True):
            summary = f"Otonom Test Tamamlandı: {'BAŞARILI' if passed else 'BAŞARISIZ'}"
            details = f"Run ID: {run_id}\nExit Code: {exit_code}\nBitiş: {datetime.now().strftime('%H:%M:%S')}"
            send_alert(summary, details, config)
    except Exception as e:
        logging.error(f"Bildirim gönderim hatası: {e}")

@app.route("/api/automation/sync", methods=["POST"])
def api_sync_scheduler():
    """Config güncellendiğinde zamanlayıcıyı tazeler."""
    sync_scheduler()
    return jsonify({"ok": True})

def sync_scheduler():
    """Zamanlayıcıyı config'e göre günceller."""
    scheduler.remove_all_jobs()
    try:
        if not CONFIG_PATH.exists(): return
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        auto = config.get("automation", {})
        if auto.get("enabled", False):
            interval = auto.get("schedule", {}).get("interval_minutes", 60)
            scheduler.add_job(autonomous_run_wrapper, 'interval', minutes=interval, id='auto_run')
            logging.info(f"📅 Scheduler aktif: Her {interval} dakikada bir.")
        else:
            logging.info("📅 Scheduler devre dışı (veya pasif).")
    except Exception as e:
        logging.error(f"Scheduler senkronizasyon hatası: {e}")

# İlk açılışta başlat
with app.app_context():
    sync_scheduler()

@app.route("/api/run/stop", methods=["POST"])
def stop_run():
    mfa_manager.cancel()
    if run_state.get("process"):
        run_state["process"].terminate()
    run_state.update({"running": False, "finished_at": datetime.now().isoformat()})
    return jsonify({"ok": True})

@app.route("/api/run/reset", methods=["POST"])
def reset_run():
    """Stuck run_state'i sıfırla (zombie process sonrası kılıç kullanın)."""
    proc = run_state.get("process")
    if proc and proc.poll() is None:
        proc.terminate()
    run_state.update({"running": False, "process": None, "exit_code": None,
                      "finished_at": datetime.now().isoformat(), "log_buffer": []})
    return jsonify({"ok": True, "msg": "Run state sıfırlandı."})

@app.route("/api/run/status", methods=["GET"])
def run_status():
    return jsonify({"running": run_state["running"], "started_at": run_state["started_at"],
                    "finished_at": run_state["finished_at"], "exit_code": run_state["exit_code"]})

@app.route("/api/run/logs")
def stream_logs():
    # Support resuming via Last-Event-ID header (browser auto-sends on reconnect)
    # or explicit ?offset= query param.
    last_id = request.headers.get("Last-Event-ID", "")
    try:
        offset = int(last_id) if last_id else request.args.get("offset", 0, type=int)
    except (ValueError, TypeError):
        offset = 0
    offset = max(0, offset)

    def generate():
        pos = offset
        last_ping = time.time()
        while True:
            buf = run_state["log_buffer"]
            # Drain any new lines added since last iteration
            while pos < len(buf):
                line = buf[pos]
                pos += 1
                if line == "__DONE__":
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return
                # id = pos lets client resume from here on reconnect
                yield f"id: {pos}\ndata: {json.dumps({'type': 'log', 'text': line})}\n\n"
            # No new lines yet — keepalive ping every 15 s
            now = time.time()
            if now - last_ping >= 15:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
                last_ping = now
            # If run finished and we've sent everything, close gracefully
            if not run_state["running"] and pos >= len(run_state["log_buffer"]):
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return
            time.sleep(0.3)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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
    p = REPORTS_DIR / filename
    return send_file(p) if p.exists() else (jsonify({"ok":False}), 404)

@app.route("/api/results/latest")
def latest_results():
    files = sorted(REPORTS_DIR.glob("*.csv"), key=os.path.getmtime, reverse=True)
    if not files: return jsonify({"ok":False,"error":"Henüz sonuç yok"})
    rows = []
    with open(files[0],"r",encoding="utf-8-sig") as f:
        for row in csv.DictReader(f): rows.append(dict(row))
    return jsonify({"ok":True,"results":rows,"file":files[0].name})

@app.route("/api/results/history")
def history_results():
    try:
        from database import get_dashboard_stats, get_recent_runs
        stats_data = get_dashboard_stats()
        recent_runs = get_recent_runs()
        return jsonify({"ok": True, "stats": stats_data, "runs": recent_runs})
    except ImportError:
        return jsonify({"ok": False, "error": "Database modülü bulunamadı"})

# ── TEMPLATES ───────────────────────────────────────────────────────
@app.route("/api/templates", methods=["GET"])
def get_templates():
    from template_manager import get_all_templates_for_api
    return jsonify({"ok": True, "data": get_all_templates_for_api()})

@app.route("/api/templates", methods=["POST"])
def post_templates():
    from template_manager import save_templates
    data = request.json or {}
    try:
        save_templates(data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/templates/export-defaults", methods=["POST"])
def export_default_templates():
    from template_manager import export_defaults_to_yaml
    try:
        export_defaults_to_yaml()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── ATTACHMENTS ─────────────────────────────────────────────────────
@app.route("/api/attachments", methods=["GET"])
def list_attachments():
    ATTACHMENTS_DIR.mkdir(exist_ok=True)
    files = []
    for f in sorted(ATTACHMENTS_DIR.iterdir()):
        if f.is_file():
            files.append({"name": f.name, "size": f.stat().st_size, "ext": f.suffix.lower()})
    return jsonify({"ok": True, "files": files})

@app.route("/api/attachments/upload", methods=["POST"])
def upload_attachment():
    ATTACHMENTS_DIR.mkdir(exist_ok=True)
    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify({"ok": False, "error": "Dosya seçilmedi"}), 400
    saved = []
    for f in uploaded:
        safe_name = secure_filename(f.filename)
        if not safe_name:
            continue
        dest = ATTACHMENTS_DIR / safe_name
        f.save(str(dest))
        saved.append(safe_name)
    return jsonify({"ok": True, "saved": saved})

@app.route('/api/attachments/<filename>', methods=['DELETE'])
def delete_attachment(filename):
    # get_config() Flask Response döndürür; doğrudan YAML oku
    cfg = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    att_dir = cfg.get('test', {}).get('test_attachment_path', 'attachments')
    if os.path.isfile(att_dir): att_dir = os.path.dirname(att_dir)
    path = os.path.join(att_dir, filename)
    if os.path.exists(path):
        os.remove(path)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Dosya bulunamadı"})

# ------------------------------------------------------------------ #
#  OAuth2 API (Sprint 7)
# ------------------------------------------------------------------ #

@app.route('/api/oauth/url', methods=['GET'])
def get_oauth_url():
    server = request.args.get('server') # outlook | gmail
    # get_config() bir Flask Response döndürür; doğrudan YAML oku
    config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    srv_cfg = config.get(server or '', {})
    oa_cfg = srv_cfg.get('oauth2', {}) if isinstance(srv_cfg, dict) else {}
    
    # Redirect URI: Tarayıcının eriştiği adres olmalı
    # Docker içinde localhost:5000 ama dışarıda localhost:5005 olabilir
    # request.host_url genellikle doğru dış adresi verir (proxy doğru ayarlandıysa)
    redirect_uri = f"{request.host_url.rstrip('/')}/api/oauth/callback"
    
    mgr = get_oauth_manager()
    if server == 'outlook':
        url = mgr.get_ms_auth_url(
            oa_cfg.get('client_id'), 
            oa_cfg.get('tenant_id', 'common'),
            redirect_uri=redirect_uri
        )
    elif server == 'gmail':
        url = mgr.get_google_auth_url(
            oa_cfg.get('client_id'),
            oa_cfg.get('client_secret'),
            redirect_uri=redirect_uri
        )
    else:
        return jsonify({"ok": False, "error": "Geçersiz sunucu"})
    
    return jsonify({"ok": True, "url": url})

@app.route('/api/oauth/callback', methods=['GET'])
def oauth_callback():
    code = request.args.get('code')
    
    # Şimdilik: Eğer code '4/' ile başlıyorsa Google, değilse Microsoft varsayalım (kaba yöntem)
    # Veya URL'e server parametresi eklemiştik (callback?server=outlook)
    server = request.args.get('server') or ('gmail' if 'scope' in request.args or 'authuser' in request.args else 'outlook')
    
    redirect_uri = f"{request.host_url.rstrip('/')}/api/oauth/callback"
    # get_config() Flask Response döndürür; doğrudan YAML oku
    config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    srv_cfg = config.get(server or '', {})
    oa_cfg = srv_cfg.get('oauth2', {}) if isinstance(srv_cfg, dict) else {}
    
    mgr = get_oauth_manager()
    success = False
    if server == 'outlook':
        success = mgr.complete_ms_auth(oa_cfg.get('client_id'), code, redirect_uri, oa_cfg.get('tenant_id', 'common'))
    elif server == 'gmail':
        success = mgr.complete_google_auth(oa_cfg.get('client_id'), oa_cfg.get('client_secret'), code, redirect_uri)
    
    if success:
        return """
        <html><body style="font-family:sans-serif; text-align:center; padding:50px;">
            <h1 style="color:#2ecc71;">✅ Yetkilendirme Başarılı!</h1>
            <p>Token başarıyla alındı ve kaydedildi. Bu sekmeyi kapatabilirsiniz.</p>
            <script>setTimeout(() => window.close(), 3000);</script>
        </body></html>
        """
    else:
        return """
        <html><body style="font-family:sans-serif; text-align:center; padding:50px;">
            <h1 style="color:#e74c3c;">❌ Yetkilendirme Hatası</h1>
            <p>Token alınırken bir sorun oluştu. Logları kontrol edin.</p>
        </body></html>
        """

@app.route('/api/oauth/status', methods=['GET'])
def get_oauth_status():
    mgr = get_oauth_manager()
    return jsonify({
        "outlook": "outlook" in mgr._tokens,
        "gmail": "gmail" in mgr._tokens
    })

# ── VISUAL REGRESSION (Sprint 8) ──────────────────────────────
@app.route("/api/visual/set-baseline", methods=["POST"])
def set_visual_baseline():
    """Mevcut bir test görselini o senaryo/kombinasyon için baseline olarak ata."""
    data = request.json or {}
    screenshot_path = data.get("screenshot_path")
    scenario_key = data.get("scenario_key")
    combo_label = data.get("combination_label", "")
    
    if not screenshot_path or not os.path.exists(screenshot_path):
        return jsonify({"ok": False, "error": "Geçerli screenshot yolu bulunamadı"}), 400
    
    try:
        baseline_dir = _CONFIG_DIR / "data" / "baselines"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        
        safe_label = combo_label.replace("/", "_").replace(" ", "_").replace("←", "_to_")
        baseline_file = baseline_dir / f"{scenario_key}_{safe_label}.png"
        
        # Kopyala (shutil)
        import shutil
        shutil.copy2(screenshot_path, str(baseline_file))
        return jsonify({"ok": True, "baseline_path": str(baseline_file)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/visual/diff-info", methods=["GET"])
def get_visual_diff():
    # reports/diffs içindeki dosyaları sunmak için statik serve yeterli (zaten send_file kullanılıyor)
    path = request.args.get("path")
    if not path or not os.path.exists(path):
        return jsonify({"ok": False, "error": "Dosya bulunamadı"}), 404
    return send_file(path)

@app.route("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    env_port = os.environ.get("PORT")
    desired_port = int(env_port) if env_port else 5000

    def _is_port_free(p: int) -> bool:
        # Flask `host="0.0.0.0"` binds on all interfaces; test bind on 0.0.0.0
        # (and IPv6 :: when available) to mirror the real server behavior.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s4:
                s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s4.bind(("0.0.0.0", p))
        except OSError:
            return False

        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s6:
                s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s6.bind(("::", p))
        except OSError:
            # If IPv6 is unavailable or already bound, treat as not free.
            return False

        return True

    port = int(os.environ.get("PORT", desired_port))
    if "PORT" not in os.environ:
        while port < desired_port + 20 and not _is_port_free(port):
            port += 1

    print(f"\n🚀 Mail Otomasyon Arayüzü: http://localhost:{port}\n")
    # Debug modunu kapatıyoruz çünkü arkaplanda (nohup/&) çalışırken 
    # reloader terminal etkileşimi bekleyip süreci durdurabiliyor (TN status).
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
