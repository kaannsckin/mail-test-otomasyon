"""
test_round4_regressions.py — 4. tur crash testinin bulguları.

Her test gerçekte yeniden üretilmiş bir hatayı sabitler; yorumlarda eski
(hatalı) davranış yazılıdır.
"""

import base64
import email
import io
import json
import os
import shutil
import signal
import smtplib
import socket
import ssl
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
import mail_transport
import sender as sender_mod
from analyzer import MailAnalyzer
from auth_manager import MFAManager
from mail_transport import Deadline, smtp_connect
from receiver import MailReceiver
from sender import MailSender, format_address_header, parse_addresses
from tests.test_orchestrator import (_analyzer_mock, _combo, _receiver_mock, _run_cli,
                                     _sender_mock, cli_env, test_cfg)  # noqa: F401

ROOT = Path(__file__).parent.parent
HAS_OPENSSL = shutil.which("openssl") is not None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("UI_PASSWORD", "UI_USERNAME", "ALLOW_NO_AUTH", "TRUST_PROXY", "VERCEL",
                 "RENDER", "RAILWAY_ENVIRONMENT", "DYNO", "FLY_APP_NAME", "K_SERVICE"):
        monkeypatch.delenv(name, raising=False)
    app_mod._auth_failures.clear()
    yield
    app_mod._auth_failures.clear()


def _saved():
    return yaml.safe_load(app_mod.CONFIG_PATH.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════
#  Sahte SMTP sunucusu (komut ve DATA kaydı)
# ═══════════════════════════════════════════════════════════════════

class FakeSMTP:
    def __init__(self, ext=("AUTH PLAIN",), refuse=()):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.ext, self.refuse = ext, refuse
        self.cmds, self.data = [], b""
        self.done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        f = conn.makefile("rb")
        conn.sendall(b"220 t\r\n")
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
                ls = [b"t"] + [e.encode() for e in self.ext]
                conn.sendall(b"".join(b"250-" + x + b"\r\n" for x in ls[:-1]) + b"250 " + ls[-1] + b"\r\n")
            elif u.startswith(b"AUTH"):
                conn.sendall(b"235 ok\r\n")
            elif u.startswith(b"RCPT") and any(r.encode() in line for r in self.refuse):
                conn.sendall(b"550 hayir\r\n")
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
        self.done.set()

    def rcpts(self):
        return [c.split(b":", 1)[1].strip().decode() for c in self.cmds if c.upper().startswith(b"RCPT TO")]

    def mail_from(self):
        return next(c for c in self.cmds if c.upper().startswith(b"MAIL FROM")).decode()


def _sender(port, frm="a@test.local", user="u"):
    return MailSender({"smtp_host": "127.0.0.1", "smtp_port": port, "smtp_use_tls": False,
                       "username": user, "password": "p" if user else "", "test_address": frm})


# ═══════════════════════════════════════════════════════════════════
#  WEB / API
# ═══════════════════════════════════════════════════════════════════

class TestMfaBridgeRace:

    def test_parallel_submits_never_500(self, flask_client, tmp_path, monkeypatch):
        # Eski: ortak response.tmp → 60 paralel submit'te 30 adet 500
        m = MFAManager(bridge_dir=tmp_path)
        monkeypatch.setattr(app_mod, "mfa_manager", m)
        (tmp_path / "challenge.json").write_text(json.dumps({"created_at": time.time()}))
        codes = []

        def go(i):
            with app_mod.app.test_client() as c:
                url = "/api/mfa/submit" if i % 3 else "/api/mfa/cancel"
                codes.append(c.post(url, json={"code": "123456"}).status_code)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(60)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert codes == [200] * 60
        assert not list(tmp_path.glob("*.tmp"))


class TestReportPathNul:

    @pytest.mark.parametrize("path", ["%00", "a.html%00.csv"])
    def test_nul_byte_is_404(self, flask_client, path):
        # Eski: sembolik bağ düzeltmesi sonrası ValueError → HTML 500
        r = flask_client.get(f"/api/reports/{path}")
        assert r.status_code == 404 and r.is_json


class TestConfigValueSafety:

    def test_invalid_date_value_is_read_as_text(self, flask_client):
        # Eski: "2026-02-30" ValueError → tüm config uç noktaları 500, kayıt onarmıyordu
        app_mod.CONFIG_PATH.write_text("test:\n  subject_prefix: 2026-02-30\n  d: 2026-01-05\n",
                                       encoding="utf-8")
        data = flask_client.get("/api/config").get_json()
        assert data["ok"] is True
        assert data["config"]["test"] == {"subject_prefix": "2026-02-30", "d": "2026-01-05"}
        assert flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "h"}}}).status_code == 200

    @pytest.mark.parametrize("value", [1e999, float("-inf")])
    def test_non_finite_number_rejected_on_save(self, flask_client, value):
        # Eski: kaydediliyor, sonra /api/config "Infinity" içeren geçersiz JSON döndürüyordu
        body = '{"config": {"test": {"wait_seconds": %s}}}' % ("1e999" if value > 0 else "-1e999")
        r = flask_client.post("/api/config", data=body, content_type="application/json")
        assert r.status_code == 400 and "sonlu" in r.get_json()["error"]
        assert not app_mod.CONFIG_PATH.exists()

    @pytest.mark.parametrize("yaml_text", [b"ems:\n  x: !!set {a: null}\n",
                                           b"ems:\n  x: !!binary aGVsbG8=\n",
                                           b"ems:\n  x: .inf\n",
                                           b"ems:\n  x: " + b"9" * 5000 + b"\n"])
    def test_import_rejects_unsafe_values_without_writing(self, flask_client, yaml_text):
        # Eski: !!set import'u dosyayı yazıp 500 veriyor, sonraki her GET 500 oluyordu
        r = flask_client.post("/api/config/import", content_type="multipart/form-data",
                              data={"file": (io.BytesIO(yaml_text), "c.yaml")})
        assert r.status_code == 400 and r.is_json
        assert not app_mod.CONFIG_PATH.exists()

    def test_unsafe_file_on_disk_is_json_error_and_repairable(self, flask_client):
        app_mod.CONFIG_PATH.write_text("anthropic:\n  api_key: sk-REAL\ntest:\n  w: .inf\n",
                                       encoding="utf-8")
        r = flask_client.get("/api/config")
        assert r.is_json and r.get_json()["ok"] is False
        json.loads(r.get_data(as_text=True))           # geçerli JSON (Infinity yok)
        assert flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "h"}}}).status_code == 200
        saved = _saved()
        assert saved["anthropic"]["api_key"] == "sk-REAL"   # geçerli bölüm kurtarıldı
        assert "test" not in saved                          # bozuk bölüm yalnızca yedekte

    def test_config_path_is_directory(self, flask_client):
        # Eski: HTML 500
        app_mod.CONFIG_PATH.mkdir()
        for r in (flask_client.get("/api/config"), flask_client.get("/api/config/export")):
            assert r.is_json and r.get_json()["ok"] is False


class TestConfigBackups:

    ALIAS = ("anthropic:\n  api_key: sk-REAL\nbase: &b {smtp_host: h, password: GIZLI}\n"
             "ems: *b\nbozuk: [kapanmamis\n")

    def test_alias_file_sections_salvaged(self, flask_client):
        # Eski: alias'lı dosyada hiçbir bölüm kurtarılmıyor, API key canlı config'ten düşüyordu
        app_mod.CONFIG_PATH.write_text("anthropic:\n  api_key: sk-REAL\nbase: &b {smtp_host: h, "
                                       "password: GIZLI}\nems: *b\n", encoding="utf-8")
        r = flask_client.post("/api/config", json={"config": {"gmail": {"smtp_host": "g"}}})
        assert r.status_code == 200 and "yedeği" in r.get_json()["warning"]
        saved = _saved()
        assert saved["anthropic"]["api_key"] == "sk-REAL"
        assert saved["ems"] == {"smtp_host": "h", "password": "GIZLI"}
        assert "*" not in app_mod.CONFIG_PATH.read_text(encoding="utf-8")

    def test_alias_bomb_not_expanded(self, flask_client):
        bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
        for i in range(1, 9):
            bomb += f"{chr(97 + i)}: &{chr(97 + i)} [" + ", ".join([f"*{chr(96 + i)}"] * 10) + "]\n"
        app_mod.CONFIG_PATH.write_text("anthropic:\n  api_key: k\n" + bomb, encoding="utf-8")
        r = flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "h"}}})
        assert r.status_code == 200
        assert app_mod.CONFIG_PATH.stat().st_size < 10_000

    def test_backups_private_deduplicated_and_bounded(self, flask_client, monkeypatch):
        # Eski: 0644 izinli, her kayıtta yeni yedek (sınırsız)
        for i in range(8):
            app_mod.CONFIG_PATH.write_text(f"ems: bozuk-{i}\n", encoding="utf-8")
            flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "h"}}})
        app_mod.CONFIG_PATH.write_text("ems: ayni\n", encoding="utf-8")
        monkeypatch.setattr(app_mod, "_write_config", MagicMock(side_effect=OSError("disk")))
        for _ in range(3):                      # aynı bozuk içerik tekrar yedeklenmez
            flask_client.post("/api/config", json={"config": {"ems": {"smtp_host": "h"}}})
        backups = list(app_mod.CONFIG_PATH.parent.glob("config.yaml.bozuk-*.bak"))
        assert len(backups) == app_mod._MAX_BACKUPS
        assert all((b.stat().st_mode & 0o777) == 0o600 for b in backups)
        assert sum(b.read_text(encoding="utf-8") == "ems: ayni\n" for b in backups) == 1

    def test_config_file_is_private(self, flask_client):
        flask_client.post("/api/config", json={"config": {"ems": {"password": "x"}}})
        assert (app_mod.CONFIG_PATH.stat().st_mode & 0o777) == 0o600

    @pytest.mark.parametrize("name", ["config.yaml.bozuk-20260101-000000-abc123.bak",
                                      "config.yaml.0123abcd.tmp"])
    def test_secret_files_ignored_by_git_and_docker(self, name):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT)
        assert r.returncode == 0
        dockerignore = (ROOT / ".dockerignore").read_text()
        assert "config.yaml.bozuk-*.bak" in dockerignore and "config.yaml.*.tmp" in dockerignore


def _basic(pw, user="admin"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


class TestAuthThrottleFairness:

    def _lock_out(self, client, addr="172.17.0.1", headers=None):
        for _ in range(app_mod.AUTH_MAX_FAILURES):
            client.get("/api/config", headers={**_basic("yanlis"), **(headers or {})},
                       environ_overrides={"REMOTE_ADDR": addr})

    def test_trusted_owner_not_locked_out_by_shared_address(self, flask_client, monkeypatch):
        # Eski: proxy/docker arkasında 10 yanlış deneme gerçek kullanıcıyı da 429'a düşürüyordu
        monkeypatch.setenv("UI_PASSWORD", "dogru")
        owner = app_mod.app.test_client()
        r = owner.get("/api/config", headers=_basic("dogru"), environ_overrides={"REMOTE_ADDR": "172.17.0.1"})
        assert r.status_code == 200 and app_mod.TRUSTED_COOKIE in r.headers.get("Set-Cookie", "")
        self._lock_out(flask_client)
        assert flask_client.get("/api/config", headers=_basic("dogru"),
                                environ_overrides={"REMOTE_ADDR": "172.17.0.1"}).status_code == 429
        assert owner.get("/api/config", headers=_basic("dogru"),
                         environ_overrides={"REMOTE_ADDR": "172.17.0.1"}).status_code == 200

    def test_trusted_cookie_does_not_skip_password(self, flask_client, monkeypatch):
        monkeypatch.setenv("UI_PASSWORD", "dogru")
        flask_client.get("/", headers=_basic("dogru"))
        assert flask_client.get("/api/config", headers=_basic("yanlis")).status_code == 401

    def test_forged_cookie_rejected(self, flask_client, monkeypatch):
        monkeypatch.setenv("UI_PASSWORD", "dogru")
        self._lock_out(flask_client, addr="127.0.0.1")
        flask_client.set_cookie(app_mod.TRUSTED_COOKIE, "0" * 64)
        assert flask_client.get("/api/config", headers=_basic("dogru")).status_code == 429

    def test_cookie_invalid_after_password_change(self, flask_client, monkeypatch):
        monkeypatch.setenv("UI_PASSWORD", "eski")
        flask_client.get("/", headers=_basic("eski"))
        monkeypatch.setenv("UI_PASSWORD", "yeni")
        self._lock_out(flask_client, addr="127.0.0.1")
        assert flask_client.get("/api/config", headers=_basic("yeni")).status_code == 429

    @pytest.mark.parametrize("env", [{"TRUST_PROXY": "1"}, {"VERCEL": "1"}])
    def test_proxy_clients_counted_separately(self, flask_client, monkeypatch, env):
        monkeypatch.setenv("UI_PASSWORD", "dogru")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        self._lock_out(flask_client, headers={"X-Forwarded-For": "198.51.100.7"})
        other = flask_client.get("/api/config", headers={**_basic("dogru"),
                                                        "X-Forwarded-For": "203.0.113.9"},
                                 environ_overrides={"REMOTE_ADDR": "172.17.0.1"})
        assert other.status_code == 200

    def test_table_overflow_keeps_active_attacker_blocked(self, monkeypatch):
        # Eski: 10000 kayıt aşılınca tablo tamamen siliniyor, saldırganın kilidi açılıyordu
        now = time.time()
        monkeypatch.setattr(app_mod, "AUTH_MAX_KEYS", 50)
        for _ in range(app_mod.AUTH_MAX_FAILURES):
            app_mod._note_auth_failure("saldirgan", now)
        for i in range(200):
            app_mod._note_auth_failure(f"10.0.{i // 250}.{i % 250}", now - 100 + i * 0.01)
        assert app_mod._auth_blocked("saldirgan", now + 1) > 0
        assert len(app_mod._auth_failures) <= 50


class _BlackholeTLS:
    """Bağlantıyı kabul eden ama TLS el sıkışmasına hiç yanıt vermeyen sunucu."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
                self.conns.append(c)
            except OSError:
                return

    def close(self):
        for c in self.conns:
            c.close()
        self.sock.close()


class TestDeadlineCoverage:

    def test_tls_handshake_is_bounded(self):
        # Eski: süre sınırı TLS el sıkışmasından SONRA başlıyordu (15 sn soket timeout)
        srv = _BlackholeTLS()
        d = Deadline(1.0)
        start = time.time()
        try:
            with pytest.raises((OSError, ssl.SSLError)):
                smtp_connect({"smtp_host": "127.0.0.1", "smtp_port": srv.port,
                              "smtp_use_ssl": True}, timeout=15, deadline=d)
        finally:
            d.close()
            srv.close()
        assert time.time() - start < 5

    def test_dns_is_bounded(self, monkeypatch):
        real = socket.getaddrinfo
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda *a, **k: (time.sleep(5), real(*a, **k))[1])
        d = Deadline(0.5)
        start = time.time()
        with pytest.raises(TimeoutError):
            d.connect(("127.0.0.1", 9), timeout=15)
        d.close()
        assert time.time() - start < 2

    def test_many_dead_addresses_bounded(self, monkeypatch):
        # 8 ölü A kaydı × 2 sn connect → eskiden 16 sn
        monkeypatch.setattr(mail_transport.Deadline, "resolve", lambda self, h, p: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.255.255.1", 25))] * 8)
        d = Deadline(1.0)
        start = time.time()
        with pytest.raises(OSError):
            d.connect(("dead.example", 25), timeout=2)
        d.close()
        assert time.time() - start < 3


class TestImapSecurityBadge:

    def test_plain_imap_reported(self, flask_client):
        # Eski: rozet yalnızca SMTP'ye bakıyor, şifresiz IMAP'te de "Güvenli" diyordu
        imap = MagicMock()
        imap.select.return_value = ("OK", [b"1"])
        with patch("smtplib.SMTP", return_value=MagicMock()), \
             patch("imaplib.IMAP4", return_value=imap):
            data = flask_client.post("/api/config/test-connection", json={
                "server": "ems", "server_config": {
                    "smtp_host": "h", "smtp_port": 587, "imap_host": "h", "imap_port": 143,
                    "imap_use_ssl": False, "username": "u", "password": "p"}}).get_json()
        assert data["ok"] is True
        assert data["security"]["imap_mode"] == "plain"


class TestLogPumpWatchdog:

    @pytest.fixture(autouse=True)
    def _logs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_mod, "LOGS_DIR", tmp_path / "logs")
        monkeypatch.setattr(app_mod, "RUN_PID_FILE", tmp_path / "logs" / "run.pid")
        monkeypatch.setattr(app_mod, "PUMP_GRACE", 0.5)
        (tmp_path / "logs").mkdir()
        yield
        app_mod.run_state.update({"running": False, "process": None, "pump_done": True})

    def test_pipe_held_open_does_not_hang_done(self, flask_client):
        # Eski: boru alt süreçte açık kalınca pump_done hiç True olmuyor, done=false sonsuz
        gate = threading.Event()

        def stdout():
            yield "satır\n"
            gate.wait(30)
        proc = MagicMock(stdout=stdout(), returncode=0)
        proc.wait.return_value = 0
        with patch.object(app_mod.subprocess, "Popen", return_value=proc):
            assert flask_client.post("/api/run/start", json={}).get_json()["ok"]
        try:
            end = time.time() + 5
            done = False
            while time.time() < end and not done:
                data = flask_client.get("/api/run/logs?offset=0").get_json()
                nxt = flask_client.get(f"/api/run/logs?offset={data['next_offset']}").get_json()
                done = nxt["done"]
                time.sleep(0.1)
            assert done
            assert "borusu açık" in Path(app_mod.run_state["log_file"]).read_text(encoding="utf-8")
        finally:
            gate.set()

    @pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX")
    def test_kill_tree_kills_grandchildren(self, tmp_path):
        script = tmp_path / "p.py"
        script.write_text("import subprocess,sys,time\n"
                          "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                          "print('x', flush=True)\ntime.sleep(60)\n")
        proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                                text=True, start_new_session=True)
        assert proc.stdout.readline().strip() == "x"
        app_mod._kill_tree(proc, grace=2)
        assert proc.stdout.read() == ""            # EOF: boruyu tutan torun da öldü


# ═══════════════════════════════════════════════════════════════════
#  MAIL HATTI — gönderici
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def smime_cert(tmp_path):
    if not HAS_OPENSSL:
        pytest.skip("openssl CLI yok")
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=t"], capture_output=True, check=True)
    return cert, key


class TestSmime:

    def test_signed_body_is_readable_and_verifies(self, smime_cert, tmp_path):
        # Eski: -text bayrağı gövdeyi "Content-Type... base64" metnine çeviriyordu
        cert, key = smime_cert
        srv = FakeSMTP()
        meta = _sender(srv.port).send_smime_signed("b@test.local", "Konu",
                                                   "Merhaba ÖZGÜN_METİN", str(cert), str(key))
        srv.done.wait(5)
        assert meta["signed"] is True
        msg = email.message_from_bytes(srv.data)
        det = MailReceiver.__new__(MailReceiver)._extract_details(msg, srv.data, "1")
        assert any("ÖZGÜN_METİN" in p["text_preview"] for p in det["parts"])
        eml = tmp_path / "s.eml"
        eml.write_bytes(srv.data)
        v = subprocess.run(["openssl", "smime", "-verify", "-noverify", "-in", str(eml)],
                           capture_output=True)
        assert v.returncode == 0

    def test_failed_signing_cleans_signed_file(self, smime_cert, tmp_path, monkeypatch):
        cert, _ = smime_cert
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        meta = _sender(1).send_smime_signed("b@t.l", "k", "g", str(cert), str(cert))
        assert meta.get("error") and meta["sent_at"] is None
        assert not list(tmp_path.glob("*.signed")) and not list(tmp_path.glob("*.eml"))


class TestAddresses:

    def test_display_name_only_from_needs_no_smtputf8(self):
        # Eski: '"Şule Çelik" <a@...>' SMTPUTF8 istiyor, desteklemeyen sunucuda reddediliyordu
        srv = FakeSMTP()
        meta = _sender(srv.port, frm='"Şule Çelik" <a@test.local>').send_plain_text("b@test.local", "k", "g")
        srv.done.wait(5)
        assert meta["sent_at"]
        assert "SMTPUTF8" not in srv.mail_from().upper() and "<a@test.local>" in srv.mail_from()

    def test_idn_domain_sent_as_a_label(self):
        srv = FakeSMTP()
        _sender(srv.port).send_plain_text("info@müller.de", "k", "g")
        srv.done.wait(5)
        assert srv.rcpts() == ["<info@xn--mller-kva.de>"]

    def test_partial_refusal_is_failure(self):
        # Eski: iki alıcıdan biri 550 alınca sonuç yine "gönderildi"
        srv = FakeSMTP(refuse=("b@x.com",))
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            _sender(srv.port).send_plain_text("a@x.com, b@x.com", "k", "g")

    @pytest.mark.parametrize("to,expected", [
        ("b@x.com; c@x.com", ["<b@x.com>", "<c@x.com>"]),
        ("b@x.com, c@x.com,", ["<b@x.com>", "<c@x.com>"]),
        ('"Soyad, Ad" <b@x.com>', ["<b@x.com>"]),
    ])
    def test_recipient_lists(self, to, expected):
        # Eski: ";" ve sondaki virgül tek bozuk RCPT olarak gidiyordu (501)
        srv = FakeSMTP()
        _sender(srv.port).send_plain_text(to, "k", "g")
        srv.done.wait(5)
        assert srv.rcpts() == expected

    @pytest.mark.parametrize("bad", ["", "   ", "b b2", "@x.com", "kimse"])
    def test_invalid_recipient_is_clear_error(self, bad):
        with pytest.raises(ValueError, match="Geçersiz e-posta"):
            parse_addresses(bad)

    def test_to_header_encodes_only_display_name(self):
        # Eski: "=?utf-8?b?...<b@test.local>?=" — adres de encoded-word içindeydi
        header = format_address_header("Ünal Çelik <b@test.local>")
        assert header.endswith(" <b@test.local>") and header.startswith("=?utf-8?")
        assert email.utils.getaddresses([header])[0][1] == "b@test.local"

    def test_relay_without_auth(self):
        # Eski: kullanıcı adı boşken de login → "SMTP AUTH extension not supported"
        srv = FakeSMTP(ext=("8BITMIME",))
        meta = _sender(srv.port, user="").send_plain_text("b@test.local", "k", "g")
        srv.done.wait(5)
        assert meta["sent_at"] and not any(c.upper().startswith(b"AUTH") for c in srv.cmds)

    def test_ics_uses_bare_mailto_and_quoted_cn(self):
        ics = sender_mod._build_ics("u", "s", "d", "l", __import__("datetime").datetime.now(),
                                    __import__("datetime").datetime.now(),
                                    "Test Kullanıcı <t@x.com>", "Çelik, Ünal <u@x.com>")
        assert 'ORGANIZER;CN="Test Kullanıcı":mailto:t@x.com' in ics.replace("\r\n ", "")
        assert ":mailto:u@x.com" in ics.replace("\r\n ", "")
        assert "mailto:Test" not in ics

    def test_attachment_size_in_send_meta(self, tmp_path):
        # Eski: prompt "orijinal: ? byte" diyordu
        f = tmp_path / "a.pdf"
        f.write_bytes(b"%PDF" + b"x" * 96)
        srv = FakeSMTP()
        meta = _sender(srv.port).send_with_attachment("b@test.local", "k", "g", str(f))
        assert meta["attachment_size"] == 100

    def test_connect_deadline_against_dripping_server(self, monkeypatch):
        # Eski: 5 sn'de bir satır damlatan sunucu koşuyu süresiz bekletiyordu
        monkeypatch.setattr(sender_mod, "CONNECT_DEADLINE", 1.0)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        stop = threading.Event()

        def drip():
            c, _ = srv.accept()
            try:
                while not stop.is_set():
                    c.sendall(b"220-bekle\r\n")
                    time.sleep(0.2)
            except OSError:
                pass
            c.close()
        threading.Thread(target=drip, daemon=True).start()
        start = time.time()
        try:
            with pytest.raises(TimeoutError, match="sn içinde"):
                _sender(srv.getsockname()[1]).send_plain_text("b@test.local", "k", "g")
        finally:
            stop.set()
            srv.close()
        assert time.time() - start < 5


# ═══════════════════════════════════════════════════════════════════
#  MAIL HATTI — alıcı, analiz, orkestratör
# ═══════════════════════════════════════════════════════════════════

class TestReceiverDeepForward:

    def test_deep_rfc822_does_not_raise(self):
        # Eski: 300+ iç içe message/rfc822 → RecursionError → "IMAP kontrol edilemedi"
        inner = b"Subject: x\r\n\r\nbody\r\n"
        for _ in range(400):
            inner = b"Content-Type: message/rfc822\r\n\r\n" + inner
        raw = (b"Subject: s\r\nMessage-ID: <d@x>\r\nMIME-Version: 1.0\r\n"
               b"Content-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\n" + inner + b"\r\n--B--\r\n")
        det = MailReceiver.__new__(MailReceiver)._extract_details(email.message_from_bytes(raw), raw, "1")
        assert det["headers"]["message_id"] == "<d@x>"


class TestVerdictWarnings:

    def test_prompts_define_check_semantics(self, received_msg, combination_meta):
        a = MailAnalyzer(api_key="k")
        for sc in ("complex_html", "calendar_invite", "html_table", "forward"):
            prompt = a._build_prompt(sc, {}, received_msg, combination_meta)
            assert "checks[].passed: true = kontrol noktasında SORUN YOK" in prompt
        html = a._build_prompt("complex_html", {}, received_msg, combination_meta)
        assert "passed=true bırak" in html and "Uyarı:" in html

    def test_warning_check_does_not_flip_pass(self):
        # Eski: prompt'un "uyarı olarak raporla" dediği @media durumu FAIL'e çevriliyordu
        v = MailAnalyzer(api_key="k")._parse_response(json.dumps({
            "passed": True, "confidence": "HIGH", "summary": "uyarı dışında sorun yok",
            "checks": [{"name": "Media query", "passed": False, "detail": "Uyarı: @media kaldırılmış"},
                       {"name": "HTML", "passed": True, "detail": "ok"}]}), "complex_html")
        assert v["passed"] is True and v["confidence"] == "HIGH"

    def test_real_failure_still_flips(self):
        v = MailAnalyzer(api_key="k")._parse_response(json.dumps({
            "passed": True, "checks": [{"name": "HTML", "passed": False, "detail": "html yok"}]}),
            "complex_html")
        assert v["passed"] is False


class TestApiKeys:

    @pytest.mark.parametrize("provider", ["claude", "gemini"])
    def test_key_whitespace_stripped(self, provider):
        a = MailAnalyzer(api_key="  AIzaGECERLI123\n", provider=provider)
        assert a.api_key == "AIzaGECERLI123" and a._circuit_reason is None

    @pytest.mark.parametrize("provider", ["claude", "gemini"])
    @pytest.mark.parametrize("key", ["anahtar-ğüş", "iki parca"])
    def test_invalid_key_fails_fast(self, provider, key, received_msg, combination_meta):
        # Eski: "bağlanılamadı" / "JSON değil" gibi yanıltıcı hatalar, devre kesiciyi besliyordu
        a = MailAnalyzer(api_key=key, provider=provider)
        with patch("analyzer.httpx.post") as post, patch.object(a, "_claude_request") as req:
            v = a.analyze("plain_text", {}, received_msg, combination_meta)
        post.assert_not_called()
        req.assert_not_called()
        assert "geçersiz karakter" in v["summary"]

    def test_whitespace_env_key_is_missing(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
        assert "tanımlı değil" in MailAnalyzer(api_key="")._circuit_reason


class TestOrchestratorRound4:

    def _cfg(self, cli_env, **test_overrides):
        cfg = yaml.safe_load(cli_env.read_text(encoding="utf-8"))
        cfg["test"].update(test_overrides)
        cli_env.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
        return cfg

    def _run(self, cli_env, *extra, results=None):
        with patch.object(main_mod, "MailSender"), patch.object(main_mod, "MailReceiver"), \
             patch.object(main_mod, "MailAnalyzer"), \
             patch.object(main_mod, "run_scenario", return_value=results or {
                 "combination": "c", "scenario_type": "plain_text", "scenario_key": "plain_text",
                 "test_time": "t", "analysis": {"passed": True, "confidence": "HIGH", "checks": [],
                                                "summary": "", "issues": [], "recommendations": []}}), \
             patch.object(main_mod.time, "sleep"):
            try:
                _run_cli(["--config", str(cli_env), "--combo", "0", "--scenario", "plain_text", *extra])
            except SystemExit as e:
                return e.code
        return 0

    @pytest.mark.parametrize("blank", ["", None])
    def test_blank_report_paths_use_defaults(self, cli_env, tmp_path, monkeypatch, blank):
        # Eski: boş report_output/results_csv → tüm sonuçlar kayboluyor, çıkış 1
        monkeypatch.chdir(tmp_path)
        self._cfg(cli_env, report_output=blank, results_csv=blank)
        assert self._run(cli_env) == 0
        assert (tmp_path / "reports" / "test_report.html").exists()
        assert (tmp_path / "reports" / "test_results.csv").exists()

    @pytest.mark.parametrize("blank", ["", None, "   "])
    def test_blank_attachment_path_means_default(self, blank, test_cfg):
        # Eski: null → TypeError ile 11 senaryonun hepsi FAIL; "" → çalışma dizini ek olarak
        cfg = {**test_cfg, "test_attachment_path": blank, "test_attachment_paths": [blank]}
        assert main_mod._configured_attachment_list(cfg) == []
        assert main_mod._missing_configured_attachments(cfg, "attachment") == []
        paths = main_mod._resolve_attachment_paths(cfg)
        assert "." not in paths and all(p.endswith("test_document.pdf") for p in paths)
        sender, receiver, analyzer = _sender_mock(), _receiver_mock(), _analyzer_mock()
        res = main_mod.run_scenario("plain_text", _combo(), 0, sender, receiver, analyzer, cfg)
        assert res["analysis"]["passed"] is True

    def test_attachment_tilde_and_data_dir(self, tmp_path, monkeypatch):
        # Eski: ~ genişletilmiyor, DATA_DIR'e bakılmıyordu (yanlış Konfigürasyon FAIL)
        home, data, cwd = tmp_path / "home", tmp_path / "data", tmp_path / "cwd"
        for d in (home, data / "files", cwd):
            d.mkdir(parents=True)
        (home / "doc.pdf").write_bytes(b"%PDF")
        (data / "files" / "ek.pdf").write_bytes(b"%PDF")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("DATA_DIR", str(data))
        monkeypatch.chdir(cwd)
        for raw in ("~/doc.pdf", "files/ek.pdf"):
            cfg = {"test_attachment_path": raw}
            assert main_mod._missing_configured_attachments(cfg, "attachment") == []
            assert len(main_mod._resolve_attachment_paths(cfg)) == 1

    def test_interrupt_during_reporting_is_deferred(self, cli_env, tmp_path, monkeypatch):
        # Eski: rapor yazılırken gelen SIGTERM yazımı kesiyor, hiç rapor kalmıyordu
        monkeypatch.chdir(tmp_path)
        self._cfg(cli_env, report_output=str(tmp_path / "r.html"), results_csv=str(tmp_path / "r.csv"))
        old_term, old_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        real_html = main_mod.generate_html_report

        def html_then_sigterm(*a, **k):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)
            return real_html(*a, **k)
        try:
            with patch.object(main_mod, "generate_html_report", side_effect=html_then_sigterm):
                code = self._run(cli_env)
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            main_mod._INTERRUPT.update({"defer": False, "pending": False})
        assert code == main_mod.EXIT_INTERRUPTED
        assert (tmp_path / "r.html").exists() and (tmp_path / "r.csv").exists()

    @pytest.mark.parametrize("provider", ["gemni", "gemini ", "GEMINI", "openai"])
    def test_provider_validated(self, cli_env, provider, caplog):
        # Eski: "gemni"/"gemini " sessizce Claude ile çalışıyordu
        cfg = yaml.safe_load(cli_env.read_text(encoding="utf-8"))
        cfg["analysis"] = {"provider": provider}
        cli_env.write_text(yaml.dump(cfg), encoding="utf-8")
        with patch.object(main_mod, "MailAnalyzer") as ma, \
             patch.object(main_mod, "run_scenario", return_value={"analysis": {"passed": True}}), \
             patch.object(main_mod, "generate_html_report"), patch.object(main_mod, "generate_csv_results"), \
             caplog.at_level("ERROR"):
            try:
                _run_cli(["--config", str(cli_env), "--combo", "0", "--scenario", "plain_text"])
                code = 0
            except SystemExit as e:
                code = e.code
        if provider.strip().lower() == "gemini":
            assert ma.call_args.kwargs.get("provider") == "gemini"
        else:
            assert code == main_mod.EXIT_ERROR and "Bilinmeyen analysis.provider" in caplog.text

    @pytest.mark.parametrize("section", ["test", "anthropic", "logging", "ems"])
    def test_wrong_typed_section_is_clean_error(self, tmp_path, section):
        # Eski: "'str' object has no attribute 'get'" ham traceback
        p = tmp_path / "c.yaml"
        p.write_text(f"{section}: metin\n", encoding="utf-8")
        with pytest.raises(ValueError, match=section):
            main_mod.load_config(str(p))
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--config", str(p)],
                           capture_output=True, text=True, cwd=tmp_path, timeout=60)
        assert r.returncode == 1 and "Traceback" not in r.stderr

    @pytest.mark.parametrize("field", ["smtp_host", "imap_host", "test_address", "smtp_port"])
    def test_blank_server_fields_are_config_errors(self, full_config, field):
        # Eski: boş imap_host localhost'a bağlanıyor, boş smtp_host anlaşılmaz hata veriyordu
        full_config["gmail"][field] = "  "
        with pytest.raises(ValueError, match=field):
            main_mod.get_server_config(full_config, "Gmail")

    def test_error_rows_use_csv_label(self, test_cfg):
        # Eski: hata satırı "attachment", normal satır "Eklentili Mesaj (Attachment)" yazıyordu
        cfg = {**test_cfg, "test_attachment_path": "yok/olmayan.pdf"}
        res = main_mod.run_scenario("attachment", _combo(), 0, _sender_mock(), _receiver_mock(),
                                    _analyzer_mock(), cfg)
        assert res["scenario_type"] == "Eklentili Mesaj (Attachment)"

    def test_scenario_help_lists_all(self, capsys):
        with pytest.raises(SystemExit):
            _run_cli(["--help"])
        out = capsys.readouterr().out
        from csv_parser import SUPPORTED_SCENARIOS
        assert all(s in out.replace("\n", "").replace(" ", "") for s in SUPPORTED_SCENARIOS)

    def test_infinite_number_falls_back(self):
        assert main_mod._num(float("inf"), 15) == 15

    def test_cli_reads_dates_as_text(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("test:\n  subject_prefix: 2026-02-30\n", encoding="utf-8")
        assert main_mod.load_config(str(p))["test"]["subject_prefix"] == "2026-02-30"


class TestE2eSmokeBlankTest:

    def test_blank_test_section(self, tmp_path):
        # Eski: "test:" boşken her senaryo NoneType.get ile düşüyordu
        cfg = tmp_path / "c.yaml"
        cfg.write_text("ems:\n  smtp_host: 127.0.0.1\n  smtp_port: 9\n  username: u\n"
                       "  password: p\n  test_address: a@b.c\ntest:\n", encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "e2e_smoke.py"), "--config", str(cfg),
                            "--server", "ems", "--to", "a@b.c", "--dry-run"],
                           capture_output=True, text=True, cwd=tmp_path, timeout=120)
        assert "NoneType" not in r.stdout + r.stderr
