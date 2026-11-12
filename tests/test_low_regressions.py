"""
test_low_regressions.py — 3. tur crash testinin düşük öncelikli bulguları.

Her test gerçekte yeniden üretilmiş bir hatayı sabitler; yorumlarda eski
(hatalı) davranış yazılıdır.
"""

import importlib.util
import json
import os
import smtplib
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_mod
import main as main_mod
import reporter
from analyzer import MailAnalyzer
from auth_manager import MFAManager
from sender import MailSender
from tests.test_orchestrator import (_analyzer_mock, _combo, _receiver_mock, _run_cli,
                                     _sender_mock, cli_env, test_cfg)  # noqa: F401

ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch):
    for name in ("UI_PASSWORD", "UI_USERNAME", "ALLOW_NO_AUTH"):
        monkeypatch.delenv(name, raising=False)
    getattr(app_mod, "_auth_failures", {}).clear()
    yield
    getattr(app_mod, "_auth_failures", {}).clear()


# ═══════════════════════════════════════════════════════════════════
#  Raporlar / sonuç dosyası
# ═══════════════════════════════════════════════════════════════════

class TestReportsEndpoints:

    @pytest.mark.parametrize("content", [b"a,b\n1,2,3\n", b"\xff\xfe,b\n1,2\n"])
    def test_latest_results_odd_csv_is_json(self, flask_client, content):
        # Eski: fazla sütunlu ya da UTF-8 olmayan CSV → HTML 500
        (app_mod.REPORTS_DIR / "garip.csv").write_bytes(content)
        r = flask_client.get("/api/results/latest")
        assert r.status_code == 200 and r.is_json
        assert r.get_json()["ok"] is True
        json.dumps(r.get_json())     # None anahtar kalmadı

    def test_latest_prefers_results_file(self, flask_client):
        # Eski: dizindeki en yeni HERHANGİ bir CSV seçiliyordu
        res = app_mod.REPORTS_DIR / "test_results.csv"
        res.write_text("Kombinasyon,Sonuç\nA,PASS\n", encoding="utf-8")
        other = app_mod.REPORTS_DIR / "baska.csv"
        other.write_text("x,y\n1,2\n", encoding="utf-8")
        os.utime(other, (time.time() + 60, time.time() + 60))
        data = flask_client.get("/api/results/latest").get_json()
        assert data["file"] == "test_results.csv"

    def test_dangling_symlink_skipped(self, flask_client):
        # Eski: kırık sembolik bağ /api/reports'u 500'e düşürüyordu
        (app_mod.REPORTS_DIR / "ok.html").write_text("<p>ok</p>")
        os.symlink("/nonexistent/x.html", app_mod.REPORTS_DIR / "kirik.html")
        r = flask_client.get("/api/reports")
        assert r.status_code == 200
        assert [f["name"] for f in r.get_json()["reports"]] == ["ok.html"]

    def test_symlink_outside_reports_not_served(self, flask_client, tmp_path):
        secret = tmp_path / "gizli.csv"
        secret.write_text("parola,123\n")
        os.symlink(secret, app_mod.REPORTS_DIR / "evil.csv")
        assert flask_client.get("/api/reports/evil.csv").status_code == 404
        names = [f["name"] for f in flask_client.get("/api/reports").get_json()["reports"]]
        assert "evil.csv" not in names


# ═══════════════════════════════════════════════════════════════════
#  Log yoklama
# ═══════════════════════════════════════════════════════════════════

class TestLogPolling:

    @pytest.fixture
    def log(self, tmp_path):
        f = tmp_path / "run.log"
        app_mod.run_state.update({"log_file": str(f), "running": False, "pump_done": True,
                                  "run_id": "r1"})
        yield f
        app_mod.run_state.update({"log_file": None, "running": False, "pump_done": True})

    def test_response_is_capped(self, flask_client, log):
        # Eski: tüm log (400k satır) tek yanıtta dönüyordu
        log.write_text("".join(f"satır {i}\n" for i in range(2500)), encoding="utf-8")
        first = flask_client.get("/api/run/logs?offset=0").get_json()
        assert len(first["lines"]) == app_mod.LOG_POLL_MAX_LINES
        assert first["next_offset"] == app_mod.LOG_POLL_MAX_LINES and first["done"] is False
        second = flask_client.get(f"/api/run/logs?offset={first['next_offset']}").get_json()
        assert second["lines"][0] == "satır 2000" and len(second["lines"]) == 500
        third = flask_client.get(f"/api/run/logs?offset={second['next_offset']}").get_json()
        assert third["lines"] == [] and third["done"] is True

    def test_stale_offset_resets(self, flask_client, log):
        # Eski: eski koşudan kalan büyük offset yeni koşuda hiç satır getirmiyordu
        log.write_text("a\nb\nc\n", encoding="utf-8")
        app_mod.run_state["running"] = True
        data = flask_client.get("/api/run/logs?offset=100000").get_json()
        assert data["reset"] is True
        assert data["lines"] == ["a", "b", "c"] and data["next_offset"] == 3
        assert data["run_id"] == "r1"

    def test_not_done_until_pump_drained(self, flask_client, log):
        # Durdur sonrası running=False olur ama okuyucu son satırları henüz
        # yazmamış olabilir; "bitti" denirse arayüz kalan satırları kaybeder.
        log.write_text("a\n", encoding="utf-8")
        app_mod.run_state["pump_done"] = False
        assert flask_client.get("/api/run/logs?offset=1").get_json()["done"] is False
        app_mod.run_state["pump_done"] = True
        assert flask_client.get("/api/run/logs?offset=1").get_json()["done"] is True


class TestRunLogFiles:

    @pytest.fixture(autouse=True)
    def _logs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_mod, "LOGS_DIR", tmp_path / "logs")
        monkeypatch.setattr(app_mod, "RUN_PID_FILE", tmp_path / "logs" / "run.pid")
        (tmp_path / "logs").mkdir()
        yield
        app_mod.run_state.update({"running": False, "process": None})

    def _wait_idle(self):
        end = time.time() + 5
        while time.time() < end and not (not app_mod.run_state["running"]
                                         and app_mod.run_state.get("pump_done")):
            time.sleep(0.02)

    def test_same_second_runs_get_distinct_files_and_no_queue(self, flask_client):
        # Eski: saniye çözünürlüklü ad → aynı saniyedeki iki koşu tek dosya
        files = []
        for text in ("birinci\n", "ikinci\n"):
            proc = MagicMock(stdout=iter([text]), returncode=0)
            with patch.object(app_mod.subprocess, "Popen", return_value=proc):
                assert flask_client.post("/api/run/start", json={}).get_json()["ok"]
                self._wait_idle()
            files.append(app_mod.run_state["log_file"])
        assert files[0] != files[1]
        assert Path(files[0]).read_text(encoding="utf-8") == "birinci\n"
        assert Path(files[1]).read_text(encoding="utf-8") == "ikinci\n"
        # Okunmayan, sınırsız büyüyen bellek kuyruğu kaldırıldı
        assert "log_queue" not in app_mod.run_state or app_mod.run_state["log_queue"].empty()


# ═══════════════════════════════════════════════════════════════════
#  MFA köprüsü — bozuk challenge.json
# ═══════════════════════════════════════════════════════════════════

class TestCorruptMfaBridge:

    @pytest.mark.parametrize("content", ['{"created_at": "x"}', '{"created_at": null}',
                                         "null", "[1, 2]", '"metin"', "{bozuk", "\xff"])
    def test_bridge_garbage_is_no_challenge(self, tmp_path, content):
        m = MFAManager(bridge_dir=tmp_path)
        (tmp_path / "challenge.json").write_text(content, encoding="latin-1")
        assert m.get_pending() is None
        assert m.submit_code("123456") is False
        m.cancel()

    def test_endpoints_do_not_500(self, flask_client, tmp_path, monkeypatch):
        # Eski: mfa/status|submit|cancel ve run/stop 500 veriyordu (süreç öldürülmüyordu)
        m = MFAManager(bridge_dir=tmp_path)
        monkeypatch.setattr(app_mod, "mfa_manager", m)
        (tmp_path / "challenge.json").write_text('{"created_at": "x"}', encoding="utf-8")
        assert flask_client.get("/api/mfa/status").get_json()["pending"] is False
        assert flask_client.post("/api/mfa/submit", json={"code": "123456"}).status_code == 200
        assert flask_client.post("/api/mfa/cancel").status_code == 200
        assert flask_client.post("/api/run/stop").status_code == 200


# ═══════════════════════════════════════════════════════════════════
#  Güvenlik başlıkları ve Basic Auth deneme sınırı
# ═══════════════════════════════════════════════════════════════════

def _basic(user, pw):
    import base64
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


class TestSecurityHardening:

    def test_frame_protection_headers(self, flask_client):
        # Eski: X-Frame-Options / CSP yoktu → çerçeveleme ile CSRF kapısı atlatılabilirdi
        r = flask_client.get("/")
        assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_bruteforce_is_throttled(self, flask_client, monkeypatch):
        # Eski: 200 yanlış deneme, hiçbir yavaşlama/kilit yok
        monkeypatch.setenv("UI_PASSWORD", "dogru")
        for i in range(app_mod.AUTH_MAX_FAILURES):
            assert flask_client.get("/api/config", headers=_basic("admin", f"yanlis{i}")).status_code == 401
        r = flask_client.get("/api/config", headers=_basic("admin", "dogru"))
        assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
        assert flask_client.get("/api/health").status_code == 200

    def test_window_expires(self, flask_client, monkeypatch):
        monkeypatch.setenv("UI_PASSWORD", "dogru")
        for i in range(app_mod.AUTH_MAX_FAILURES):
            flask_client.get("/api/config", headers=_basic("admin", "x"))
        later = time.time() + app_mod.AUTH_WINDOW_SECONDS + 1
        monkeypatch.setattr(app_mod.time, "time", lambda: later)
        assert flask_client.get("/api/config", headers=_basic("admin", "dogru")).status_code == 200

    def test_unauthenticated_first_request_not_counted(self, flask_client, monkeypatch):
        monkeypatch.setenv("UI_PASSWORD", "dogru")
        for _ in range(app_mod.AUTH_MAX_FAILURES + 5):     # tarayıcının kimliksiz ilk istekleri
            assert flask_client.get("/").status_code == 401
        assert flask_client.get("/", headers=_basic("admin", "dogru")).status_code == 200

    def test_spoofed_forwarded_for_does_not_reset_counter(self, flask_client, monkeypatch):
        # PaaS'ta en sağdaki (proxy'nin eklediği) adres kullanılır; istemcinin
        # yazdığı soldaki değeri değiştirmek sayacı sıfırlamaz.
        monkeypatch.setenv("UI_PASSWORD", "dogru")
        monkeypatch.setenv("RENDER", "true")
        for i in range(app_mod.AUTH_MAX_FAILURES):
            flask_client.get("/api/config", headers={**_basic("admin", "x"),
                                                     "X-Forwarded-For": f"10.0.0.{i}, 198.51.100.7"})
        r = flask_client.get("/api/config", headers={**_basic("admin", "dogru"),
                                                     "X-Forwarded-For": "1.2.3.4, 198.51.100.7"})
        assert r.status_code == 429


# ═══════════════════════════════════════════════════════════════════
#  /api/combinations — yanlış csv_input
# ═══════════════════════════════════════════════════════════════════

class TestCsvInputWarning:

    def _cfg(self, csv_input):
        app_mod.CONFIG_PATH.write_text(yaml.dump({"test": {"csv_input": csv_input}}),
                                       encoding="utf-8")

    def test_missing_configured_csv_warns(self, flask_client):
        # Eski: sessizce varsayılan checklist'e düşülüyordu
        self._cfg("yok_boyle_bir.csv")
        data = flask_client.get("/api/combinations").get_json()
        assert data["ok"] is True and data["combinations"]
        assert "yok_boyle_bir.csv" in data["warning"]

    def test_csv_without_combinations_warns(self, flask_client, tmp_path):
        empty = tmp_path / "bos.csv"
        empty.write_text("a,b\n1,2\n", encoding="utf-8")
        self._cfg(str(empty))
        data = flask_client.get("/api/combinations").get_json()
        assert data["combinations"] == [] and "kombinasyonu bulunamadı" in data["warning"]

    def test_valid_csv_no_warning(self, flask_client):
        self._cfg(str(ROOT / "mail_test_checklist.csv"))
        assert "warning" not in flask_client.get("/api/combinations").get_json()


# ═══════════════════════════════════════════════════════════════════
#  YAML alias: CLI ve arayüz aynı kuralı uygular
# ═══════════════════════════════════════════════════════════════════

class TestAliasConsistency:

    def test_cli_rejects_alias_with_clear_message(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("base: &b {smtp_host: h}\nems: *b\n", encoding="utf-8")
        with pytest.raises(ValueError, match="alias"):
            main_mod.load_config(str(p))

    def test_cli_plain_config_still_loads(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("ems: {smtp_host: h}\ntest:\n", encoding="utf-8")
        assert main_mod.load_config(str(p)) == {"ems": {"smtp_host": "h"}, "test": {}}


# ═══════════════════════════════════════════════════════════════════
#  Bağlantı testi toplam süre sınırı
# ═══════════════════════════════════════════════════════════════════

class _DripServer:
    """Her 0.2 sn'de bir '220-' satırı damlatan, banner'ı hiç bitirmeyen sunucu."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        try:
            while not self.stop.is_set():
                conn.sendall(b"220-bekleyin\r\n")
                time.sleep(0.2)
        except OSError:
            pass
        finally:
            conn.close()
            self.sock.close()


class TestConnectionDeadline:

    def test_drip_server_cut_off_at_deadline(self, flask_client, monkeypatch):
        # Eski: soket timeout'u yalnız tek okumayı sınırladığı için istek 150 sn+ sürdü
        monkeypatch.setattr(app_mod, "TEST_CONNECTION_DEADLINE", 1.0)
        srv = _DripServer()
        start = time.time()
        try:
            r = flask_client.post("/api/config/test-connection", json={
                "server": "ems", "server_config": {
                    "smtp_host": "127.0.0.1", "smtp_port": srv.port, "smtp_use_tls": False,
                    "username": "u", "password": "p"}})
        finally:
            srv.stop.set()
        elapsed = time.time() - start
        data = r.get_json()
        assert elapsed < 5
        assert data["ok"] is False and "sn içinde" in data["error"]

    def test_deadline_cancelled_on_success(self):
        from mail_transport import Deadline
        d = Deadline(0.2)
        d.close()
        time.sleep(0.3)
        assert d.expired is False

    def test_success_reports_actual_tls_mode(self, flask_client):
        # Eski: düz SMTP / tls_verify:false için de "Bağlantı: Güvenli" gösteriliyordu
        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp):
            data = flask_client.post("/api/config/test-connection", json={
                "server": "ems", "server_config": {
                    "smtp_host": "h", "smtp_port": 25, "smtp_use_tls": False,
                    "tls_verify": False, "username": "u", "password": "p"}}).get_json()
        assert data["ok"] is True
        assert data["security"] == {"smtp_mode": "plain", "imap_mode": None, "tls_verify": False}


# ═══════════════════════════════════════════════════════════════════
#  Koşu çıkış kodu — her şey SKIP
# ═══════════════════════════════════════════════════════════════════

class TestAllSkipped:

    def test_all_skipped_run_is_not_success(self, cli_env, caplog):
        # Eski: "0/0 PASS", çıkış 0 — hiçbir şey test edilmeden başarılı
        skip = {"analysis": {"passed": None, "summary": "atlandı"}, "skipped": True}
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "run_scenario", return_value=skip), \
             patch.object(main_mod, "generate_html_report") as html, \
             patch.object(main_mod, "generate_csv_results"), \
             patch.object(main_mod.time, "sleep"), \
             caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
            _run_cli(["--config", str(cli_env), "--combo", "0"])
        assert exc.value.code == main_mod.EXIT_ERROR
        assert html.called                       # rapor yine yazılır
        assert "tümü atlandı" in caplog.text


# ═══════════════════════════════════════════════════════════════════
#  Rapor üretici — bozuk satır ve CSV enjeksiyonu
# ═══════════════════════════════════════════════════════════════════

GOOD = {"combination": "A", "scenario_type": "plain_text", "test_time": "t",
        "analysis": {"passed": True, "confidence": "HIGH", "summary": "ok",
                     "checks": [{"name": "c", "passed": True, "detail": "d"}],
                     "issues": [], "recommendations": []}}


class TestReporterTolerance:

    @pytest.mark.parametrize("bad", [
        {"combination": "A", "scenario_type": "x", "analysis": None},
        {"combination": "A", "scenario_type": "x", "analysis": {"passed": False, "checks": None}},
        {"combination": "A", "scenario_type": "x",
         "analysis": {"passed": False, "issues": [1, None, {"a": 1}], "recommendations": "tek"}},
        {"analysis": {"passed": True}},                                 # anahtar eksik
        {"combination": "A", "scenario_type": "x",
         "analysis": {"passed": False, "summary": "kırık \ud800 surrogate"}},
        "satır sözlük değil",
    ])
    def test_bad_row_does_not_kill_report(self, tmp_path, bad):
        # Eski: tek bozuk satır hem HTML hem CSV üretimini düşürüyordu
        html_p, csv_p = tmp_path / "r.html", tmp_path / "r.csv"
        reporter.generate_html_report([GOOD, bad], str(html_p))
        reporter.generate_csv_results([GOOD, bad], str(csv_p))
        assert html_p.read_text(encoding="utf-8").count("<tr") >= 2
        assert len(csv_p.read_text(encoding="utf-8-sig").strip().splitlines()) == 3

    def test_csv_formula_cells_neutralized(self, tmp_path):
        import csv as csv_mod
        row = {"combination": "=HYPERLINK(\"http://x\")", "scenario_type": "plain_text",
               "test_time": "t", "analysis": {"passed": False, "summary": "+cmd|calc",
                                              "issues": ["@SUM(1)"], "recommendations": ["-2+3"]}}
        p = tmp_path / "r.csv"
        reporter.generate_csv_results([row], str(p))
        with open(p, encoding="utf-8-sig", newline="") as f:
            data = list(csv_mod.reader(f))[1]
        assert data[0].startswith("'=") and data[4].startswith("'+")
        assert data[5].startswith("'@") and data[6].startswith("'-")
        assert data[2] == "FAIL"


# ═══════════════════════════════════════════════════════════════════
#  Scriptler
# ═══════════════════════════════════════════════════════════════════

def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestScripts:

    def test_extend_checklist_reads_excel_tr_csv(self, tmp_path):
        # Eski: cp1254 + ';' dosyada ham UnicodeDecodeError traceback'i
        mod = _load_script("extend_checklist")
        src = (ROOT / "mail_test_checklist.csv").read_text(encoding="utf-8-sig")
        lines = src.splitlines()[:40]
        tr = "\n".join(lines).replace(",", ";").replace("→", "->").replace("🔀", "")
        p = tmp_path / "excel.csv"
        p.write_bytes(tr.encode("cp1254", errors="replace"))
        preamble, combos = mod.parse(p)
        utf_p = tmp_path / "utf.csv"
        utf_p.write_text("\n".join(lines), encoding="utf-8")
        assert len(combos) == len(mod.parse(utf_p)[1])

    def test_e2e_smoke_missing_config_is_clean(self, tmp_path):
        # Eski: FileNotFoundError traceback
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "e2e_smoke.py"),
                            "--config", str(tmp_path / "yok.yaml"), "--to", "a@b.c"],
                           capture_output=True, text=True, cwd=tmp_path, timeout=60)
        assert r.returncode != 0
        assert "bulunamadı" in r.stderr + r.stdout
        assert "Traceback" not in r.stderr


# ═══════════════════════════════════════════════════════════════════
#  Analiz — boş API key
# ═══════════════════════════════════════════════════════════════════

class TestEmptyApiKey:

    def test_claude_empty_key_fails_fast(self, monkeypatch, received_msg, combination_meta):
        # Eski: her senaryo "bağlanılamadı" ile 3 boş API çağrısı yapıyordu
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        a = MailAnalyzer(api_key="  ")
        with patch.object(a, "_claude_request") as req:
            v = a.analyze("plain_text", {}, received_msg, combination_meta)
        req.assert_not_called()
        assert v["passed"] is False and "API key tanımlı değil" in v["summary"]

    def test_claude_empty_key_uses_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
        a = MailAnalyzer(api_key="")
        assert a._circuit_reason is None
        assert a.client.api_key == "sk-env"

    def test_gemini_empty_key_fails_fast(self, received_msg, combination_meta):
        a = MailAnalyzer(api_key="", provider="gemini")
        with patch("analyzer.httpx.post") as post:
            v = a.analyze("plain_text", {}, received_msg, combination_meta)
        post.assert_not_called()
        assert "Gemini API key tanımlı değil" in v["summary"]


# ═══════════════════════════════════════════════════════════════════
#  SMTP adresleri: ASCII dışı ve virgüllü alıcı
# ═══════════════════════════════════════════════════════════════════

class _CmdServer:
    """Komutları kaydeden sahte SMTP sunucusu."""

    def __init__(self, extensions=("AUTH PLAIN",)):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.ext = extensions
        self.cmds, self.data = [], b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.sock.accept()
        f = conn.makefile("rb")
        conn.sendall(b"220 test\r\n")
        in_data = False
        while True:
            line = f.readline()
            if not line:
                break
            if in_data:
                if line.rstrip(b"\r\n") == b".":
                    in_data = False
                    conn.sendall(b"250 ok\r\n")
                else:
                    self.data += line
                continue
            self.cmds.append(line.rstrip(b"\r\n"))
            u = line.upper()
            if u.startswith(b"EHLO"):
                lines = [b"test"] + [e.encode() for e in self.ext]
                conn.sendall(b"".join(b"250-" + l + b"\r\n" for l in lines[:-1])
                             + b"250 " + lines[-1] + b"\r\n")
            elif u.startswith(b"AUTH"):
                conn.sendall(b"235 ok\r\n")
            elif u.startswith(b"DATA"):
                in_data = True
                conn.sendall(b"354 go\r\n")
            elif u.startswith(b"QUIT"):
                conn.sendall(b"221 bye\r\n")
                break
            else:
                conn.sendall(b"250 ok\r\n")
        conn.close()
        self.sock.close()


def _sender(port, from_addr="a@test.local"):
    return MailSender({"smtp_host": "127.0.0.1", "smtp_port": port, "smtp_use_tls": False,
                       "username": "u", "password": "p", "test_address": from_addr})


class TestSmtpAddresses:

    def test_non_ascii_recipient_uses_smtputf8(self):
        # Eski: UnicodeEncodeError('ascii' codec) ham dönüyordu
        srv = _CmdServer(("AUTH PLAIN", "SMTPUTF8"))
        meta = _sender(srv.port).send_plain_text("ğüş@örnek.com", "Konu", "gövde")
        srv.thread.join(5)
        assert meta["sent_at"]
        mail_from = next(c for c in srv.cmds if c.upper().startswith(b"MAIL FROM"))
        assert b"SMTPUTF8" in mail_from.upper()
        assert any("ğüş@örnek.com".encode() in c for c in srv.cmds)
        assert "To: ğüş@örnek.com".encode() in srv.data

    def test_non_ascii_without_server_support_is_clear(self):
        srv = _CmdServer(("AUTH PLAIN",))
        with pytest.raises(smtplib.SMTPNotSupportedError, match="SMTPUTF8"):
            _sender(srv.port).send_plain_text("ğüş@örnek.com", "Konu", "gövde")

    def test_comma_separated_recipients_split(self):
        # Eski: "a@x,b@y" tek RCPT olarak gidip 501 alıyordu
        srv = _CmdServer()
        _sender(srv.port).send_plain_text("x@test.local, y@test.local", "Konu", "gövde")
        srv.thread.join(5)
        rcpts = [c.upper() for c in srv.cmds if c.upper().startswith(b"RCPT TO")]
        assert rcpts == [b"RCPT TO:<X@TEST.LOCAL>", b"RCPT TO:<Y@TEST.LOCAL>"]


# ═══════════════════════════════════════════════════════════════════
#  Orkestratör küçükleri
# ═══════════════════════════════════════════════════════════════════

class TestOrchestratorLow:

    def test_slow_data_timeout_not_called_blocked_port(self):
        # Eski: bağlantı kurulmuşken DATA zaman aşımı "port engelli" diye raporlanıyordu
        try:
            try:
                raise TimeoutError("timed out")
            except TimeoutError as e:
                raise smtplib.SMTPServerDisconnected(
                    "Connection unexpectedly closed: timed out") from e
        except smtplib.SMTPServerDisconnected as exc:
            assert main_mod._is_network_unreachable(exc) is False
        assert main_mod._is_network_unreachable(TimeoutError("timed out")) is True

    def test_missing_configured_attachment_fails(self, test_cfg):
        # Eski: varsayılan test_document.pdf ile sessizce değiştirilip PASS oluyordu
        cfg = {**test_cfg, "test_attachment_path": "test_files/yok.pdf"}
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        res = main_mod.run_scenario("attachment", _combo(), 0, sender, receiver, analyzer, cfg)
        sender.send_with_attachment.assert_not_called()
        assert res["analysis"]["passed"] is False
        assert "yok.pdf" in res["analysis"]["summary"]

    def test_unconfigured_attachment_still_uses_default(self, test_cfg):
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        main_mod.run_scenario("attachment", _combo(), 0, sender, receiver, analyzer, test_cfg)
        sender.send_with_attachment.assert_called_once()

    def test_negative_wait_clamped(self, test_cfg):
        cfg = {**test_cfg, "wait_seconds": -5, "retry_interval": -1}
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, cfg)
        kw = receiver.wait_for_message.call_args.kwargs
        assert kw["wait_seconds"] == 0 and kw["retry_interval"] == 0


class TestCsvParserWarnings:

    def test_short_row_and_unknown_scenario_logged(self, tmp_path, caplog):
        from csv_parser import parse_csv
        p = tmp_path / "c.csv"
        p.write_text(
            "🔀 Kombinasyon 1: Alan EMS (iOS) ← Gönderen Gmail (Android),,,,,,,\n"
            "1,Sadece İçerik (Plain Text),EMS,iOS,Gmail,Android,adım,\n"
            "2,Sadece İcerik (Plain Text),EMS,iOS,Gmail,Android,adım,\n"
            "3,kısa,satır\n", encoding="utf-8")
        with caplog.at_level("WARNING"):
            combos = parse_csv(str(p))
        assert combos
        assert "#3" in caplog.text and "en az 7" in caplog.text
        assert "tanınmayan senaryo tipi" in caplog.text
