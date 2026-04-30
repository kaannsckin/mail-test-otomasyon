"""
app.py — Mail Otomasyon Web Arayüzü (Flask)
Çalıştır: python app.py  →  http://localhost:5000 (boşsa) / otomatik alternatif port
"""

import csv, json, logging, os, queue, socket, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template, request, send_file
from auth_manager import mfa_manager, generate_totp, totp_remaining_seconds

app = Flask(__name__)
_data_dir     = Path(os.environ.get("DATA_DIR", "."))
CONFIG_PATH   = _data_dir / os.environ.get("CONFIG_FILENAME", "config.yaml")
REPORTS_DIR   = _data_dir / "reports"
LOGS_DIR      = _data_dir / "logs"
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

# ── CONFIG ──────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def get_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return jsonify({"ok": True, "config": data})
    return jsonify({"ok": False, "error": "config.yaml bulunamadı"})

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
        if not sc:
            return jsonify({"ok": False, "error": f"'{server_key}' config'de tanımlı değil"})

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
@app.route("/api/run/start", methods=["POST"])
def start_run():
    if run_state["running"]:
        return jsonify({"ok": False, "error": "Zaten bir test çalışıyor"}), 400
    body      = request.json or {}
    cmd       = [sys.executable, "main.py"]
    if body.get("combo") is not None: cmd += ["--combo", str(body["combo"])]
    if body.get("scenario"):          cmd += ["--scenario", body["scenario"]]
    if body.get("dry_run"):           cmd += ["--dry-run"]
    run_state.update({"log_queue": queue.Queue(), "running": True,
                      "started_at": datetime.now().isoformat(),
                      "finished_at": None, "exit_code": None})
    def _run():
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", cwd=Path(__file__).parent)
            run_state["process"] = proc
            for line in proc.stdout: run_state["log_queue"].put(line.rstrip())
            proc.wait(); run_state["exit_code"] = proc.returncode
        except Exception as e:
            run_state["log_queue"].put(f"[HATA] {e}")
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
def stream_logs():
    def generate():
        while True:
            try:
                line = run_state["log_queue"].get(timeout=30)
                if line == "__DONE__":
                    yield f"data: {json.dumps({'type':'done'})}\n\n"; break
                yield f"data: {json.dumps({'type':'log','text':line})}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

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

@app.route("/api/reports/<filename>")
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

    port = desired_port
    if env_port is None:
        while port < desired_port + 20 and not _is_port_free(port):
            port += 1

    print(f"\n🚀 Mail Otomasyon Arayüzü: http://localhost:{port}\n")
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)
