"""
test_ui.py — Tarayıcı tabanlı arayüz testleri (Playwright + Chromium).

templates/index.html ~600 satır inline JS içerir ve pytest bunu çalıştıramaz;
bu dosya gerçek bir tarayıcıda gerçek Flask sunucusuna karşı koşar.

Playwright veya tarayıcı yoksa tüm dosya atlanır — temel test paketi
(python -m pytest) ek bağımlılık olmadan çalışmaya devam eder.

Yerel kurulum:
    pip install playwright && playwright install chromium
    python -m pytest tests/test_ui.py
"""

import glob
import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright kurulu değil"
).sync_playwright

import app as app_mod


# ── Tarayıcı bulma ───────────────────────────────────────────────────

def _chromium_executable():
    """Ortamda hazır Chromium varsa yolunu döndürür (indirme gerektirmez)."""
    env_path = os.environ.get("PW_CHROMIUM_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    for pattern in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome",
                    "/opt/pw-browsers/chromium-*/chrome-linux64/chrome"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        exe = _chromium_executable()
        try:
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as e:                                   # tarayıcı indirilmemiş
            pytest.skip(f"Chromium başlatılamadı: {e}")
        yield b
        b.close()


# ── Canlı Flask sunucusu ─────────────────────────────────────────────

@pytest.fixture(scope="session")
def live_server():
    from werkzeug.serving import make_server

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    srv = make_server("127.0.0.1", port, app_mod.app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    thread.join(timeout=5)


def _open(browser, url):
    """Sayfayı açar. Dış CDN istekleri (Google Fonts) kesilir — testler ağa
    bağımlı olmasın ve çevrimdışı ortamda font timeout'u beklemesin."""
    context = browser.new_context()
    context.route(
        "**/*",
        lambda route: route.abort()
        if "127.0.0.1" not in route.request.url and "localhost" not in route.request.url
        else route.continue_(),
    )
    page = context.new_page()
    page.goto(url)
    page.wait_for_selector("#page-dashboard", timeout=15000)
    return context, page


@pytest.fixture
def ui(browser, live_server, tmp_path, monkeypatch):
    """Temiz config + boş localStorage ile açılmış sayfa."""
    monkeypatch.setattr(app_mod, "CONFIG_PATH", tmp_path / "config.yaml")
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    monkeypatch.setattr(app_mod, "REPORTS_DIR", reports)
    app_mod.run_state.update({"running": False, "process": None,
                              "log_file": None, "exit_code": None})
    context, page = _open(browser, live_server)
    page.wait_for_timeout(300)   # ilk loadConfig/loadDashboard tamamlansın
    yield page
    context.close()


def _write_config(tmp_path, data):
    (tmp_path / "config.yaml").write_text(
        yaml.dump(data, allow_unicode=True), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════
#  Sayfa yükleme ve gezinme
# ═══════════════════════════════════════════════════════════════════

class TestNavigation:

    def test_page_loads_without_js_errors(self, browser, live_server, tmp_path, monkeypatch):
        monkeypatch.setattr(app_mod, "CONFIG_PATH", tmp_path / "config.yaml")
        errors = []
        context = browser.new_context()
        context.route(
            "**/*",
            lambda route: route.abort() if "127.0.0.1" not in route.request.url
            else route.continue_(),
        )
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(live_server)
        page.wait_for_selector("#page-dashboard", timeout=15000)
        page.wait_for_timeout(800)
        context.close()
        assert errors == [], f"Arayüzde JS hatası: {errors}"

    def test_dashboard_is_default_page(self, ui):
        assert ui.is_visible("#page-dashboard")

    @pytest.mark.parametrize("page_key", [
        "config", "security", "runner", "logs", "results", "reports", "guide",
    ])
    def test_every_nav_item_opens_its_page(self, ui, page_key):
        ui.click(f'.nav-item[data-page="{page_key}"]')
        assert ui.is_visible(f"#page-{page_key}")

    def test_nav_item_marked_active(self, ui):
        ui.click('.nav-item[data-page="config"]')
        cls = ui.get_attribute('.nav-item[data-page="config"]', "class")
        assert "active" in cls

    def test_server_tabs_switch_panels(self, ui):
        ui.click('.nav-item[data-page="config"]')
        assert ui.is_visible("#srv-ems")
        ui.click('#page-config .srv-tab:has-text("Gmail")')
        assert ui.is_visible("#srv-gmail")
        assert not ui.is_visible("#srv-ems")


# ═══════════════════════════════════════════════════════════════════
#  Secret gizliliği — güvenlik açısından kritik
# ═══════════════════════════════════════════════════════════════════

class TestSecretHandling:

    def test_saved_password_shown_masked_not_plaintext(self, browser, live_server,
                                                       tmp_path, monkeypatch):
        _write_config(tmp_path, {
            "ems": {"username": "u@t.local", "password": "COK-GIZLI-SIFRE",
                    "totp_secret": "JBSWY3DPEHPK3PXP", "smtp_host": "s.t", "smtp_port": 587},
            "anthropic": {"api_key": "sk-ant-COK-GIZLI"},
        })
        monkeypatch.setattr(app_mod, "CONFIG_PATH", tmp_path / "config.yaml")
        context, page = _open(browser, live_server)
        page.wait_for_timeout(400)
        html = page.content()
        pwd = page.input_value("#ems_password")
        api = page.input_value("#anthropic_api_key")
        context.close()

        assert "COK-GIZLI-SIFRE" not in html
        assert "sk-ant-COK-GIZLI" not in html
        assert set(pwd) == {"•"} and set(api) == {"•"}

    def test_localstorage_never_stores_secrets(self, ui, tmp_path):
        ui.click('.nav-item[data-page="config"]')
        ui.fill("#ems_password", "yeni-sifre-123")
        ui.fill("#anthropic_api_key", "sk-ant-yeni")
        # TOTP alanı Güvenlik sekmesinde ve koşullu görünür — değeri doğrudan
        # atıyoruz; test edilen şey lsSave'in secret'ı ayıklaması.
        ui.evaluate("document.getElementById('ems_totp_secret').value = 'JBSWY3DPEHPK3PXP'")
        ui.click("text=💾 Kaydet")
        ui.wait_for_timeout(400)

        stored = ui.evaluate("localStorage.getItem('mail_otomasyon_config')")
        assert stored, "localStorage'a config yazılmalı"
        assert "yeni-sifre-123" not in stored
        assert "JBSWY3DPEHPK3PXP" not in stored
        assert "sk-ant-yeni" not in stored
        cfg = json.loads(stored)
        assert cfg["ems"].get("password") is None
        assert cfg["ems"].get("totp_secret") is None
        assert cfg.get("anthropic", {}).get("api_key") is None

    def test_saving_masked_form_preserves_password(self, browser, live_server,
                                                   tmp_path, monkeypatch):
        """En riskli akış: kullanıcı şifreye dokunmadan Kaydet'e basarsa
        maskeli değer düz metin olarak üzerine yazılmamalı."""
        cfg_path = tmp_path / "config.yaml"
        _write_config(tmp_path, {
            "ems": {"username": "u@t.local", "password": "ORIJINAL-SIFRE",
                    "smtp_host": "s.t", "smtp_port": 587},
            "anthropic": {"api_key": "sk-ant-ORIJINAL"},
        })
        monkeypatch.setattr(app_mod, "CONFIG_PATH", cfg_path)
        context, page = _open(browser, live_server)
        page.wait_for_timeout(400)
        page.click('.nav-item[data-page="config"]')
        page.fill("#ems_username", "yeni@t.local")     # secret'a dokunmadan başka alan
        page.click("text=💾 Kaydet")
        page.wait_for_timeout(500)
        context.close()

        saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert saved["ems"]["password"] == "ORIJINAL-SIFRE"
        assert saved["anthropic"]["api_key"] == "sk-ant-ORIJINAL"
        assert saved["ems"]["username"] == "yeni@t.local"

    def test_typed_password_reaches_server(self, ui, tmp_path):
        ui.click('.nav-item[data-page="config"]')
        ui.fill("#ems_password", "gercekten-yeni")
        ui.click("text=💾 Kaydet")
        ui.wait_for_timeout(500)
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["ems"]["password"] == "gercekten-yeni"


# ═══════════════════════════════════════════════════════════════════
#  Konfigürasyon formu
# ═══════════════════════════════════════════════════════════════════

class TestConfigForm:

    def test_form_filled_from_server_config(self, browser, live_server, tmp_path, monkeypatch):
        _write_config(tmp_path, {
            "ems": {"smtp_host": "ems.kurum.local", "smtp_port": 2525,
                    "username": "test@kurum.local", "label": "Kurum EMS"},
            "test": {"wait_seconds": 42, "subject_prefix": "[KURUM]"},
        })
        monkeypatch.setattr(app_mod, "CONFIG_PATH", tmp_path / "config.yaml")
        context, page = _open(browser, live_server)
        page.wait_for_timeout(400)
        page.click('.nav-item[data-page="config"]')
        assert page.input_value("#ems_smtp_host") == "ems.kurum.local"
        assert page.input_value("#ems_smtp_port") == "2525"
        assert page.input_value("#test_wait_seconds") == "42"
        assert page.input_value("#test_subject_prefix") == "[KURUM]"
        context.close()

    def test_save_shows_success_toast(self, ui):
        ui.click('.nav-item[data-page="config"]')
        ui.fill("#ems_smtp_host", "yeni.host.local")
        ui.click("text=💾 Kaydet")
        ui.wait_for_selector("#toasts >> text=kaydedildi", timeout=5000)

    def test_numeric_ports_saved_as_numbers(self, ui, tmp_path):
        ui.click('.nav-item[data-page="config"]')
        ui.fill("#ems_smtp_port", "465")
        ui.click("text=💾 Kaydet")
        ui.wait_for_timeout(500)
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert saved["ems"]["smtp_port"] == 465
        assert isinstance(saved["ems"]["smtp_port"], int)

    def test_reload_restores_from_server(self, ui, tmp_path):
        ui.click('.nav-item[data-page="config"]')
        ui.fill("#ems_smtp_host", "kalici.host")
        ui.click("text=💾 Kaydet")
        ui.wait_for_timeout(400)
        ui.fill("#ems_smtp_host", "gecici-degisiklik")
        ui.click("text=↺ Yenile")
        ui.wait_for_timeout(400)
        assert ui.input_value("#ems_smtp_host") == "kalici.host"


# ═══════════════════════════════════════════════════════════════════
#  Test matrisi
# ═══════════════════════════════════════════════════════════════════

class TestCombinationsUi:

    def test_matrix_loads_18_combinations(self, ui):
        ui.wait_for_selector("#dashCombosTable tr", timeout=8000)
        rows = ui.locator("#dashCombosTable tr").count()
        assert rows >= 18

    def test_runner_page_lists_combinations(self, ui):
        ui.click('.nav-item[data-page="runner"]')
        ui.wait_for_selector("#comboGrid", timeout=8000)
        assert ui.locator("#comboGrid").inner_text().strip() != ""


# ═══════════════════════════════════════════════════════════════════
#  Canlı loglar
# ═══════════════════════════════════════════════════════════════════

class TestLogStreaming:

    def test_terminal_renders_polled_lines(self, ui, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("▶ Senaryo: plain_text\n✅ PASS ğüşıöç\n", encoding="utf-8")
        app_mod.run_state["log_file"] = str(log)
        ui.click('.nav-item[data-page="logs"]')
        ui.evaluate("startLogStream()")
        ui.wait_for_selector("#terminalBody .log-line", timeout=8000)
        body = ui.locator("#terminalBody").inner_text()
        assert "Senaryo: plain_text" in body
        assert "ğüşıöç" in body                      # UTF-8 bozulmamalı
        assert "2 satır" in ui.locator("#logCount").inner_text()

    def test_stream_stops_when_run_finished(self, ui, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("tek satır\n", encoding="utf-8")
        app_mod.run_state["log_file"] = str(log)
        ui.click('.nav-item[data-page="logs"]')
        ui.evaluate("startLogStream()")
        ui.wait_for_selector("#terminalBody .log-line", timeout=8000)
        # done=True gelince interval temizlenmeli — arka planda sonsuz poll kalmasın
        ui.wait_for_function("() => logPollInterval === null", timeout=10000)


# ═══════════════════════════════════════════════════════════════════
#  2FA modal
# ═══════════════════════════════════════════════════════════════════

class TestMfaModal:

    def test_modal_opens_when_server_requests_code(self, ui):
        ui.click('.nav-item[data-page="config"]')
        ui.fill("#ems_smtp_host", "smtp.kurum.local")
        ui.fill("#ems_username", "u@kurum.local")
        ui.fill("#ems_password", "sifre")
        ui.fill("#ems_label", "Kurum EMS")
        # 2FA yöntemi/alanları Güvenlik sekmesinde — JS durumunu doğrudan kuruyoruz
        ui.evaluate("document.getElementById('ems_mfa_method').value = 'sms'")
        ui.evaluate("authMethods.ems = 'totp_password'")
        ui.evaluate("testConn('ems')")
        ui.wait_for_selector("#connMfaModal", state="visible", timeout=10000)
        # etiket CSS ile büyük harfe çevriliyor — büyük/küçük duyarsız karşılaştır
        assert "KURUM EMS" in ui.locator("#connMfaLabel").inner_text().upper()

    def test_modal_can_be_cancelled(self, ui):
        ui.click('.nav-item[data-page="config"]')
        ui.fill("#ems_smtp_host", "smtp.kurum.local")
        ui.fill("#ems_username", "u@kurum.local")
        ui.fill("#ems_password", "sifre")
        ui.evaluate("document.getElementById('ems_mfa_method').value = 'sms'")
        ui.evaluate("authMethods.ems = 'totp_password'")
        ui.evaluate("testConn('ems')")
        ui.wait_for_selector("#connMfaModal", state="visible", timeout=10000)
        ui.evaluate("closeConnMfaModal()")
        ui.wait_for_selector("#connMfaModal", state="hidden", timeout=5000)

    def test_modal_hidden_initially(self, ui):
        assert not ui.is_visible("#mfaModal")
        assert not ui.is_visible("#connMfaModal")


# ═══════════════════════════════════════════════════════════════════
#  Sonuçlar ve raporlar
# ═══════════════════════════════════════════════════════════════════

class TestResultsAndReports:

    def test_results_table_renders_csv(self, ui, tmp_path):
        (tmp_path / "reports" / "sonuc.csv").write_text(
            "Kombinasyon,Senaryo,Sonuç,Güven,Özet\n"
            "EMS/iOS ← Gmail/Android,plain_text,PASS,HIGH,Tamam\n",
            encoding="utf-8")
        ui.click('.nav-item[data-page="results"]')
        ui.wait_for_timeout(800)
        assert "EMS/iOS" in ui.locator("#resultsBody").inner_text()

    def test_report_list_shows_html_file(self, ui, tmp_path):
        (tmp_path / "reports" / "rapor.html").write_text("<h1>Rapor</h1>", encoding="utf-8")
        ui.click('.nav-item[data-page="reports"]')
        ui.wait_for_timeout(800)
        assert "rapor.html" in ui.locator("#reportList").inner_text()

    def test_empty_state_does_not_crash(self, ui):
        errors = []
        ui.on("pageerror", lambda e: errors.append(str(e)))
        ui.click('.nav-item[data-page="results"]')
        ui.wait_for_timeout(500)
        ui.click('.nav-item[data-page="reports"]')
        ui.wait_for_timeout(500)
        assert errors == []
