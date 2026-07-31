"""
test_app_endpoints.py — app.py'de test edilmemiş uç noktalar.

Kapsam: config içe/dışa aktarma, log polling, çalışma durdurma,
CSV yolu çözümleme ve kombinasyon uç noktası hata yolları.
"""

import io
import json
import smtplib
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import app as app_mod


# ═══════════════════════════════════════════════════════════════════
#  /api/config/import
# ═══════════════════════════════════════════════════════════════════

class TestConfigImport:

    def _upload(self, client, content: str, filename="config.yaml"):
        return client.post(
            "/api/config/import",
            data={"file": (io.BytesIO(content.encode("utf-8")), filename)},
            content_type="multipart/form-data",
        )

    def test_no_file_returns_400(self, flask_client):
        r = flask_client.post("/api/config/import", data={}, content_type="multipart/form-data")
        assert r.status_code == 400
        assert "Dosya bulunamadı" in r.get_json()["error"]

    def test_wrong_extension_rejected(self, flask_client):
        r = self._upload(flask_client, "ems: {}", filename="config.txt")
        assert r.status_code == 400
        assert ".yaml" in r.get_json()["error"]

    def test_yml_extension_accepted(self, flask_client):
        r = self._upload(flask_client, "ems:\n  username: a@b.c\n", filename="config.yml")
        assert r.status_code == 200 and r.get_json()["ok"] is True

    def test_non_dict_yaml_rejected(self, flask_client):
        r = self._upload(flask_client, "- bir\n- iki\n")
        assert r.status_code == 400
        assert "Geçersiz YAML" in r.get_json()["error"]

    def test_malformed_yaml_returns_500(self, flask_client):
        r = self._upload(flask_client, "ems: [kapanmamis\n  girinti: bozuk\n")
        assert r.status_code in (400, 500)
        assert r.get_json()["ok"] is False

    def test_valid_import_writes_config_file(self, flask_client, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg_path)
        r = self._upload(flask_client, yaml.dump({
            "ems": {"username": "a@b.c", "password": "gizli"},
            "anthropic": {"api_key": "sk-ant-123"},
        }))
        assert r.status_code == 200
        saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert saved["ems"]["password"] == "gizli"       # dosyada düz metin
        assert saved["anthropic"]["api_key"] == "sk-ant-123"

    def test_import_response_masks_secrets(self, flask_client, tmp_path, monkeypatch):
        monkeypatch.setattr(app_mod, "CONFIG_PATH", tmp_path / "config.yaml")
        r = self._upload(flask_client, yaml.dump({
            "ems": {"password": "gizli", "totp_secret": "JBSWY3DPEHPK3PXP"},
            "anthropic": {"api_key": "sk-ant-123"},
        }))
        cfg = r.get_json()["config"]
        assert cfg["ems"]["password"] == app_mod.SECRET_MASK
        assert cfg["ems"]["totp_secret"] == app_mod.SECRET_MASK
        assert cfg["anthropic"]["api_key"] == app_mod.SECRET_MASK

    def test_import_overwrites_previous_config(self, flask_client, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump({"ems": {"username": "eski@x.y"}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg_path)
        self._upload(flask_client, yaml.dump({"ems": {"username": "yeni@x.y"}}))
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["ems"]["username"] == "yeni@x.y"


# ═══════════════════════════════════════════════════════════════════
#  /api/config/export
# ═══════════════════════════════════════════════════════════════════

class TestConfigExport:

    def test_export_without_config_404(self, flask_client):
        r = flask_client.get("/api/config/export")
        assert r.status_code == 404
        assert r.get_json()["ok"] is False

    def test_export_returns_attachment(self, flask_client, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("ems:\n  username: a@b.c\n", encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg_path)
        r = flask_client.get("/api/config/export")
        assert r.status_code == 200
        assert "attachment" in r.headers["Content-Disposition"]
        assert "config.yaml" in r.headers["Content-Disposition"]
        assert b"a@b.c" in r.data

    def test_export_roundtrip_with_import(self, flask_client, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        original = {"ems": {"username": "a@b.c", "password": "p"}, "test": {"wait_seconds": 20}}
        cfg_path.write_text(yaml.dump(original, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg_path)
        exported = flask_client.get("/api/config/export").data.decode("utf-8")
        cfg_path.unlink()
        flask_client.post(
            "/api/config/import",
            data={"file": (io.BytesIO(exported.encode("utf-8")), "config.yaml")},
            content_type="multipart/form-data",
        )
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8")) == original


# ═══════════════════════════════════════════════════════════════════
#  /api/run/logs — polling
# ═══════════════════════════════════════════════════════════════════

class TestRunLogsPolling:

    def test_no_log_file_reports_done(self, flask_client):
        app_mod.run_state["log_file"] = None
        r = flask_client.get("/api/run/logs")
        data = r.get_json()
        assert data["lines"] == [] and data["done"] is True

    def test_reads_lines_from_offset_zero(self, flask_client, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("satir 1\nsatir 2\nsatir 3\n", encoding="utf-8")
        app_mod.run_state["log_file"] = str(log)
        data = flask_client.get("/api/run/logs?offset=0").get_json()
        assert data["lines"] == ["satir 1", "satir 2", "satir 3"]
        assert data["next_offset"] == 3

    def test_incremental_offset_returns_only_new(self, flask_client, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("a\nb\n", encoding="utf-8")
        app_mod.run_state["log_file"] = str(log)
        first = flask_client.get("/api/run/logs?offset=0").get_json()
        log.write_text("a\nb\nc\n", encoding="utf-8")
        second = flask_client.get(f"/api/run/logs?offset={first['next_offset']}").get_json()
        assert second["lines"] == ["c"]
        assert second["next_offset"] == 3

    def test_running_run_not_marked_done(self, flask_client, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("a\n", encoding="utf-8")
        app_mod.run_state["log_file"] = str(log)
        app_mod.run_state["running"] = True
        try:
            assert flask_client.get("/api/run/logs?offset=1").get_json()["done"] is False
        finally:
            app_mod.run_state["running"] = False

    def test_finished_run_with_drained_log_is_done(self, flask_client, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("a\n", encoding="utf-8")
        app_mod.run_state["log_file"] = str(log)
        app_mod.run_state["running"] = False
        assert flask_client.get("/api/run/logs?offset=1").get_json()["done"] is True

    def test_utf8_turkish_lines_preserved(self, flask_client, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("✅ Gönderildi ğüşıöç\n", encoding="utf-8")
        app_mod.run_state["log_file"] = str(log)
        assert flask_client.get("/api/run/logs?offset=0").get_json()["lines"] == \
            ["✅ Gönderildi ğüşıöç"]

    def test_missing_log_file_path_handled(self, flask_client, tmp_path):
        app_mod.run_state["log_file"] = str(tmp_path / "yok.log")
        data = flask_client.get("/api/run/logs?offset=0").get_json()
        assert data["lines"] == []


# ═══════════════════════════════════════════════════════════════════
#  /api/run/stop
# ═══════════════════════════════════════════════════════════════════

class TestRunStopWithProcess:

    def test_stop_terminates_process(self, flask_client):
        proc = MagicMock()
        app_mod.run_state["process"] = proc
        app_mod.run_state["running"] = True
        try:
            r = flask_client.post("/api/run/stop")
            assert r.get_json()["ok"] is True
            proc.terminate.assert_called_once()
            assert app_mod.run_state["running"] is False
        finally:
            app_mod.run_state["process"] = None

    def test_stop_cancels_pending_mfa(self, flask_client):
        app_mod.run_state["process"] = MagicMock()
        try:
            with patch.object(app_mod.mfa_manager, "cancel") as cancel:
                flask_client.post("/api/run/stop")
            cancel.assert_called_once()
        finally:
            app_mod.run_state["process"] = None


# ═══════════════════════════════════════════════════════════════════
#  _get_csv_path — çözümleme ve yedekler
# ═══════════════════════════════════════════════════════════════════

class TestGetCsvPath:

    def test_default_when_no_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)   # cwd repo kökü değil → mutlak yola çözülmeli
        monkeypatch.setattr(app_mod, "CONFIG_PATH", tmp_path / "yok.yaml")
        assert app_mod._get_csv_path() == str(PROJECT_ROOT / "mail_test_checklist.csv")

    def test_repo_root_cwd_keeps_relative_name(self, tmp_path, monkeypatch):
        """cwd zaten repo kökü ise göreli ad olduğu gibi döner (parse_csv açabilir)."""
        monkeypatch.chdir(PROJECT_ROOT)
        monkeypatch.setattr(app_mod, "CONFIG_PATH", tmp_path / "yok.yaml")
        assert app_mod._get_csv_path() == "mail_test_checklist.csv"
        assert Path(app_mod._get_csv_path()).is_file()

    def test_absolute_path_from_config(self, tmp_path, monkeypatch):
        target = tmp_path / "ozel.csv"
        target.write_text("a,b\n", encoding="utf-8")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"test": {"csv_input": str(target)}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        assert app_mod._get_csv_path() == str(target)

    def test_relative_path_resolved_against_repo_root(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"test": {"csv_input": "mail_test_checklist.csv"}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        monkeypatch.chdir(tmp_path)
        assert app_mod._get_csv_path() == str(PROJECT_ROOT / "mail_test_checklist.csv")

    def test_missing_csv_falls_back_to_repo_checklist(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"test": {"csv_input": "/yok/olmayan.csv"}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        assert app_mod._get_csv_path() == str(PROJECT_ROOT / "mail_test_checklist.csv")

    def test_config_without_test_section(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"ems": {"username": "a@b.c"}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        assert app_mod._get_csv_path() == str(PROJECT_ROOT / "mail_test_checklist.csv")

    def test_glob_fallback_prefers_checklist(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(app_mod, "__file__", str(tmp_path / "app.py"))
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"test": {"csv_input": "/yok/x.csv"}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        # Beklenen adda dosya YOK — glob yedeği devreye girmeli
        (tmp_path / "zzz_rastgele.csv").write_text("a\n", encoding="utf-8")
        (tmp_path / "eski_checklist.csv").write_text("a\n", encoding="utf-8")
        assert app_mod._get_csv_path() == str(tmp_path / "eski_checklist.csv")

    def test_no_csv_returns_configured_value(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(app_mod, "__file__", str(tmp_path / "app.py"))
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"test": {"csv_input": "/yok/x.csv"}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        assert app_mod._get_csv_path() == "/yok/x.csv"


# ═══════════════════════════════════════════════════════════════════
#  /api/run/start — alt süreç çalıştırıcı
# ═══════════════════════════════════════════════════════════════════

class TestRunnerSubprocess:

    def _wait_idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not app_mod.run_state["running"]:
                return True
            time.sleep(0.02)
        return False

    @pytest.fixture(autouse=True)
    def _isolated_logs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_mod, "LOGS_DIR", tmp_path / "logs")
        (tmp_path / "logs").mkdir()
        yield
        app_mod.run_state.update({"running": False, "process": None})

    def _fake_proc(self, lines, returncode=0):
        proc = MagicMock()
        proc.stdout = iter(lines)
        proc.returncode = returncode
        proc.wait.return_value = returncode
        return proc

    def test_subprocess_output_written_to_log_file(self, flask_client):
        proc = self._fake_proc(["ilk satır\n", "ikinci satır\n"])
        with patch.object(app_mod.subprocess, "Popen", return_value=proc):
            flask_client.post("/api/run/start", json={})
            assert self._wait_idle()
        log = Path(app_mod.run_state["log_file"])
        assert log.read_text(encoding="utf-8").splitlines() == ["ilk satır", "ikinci satır"]

    def test_exit_code_recorded(self, flask_client):
        with patch.object(app_mod.subprocess, "Popen", return_value=self._fake_proc([], 3)):
            flask_client.post("/api/run/start", json={})
            assert self._wait_idle()
        assert app_mod.run_state["exit_code"] == 3
        assert app_mod.run_state["finished_at"] is not None

    def test_mfa_interactive_env_passed_to_subprocess(self, flask_client):
        with patch.object(app_mod.subprocess, "Popen", return_value=self._fake_proc([])) as popen:
            flask_client.post("/api/run/start", json={})
            assert self._wait_idle()
        assert popen.call_args.kwargs["env"]["MFA_INTERACTIVE"] == "1"

    def test_spawn_failure_logged_not_crashed(self, flask_client):
        with patch.object(app_mod.subprocess, "Popen", side_effect=OSError("çalıştırılamadı")):
            r = flask_client.post("/api/run/start", json={})
            assert r.get_json()["ok"] is True    # başlatma isteği kabul edilir
            assert self._wait_idle()
        log = Path(app_mod.run_state["log_file"]).read_text(encoding="utf-8")
        assert "[HATA]" in log and "çalıştırılamadı" in log
        assert app_mod.run_state["running"] is False   # takılı kalmamalı

    def test_stale_mfa_bridge_cleared_on_start(self, flask_client):
        with patch.object(app_mod.subprocess, "Popen", return_value=self._fake_proc([])), \
             patch.object(app_mod.mfa_manager, "clear_bridge") as clear:
            flask_client.post("/api/run/start", json={})
            assert self._wait_idle()
        clear.assert_called_once()

    def test_logs_endpoint_serves_subprocess_output(self, flask_client):
        proc = self._fake_proc(["satır A\n", "satır B\n"])
        with patch.object(app_mod.subprocess, "Popen", return_value=proc):
            flask_client.post("/api/run/start", json={})
            assert self._wait_idle()
        # Sözleşme: yeni satır dönen poll'da done=False — arayüz kalan logları
        # boşaltmadan durmasın. done ancak boş bir poll'da True olur.
        first = flask_client.get("/api/run/logs?offset=0").get_json()
        assert first["lines"] == ["satır A", "satır B"]
        assert first["done"] is False
        second = flask_client.get(f"/api/run/logs?offset={first['next_offset']}").get_json()
        assert second["lines"] == [] and second["done"] is True


# ═══════════════════════════════════════════════════════════════════
#  /api/combinations — hata yolu
# ═══════════════════════════════════════════════════════════════════

class TestCombinationsErrors:

    def test_parse_error_reported_as_json(self, flask_client, tmp_path, monkeypatch):
        monkeypatch.setattr(app_mod, "_get_csv_path", lambda: str(tmp_path / "var.csv"))
        (tmp_path / "var.csv").write_text("x\n", encoding="utf-8")
        with patch("csv_parser.parse_csv", side_effect=RuntimeError("bozuk CSV")):
            data = flask_client.get("/api/combinations").get_json()
        assert data["ok"] is False and "bozuk CSV" in data["error"]

    def test_missing_csv_file_reported(self, flask_client, tmp_path, monkeypatch):
        monkeypatch.setattr(app_mod, "_get_csv_path", lambda: str(tmp_path / "yok.csv"))
        data = flask_client.get("/api/combinations").get_json()
        assert data["ok"] is False
        assert "CSV bulunamadı" in data["error"]

    def test_step_count_reported(self, flask_client):
        data = flask_client.get("/api/combinations").get_json()
        assert data["ok"] is True
        assert all(c["step_count"] > 0 for c in data["combinations"])
        assert len(data["combinations"]) == 18


# ═══════════════════════════════════════════════════════════════════
#  /api/config/test-connection — 2FA varyantları
# ═══════════════════════════════════════════════════════════════════

class TestConnectionMfaVariants:

    TOTP_SECRET = "JBSWY3DPEHPK3PXP"

    @pytest.fixture
    def saved_cfg(self, tmp_path, monkeypatch):
        def _write(ems: dict):
            cfg = tmp_path / "config.yaml"
            cfg.write_text(yaml.dump({"ems": ems}, allow_unicode=True), encoding="utf-8")
            monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
            return cfg
        return _write

    def _base(self, **over):
        return {"smtp_host": "smtp.t.local", "smtp_port": 587, "smtp_use_tls": False,
                "username": "u@t.local", "password": "sifre", **over}

    def test_totp_secret_appended_to_password(self, flask_client, saved_cfg):
        saved_cfg(self._base(auth_method="totp_password", totp_secret=self.TOTP_SECRET))
        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp):
            r = flask_client.post("/api/config/test-connection", json={"server": "ems"})
        assert r.get_json()["ok"] is True
        used = smtp.login.call_args[0][1]
        assert used.startswith("sifre") and len(used) == len("sifre") + 6

    def test_rejected_totp_login_falls_back_to_password(self, flask_client, saved_cfg):
        saved_cfg(self._base(auth_method="totp_password", totp_secret=self.TOTP_SECRET))
        smtp = MagicMock()
        smtp.login.side_effect = [smtplib.SMTPAuthenticationError(535, b"denied"), None]
        with patch("smtplib.SMTP", return_value=smtp):
            r = flask_client.post("/api/config/test-connection", json={"server": "ems"})
        assert r.get_json()["ok"] is True
        assert smtp.login.call_args_list[1][0][1] == "sifre"

    def test_otp_only_logs_in_with_code_alone(self, flask_client, saved_cfg):
        saved_cfg(self._base(auth_method="otp_only", mfa_method="sms"))
        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp):
            r = flask_client.post("/api/config/test-connection",
                                  json={"server": "ems", "mfa_code": "998877"})
        assert r.get_json()["ok"] is True
        smtp.login.assert_called_once_with("u@t.local", "998877")

    def test_sms_method_requires_manual_code(self, flask_client, saved_cfg):
        saved_cfg(self._base(auth_method="totp_password", mfa_method="sms",
                             totp_secret=self.TOTP_SECRET, label="EMS On-Prem"))
        r = flask_client.post("/api/config/test-connection", json={"server": "ems"})
        data = r.get_json()
        # TOTP secret var ama yöntem SMS — otomatik üretim yapılmamalı
        assert data["needs_mfa"] is True
        assert data["mfa_method"] == "sms"
        assert data["server_label"] == "EMS On-Prem"

    def test_unsaved_form_values_can_be_tested(self, flask_client, saved_cfg):
        saved_cfg(self._base(password="kayitli-sifre"))
        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp):
            r = flask_client.post("/api/config/test-connection", json={
                "server": "ems",
                "server_config": {**self._base(smtp_host="yeni.host"), "password": ""},
            })
        assert r.get_json()["ok"] is True
        # Form'da secret boş → dosyadaki kayıtlı şifre kullanılır
        smtp.login.assert_called_once_with("u@t.local", "kayitli-sifre")

    def test_masked_secret_in_form_uses_saved_value(self, flask_client, saved_cfg):
        saved_cfg(self._base(password="kayitli-sifre"))
        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp):
            flask_client.post("/api/config/test-connection", json={
                "server": "ems",
                "server_config": {**self._base(), "password": app_mod.SECRET_MASK},
            })
        smtp.login.assert_called_once_with("u@t.local", "kayitli-sifre")

    def test_starttls_used_when_enabled(self, flask_client, saved_cfg):
        saved_cfg(self._base(smtp_use_tls=True))
        smtp = MagicMock()
        with patch("smtplib.SMTP", return_value=smtp):
            flask_client.post("/api/config/test-connection", json={"server": "ems"})
        smtp.starttls.assert_called_once()

    def test_unknown_server_without_body_config(self, flask_client, saved_cfg):
        saved_cfg(self._base())
        r = flask_client.post("/api/config/test-connection", json={"server": "yandex"})
        assert r.get_json()["ok"] is False
        assert "config bulunamadı" in r.get_json()["error"]


# ═══════════════════════════════════════════════════════════════════
#  /api/diagnostics/network — kısıtlı ağ
# ═══════════════════════════════════════════════════════════════════

class TestDiagnosticsRestrictedNetwork:

    def test_all_ports_blocked_verdict(self, flask_client, monkeypatch):
        monkeypatch.setattr(app_mod.socket, "create_connection",
                            MagicMock(side_effect=OSError("Network is unreachable")))
        data = flask_client.get("/api/diagnostics/network").get_json()
        assert data["smtp_available"] is False
        assert "dış ağ erişimi kısıtlı" in data["verdict"]
        assert all(t["reachable"] is False for t in data["targets"])
        assert all("error" in t for t in data["targets"])


# ═══════════════════════════════════════════════════════════════════
#  /api/config — bozuk dosya davranışı
# ═══════════════════════════════════════════════════════════════════

class TestConfigSaveEdgeCases:

    def test_unknown_sections_preserved(self, flask_client, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({
            "ems": {"password": "p"},
            "analysis": {"provider": "gemini"},
            "gemini": {"api_key": "g"},
            "logging": {"level": "DEBUG"},
        }), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        # UI yalnızca 'ems' gönderiyor — diğer bölümler silinmemeli
        flask_client.post("/api/config", json={"config": {"ems": {"password": ""}}})
        saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert saved["analysis"]["provider"] == "gemini"
        assert saved["gemini"]["api_key"] == "g"
        assert saved["logging"]["level"] == "DEBUG"
        assert saved["ems"]["password"] == "p"   # boş gelen secret korunur

    def test_new_server_section_added(self, flask_client, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"ems": {"password": "p"}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        flask_client.post("/api/config", json={
            "config": {"ems": {"password": ""}, "outlook": {"password": "yeni"}}})
        saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert saved["outlook"]["password"] == "yeni"

    def test_unwritable_config_returns_500(self, flask_client, tmp_path, monkeypatch):
        monkeypatch.setattr(app_mod, "CONFIG_PATH", tmp_path / "config.yaml")
        with patch("builtins.open", side_effect=OSError("disk dolu")):
            r = flask_client.post("/api/config", json={"config": {"ems": {}}})
        assert r.status_code == 500
        assert "disk dolu" in r.get_json()["error"]

    def test_masked_api_key_not_written_literally(self, flask_client, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"anthropic": {"api_key": "sk-ant-gercek"}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg)
        flask_client.post("/api/config", json={
            "config": {"anthropic": {"api_key": app_mod.SECRET_MASK}}})
        saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert saved["anthropic"]["api_key"] == "sk-ant-gercek"
