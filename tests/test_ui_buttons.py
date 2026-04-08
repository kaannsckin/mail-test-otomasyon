"""
test_ui_buttons.py — Frontend HTML şablonundaki tüm butonların ve
etkileşimli elementlerin kapsamlı testleri.

Test Kategorileri:
  - Tüm butonların HTML'de varlığı
  - Buton onclick handler'larının varlığı
  - Navigasyon menu item'ları
  - Tab switchleri (EMS/Gmail/Outlook)
  - API endpoint'lerinden buton triggerları
  - Form alanları (input/select)
  - Run start/stop butonları
  - Modal butonları
  - Pill (senaryo seçici) elementi sayısı
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = Path(__file__).parent.parent
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "index.html"


# ── Fixtures ───────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def html_content():
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def soup(html_content):
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html_content, "html.parser")
    except ImportError:
        pytest.skip("beautifulsoup4 yüklü değil: pip install beautifulsoup4")


@pytest.fixture
def client():
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ══════════════════════════════════════════════════════════════════════
# 1. GENEL BUTON TESTLERİ
# ══════════════════════════════════════════════════════════════════════
class TestButtonsExist:
    def test_html_has_buttons(self, soup):
        """HTML'de button elementi bulunmalı."""
        buttons = soup.find_all("button")
        assert len(buttons) > 0, "Hiç buton bulunamadı"

    def test_minimum_button_count(self, soup):
        """En az 20 buton bulunmalı (tüm sayfalar)."""
        buttons = soup.find_all("button")
        assert len(buttons) >= 20, f"Yeterli buton yok. Bulunan: {len(buttons)}"

    def test_all_buttons_have_text_or_icon(self, soup):
        """Tüm butonlar görünür içerik (metin ya da emoji) içermeli."""
        buttons = soup.find_all("button")
        empty_buttons = []
        for btn in buttons:
            text = btn.get_text(strip=True)
            if not text:
                empty_buttons.append(str(btn)[:80])
        assert len(empty_buttons) == 0, f"İçeriksiz butonlar: {empty_buttons}"


# ══════════════════════════════════════════════════════════════════════
# 2. TOPBAR / HEADER BUTONLARI
# ══════════════════════════════════════════════════════════════════════
class TestTopbarButtons:
    def test_refresh_button_exists(self, soup):
        """Yenile butonu bulunmalı."""
        btn = soup.find("button", string=re.compile("Yenile"))
        if not btn:
            # İçinde Yenile geçen buton
            btns = [b for b in soup.find_all("button") if "Yenile" in b.get_text()]
            btn = btns[0] if btns else None
        assert btn is not None, "↻ Yenile butonu bulunamadı"

    def test_quick_start_button_exists(self, soup):
        """Hızlı Başlat butonu bulunmalı."""
        btn = soup.find("button", {"id": "quickRunBtn"})
        assert btn is not None, "quickRunBtn butonu bulunamadı"

    def test_quick_start_button_onclick(self, html_content):
        """quickRunBtn onclick handler'ı quickRun() olmalı."""
        assert 'onclick="quickRun()"' in html_content

    def test_stop_button_exists(self, soup):
        """Durdur butonu bulunmalı."""
        btn = soup.find("button", {"id": "topStopBtn"})
        assert btn is not None, "topStopBtn butonu bulunamadı"

    def test_stop_button_onclick(self, html_content):
        """topStopBtn onclick handler'ı stopRun() olmalı."""
        assert 'onclick="stopRun()"' in html_content

    def test_stop_button_initially_hidden(self, soup):
        """Durdur butonu başlangıçta gizli olmalı."""
        btn = soup.find("button", {"id": "topStopBtn"})
        assert btn is not None
        classes = btn.get("class", [])
        assert "hidden" in classes, "topStopBtn başlangıçta hidden class'ına sahip olmalı"

    def test_refresh_button_onclick(self, html_content):
        """Refresh onclick refreshAll() çağırmalı."""
        assert "refreshAll()" in html_content


# ══════════════════════════════════════════════════════════════════════
# 3. NAVİGASYON MENU
# ══════════════════════════════════════════════════════════════════════
class TestNavigation:
    def test_nav_items_exist(self, soup):
        """Navigasyon elementleri bulunmalı."""
        nav_items = soup.find_all(class_="nav-item")
        assert len(nav_items) > 0, "nav-item elementleri bulunamadı"

    def test_nav_has_dashboard(self, soup):
        """Dashboard nav item'ı bulunmalı."""
        items = [el for el in soup.find_all(class_="nav-item") if "dashboard" in el.get_text().lower()]
        assert len(items) > 0, "Dashboard nav item bulunamadı"

    def test_nav_has_config(self, soup):
        """Konfigürasyon nav item'ı bulunmalı."""
        items = [el for el in soup.find_all(class_="nav-item") if "konfig" in el.get_text().lower()]
        assert len(items) > 0, "Konfigürasyon nav item bulunamadı"

    def test_nav_has_runner(self, soup):
        """Test Çalıştır nav item'ı bulunmalı."""
        items = [el for el in soup.find_all(class_="nav-item") if "test" in el.get_text().lower()]
        assert len(items) > 0, "Test Çalıştır nav item bulunamadı"

    def test_nav_has_security(self, soup):
        """Güvenlik nav item'ı bulunmalı."""
        items = [el for el in soup.find_all(class_="nav-item") if "güvenlik" in el.get_text().lower() or "security" in el.get("data-page", "")]
        assert len(items) > 0, "Güvenlik nav item bulunamadı"

    def test_nav_has_reports(self, soup):
        """Raporlar nav item'ı bulunmalı."""
        items = [el for el in soup.find_all(class_="nav-item") if "rapor" in el.get_text().lower()]
        assert len(items) > 0, "Raporlar nav item bulunamadı"

    def test_nav_items_have_data_page(self, soup):
        """Tüm nav-item'lar data-page attribute'a sahip olmalı."""
        nav_items = soup.find_all(class_="nav-item")
        missing = [str(el)[:60] for el in nav_items if not el.get("data-page")]
        assert len(missing) == 0, f"data-page eksik nav-item'lar: {missing}"

    def test_nav_page_count_minimum(self, soup):
        """En az 8 sayfa (tab) bulunmalı."""
        nav_items = soup.find_all(class_="nav-item")
        assert len(nav_items) >= 8, f"Yeterli nav item yok. Bulunan: {len(nav_items)}"

    def test_nav_first_item_is_active(self, soup):
        """İlk nav item active class'ına sahip olmalı."""
        nav_items = soup.find_all(class_="nav-item")
        assert len(nav_items) > 0
        first = nav_items[0]
        assert "active" in first.get("class", []), "İlk nav item active değil"


# ══════════════════════════════════════════════════════════════════════
# 4. CONFIG SAYFASI BUTONLARI
# ══════════════════════════════════════════════════════════════════════
class TestConfigPageButtons:
    def test_save_config_button_exists(self, html_content):
        """Kaydet butonu HTML'de bulunmalı."""
        assert "saveConfig()" in html_content, "saveConfig() onclick eksik"

    def test_reload_config_button_exists(self, html_content):
        """Yenile/refresh config butonu bulunmalı."""
        assert "loadConfig()" in html_content, "loadConfig() onclick eksik"

    def test_ems_tab_button_exists(self, soup):
        """EMS tab butonu bulunmalı."""
        tabs = [el for el in soup.find_all(class_="srv-tab") if "EMS" in el.get_text()]
        assert len(tabs) > 0, "EMS srv-tab bulunamadı"

    def test_gmail_tab_button_exists(self, soup):
        """Gmail tab butonu bulunmalı."""
        tabs = [el for el in soup.find_all(class_="srv-tab") if "Gmail" in el.get_text()]
        assert len(tabs) > 0, "Gmail srv-tab bulunamadı"

    def test_outlook_tab_button_exists(self, soup):
        """Outlook tab butonu bulunmalı."""
        tabs = [el for el in soup.find_all(class_="srv-tab") if "Outlook" in el.get_text()]
        assert len(tabs) > 0, "Outlook srv-tab bulunamadı"

    def test_ems_connection_test_button(self, html_content):
        """EMS bağlantı test butonu bulunmalı."""
        assert "testConn('ems')" in html_content

    def test_gmail_connection_test_button(self, html_content):
        """Gmail bağlantı test butonu bulunmalı."""
        assert "testConn('gmail')" in html_content

    def test_outlook_connection_test_button(self, html_content):
        """Outlook bağlantı test butonu bulunmalı."""
        assert "testConn('outlook')" in html_content

    def test_add_other_address_button(self, html_content):
        """Diğer adres ekle butonu bulunmalı."""
        assert "addOtherAddress()" in html_content


# ══════════════════════════════════════════════════════════════════════
# 5. GÜVENLİK & 2FA SAYFASI BUTONLARI
# ══════════════════════════════════════════════════════════════════════
class TestSecurityPageButtons:
    def test_security_test_button(self, html_content):
        """Güvenlik testi butonu bulunmalı."""
        assert "checkSecurity" in html_content

    def test_mfa_test_button(self, html_content):
        """2FA ile test butonu bulunmalı."""
        assert "testConnMFA" in html_content

    def test_totp_preview_button(self, html_content):
        """TOTP önizle butonu bulunmalı."""
        assert "previewTotp" in html_content

    def test_auth_cards_exist(self, soup):
        """Auth method card'ları bulunmalı."""
        auth_cards = soup.find_all(class_="auth-card")
        assert len(auth_cards) > 0, "auth-card elementleri bulunamadı"

    def test_auth_cards_have_onclick(self, html_content):
        """Auth card'ların onclick handler'ı bulunmalı."""
        assert "selectAuth(" in html_content

    def test_auth_method_grid_present(self, soup):
        """Auth method grid bulunmalı."""
        grid = soup.find("div", {"id": "authGrid-ems"})
        assert grid is not None, "authGrid-ems bulunamadı"

    def test_security_tab_ems(self, html_content):
        """EMS güvenlik tab'ı bulunmalı."""
        assert "switchSecSrv('ems'" in html_content

    def test_security_tab_gmail(self, html_content):
        """Gmail güvenlik tab'ı bulunmalı."""
        assert "switchSecSrv('gmail'" in html_content

    def test_security_tab_outlook(self, html_content):
        """Outlook güvenlik tab'ı bulunmalı."""
        assert "switchSecSrv('outlook'" in html_content


# ══════════════════════════════════════════════════════════════════════
# 6. TEST ÇALIŞTIRMA SAYFASI BUTONLARI
# ══════════════════════════════════════════════════════════════════════
class TestRunnerPageButtons:
    def test_start_run_function_exists(self, html_content):
        """startRun fonksiyon çağrısı bulunmalı."""
        assert "startRun" in html_content

    def test_stop_run_function_exists(self, html_content):
        """stopRun fonksiyon çağrısı bulunmalı."""
        assert "stopRun" in html_content

    def test_dry_run_option_exists(self, html_content):
        """Dry run opsiyonu bulunmalı."""
        assert "dry_run" in html_content or "dry-run" in html_content

    def test_load_combinations_function(self, html_content):
        """loadCombinations fonksiyon çağrısı bulunmalı."""
        assert "loadCombinations" in html_content

    def test_combo_cards_css_class(self, html_content):
        """combo-card CSS class'ı HTML'de bulunmalı."""
        assert "combo-card" in html_content

    def test_scenario_pills_css_class(self, html_content):
        """pill CSS class'ı HTML'de bulunmalı."""
        assert "pill" in html_content


# ══════════════════════════════════════════════════════════════════════
# 7. RAPORLAR SAYFASI BUTONLARI
# ══════════════════════════════════════════════════════════════════════
class TestReportsPageButtons:
    def test_reports_refresh_function(self, html_content):
        """Rapor yenileme fonksiyonu bulunmalı."""
        assert "loadReports" in html_content or "refreshReports" in html_content or "list_reports" in html_content

    def test_report_download_reference(self, html_content):
        """Rapor indirme referansı bulunmalı."""
        assert "/api/reports/" in html_content

    def test_latest_results_reference(self, html_content):
        """Son sonuçlar API referansı bulunmalı."""
        assert "/api/results/latest" in html_content


# ══════════════════════════════════════════════════════════════════════
# 8. ŞABLONLAR SAYFASI BUTONLARI
# ══════════════════════════════════════════════════════════════════════
class TestTemplatesPageButtons:
    def test_save_templates_function(self, html_content):
        """Şablon kaydetme fonksiyonu bulunmalı."""
        assert "saveTemplates" in html_content or "postTemplates" in html_content or "/api/templates" in html_content

    def test_export_defaults_function(self, html_content):
        """Varsayılanları dışa aktar fonksiyonu bulunmalı."""
        assert "export-defaults" in html_content or "exportDefaults" in html_content

    def test_attachment_upload_exists(self, html_content):
        """Dosya yükleme butonu/formu bulunmalı."""
        assert "upload" in html_content.lower()

    def test_template_page_div_exists(self, soup):
        """Şablonlar sayfası div'i bulunmalı."""
        page = soup.find("div", {"id": "page-templates"})
        assert page is not None, "page-templates div'i bulunamadı"

    def test_template_layout_css_class(self, html_content):
        """Şablon sayfası layout CSS class'ı bulunmalı."""
        assert "tmpl-layout" in html_content or "tmpl-nav-item" in html_content or "templates" in html_content.lower()


# ══════════════════════════════════════════════════════════════════════
# 9. MODAL BUTONLARI
# ══════════════════════════════════════════════════════════════════════
class TestModalButtons:
    def test_screenshot_modal_close_button(self, soup):
        """Screenshot modal kapatma butonu bulunmalı."""
        modal = soup.find("div", {"id": "screenshotModal"})
        assert modal is not None, "screenshotModal bulunamadı"
        close_btn = modal.find("button")
        assert close_btn is not None, "screenshotModal kapatma butonu bulunamadı"

    def test_visual_modal_close_button(self, html_content):
        """Görsel regresyon modalı kapatma butonu bulunmalı."""
        assert "closeVisualModal()" in html_content

    def test_set_baseline_button_exists(self, soup):
        """Baseline atama butonu bulunmalı."""
        btn = soup.find("button", {"id": "setBaselineBtn"})
        assert btn is not None, "setBaselineBtn bulunamadı"

    def test_set_baseline_button_onclick(self, html_content):
        """setBaselineBtn onclick setAsBaseline() çağırmalı."""
        assert "setAsBaseline()" in html_content

    def test_mfa_modal_submit_function(self, html_content):
        """MFA kodu gönderme fonksiyonu bulunmalı."""
        assert "submitMFA" in html_content or "mfa/submit" in html_content or "submitCode" in html_content

    def test_mfa_modal_cancel_function(self, html_content):
        """MFA iptal fonksiyonu bulunmalı."""
        assert "cancelMFA" in html_content or "mfa/cancel" in html_content or "mfaCancel" in html_content


# ══════════════════════════════════════════════════════════════════════
# 10. FORM ALANLARI
# ══════════════════════════════════════════════════════════════════════
class TestFormInputs:
    def test_ems_smtp_host_input_exists(self, soup):
        inp = soup.find("input", {"id": "ems_smtp_host"})
        assert inp is not None, "ems_smtp_host input bulunamadı"

    def test_ems_smtp_port_input_exists(self, soup):
        inp = soup.find("input", {"id": "ems_smtp_port"})
        assert inp is not None, "ems_smtp_port input bulunamadı"

    def test_gmail_username_input_exists(self, soup):
        inp = soup.find("input", {"id": "gmail_username"})
        assert inp is not None, "gmail_username input bulunamadı"

    def test_outlook_smtp_host_input_exists(self, soup):
        inp = soup.find("input", {"id": "outlook_smtp_host"})
        assert inp is not None, "outlook_smtp_host input bulunamadı"

    def test_llm_provider_select_exists(self, soup):
        sel = soup.find("select", {"id": "llm_provider"})
        assert sel is not None, "llm_provider select bulunamadı"

    def test_llm_provider_has_google_option(self, soup):
        sel = soup.find("select", {"id": "llm_provider"})
        if sel:
            options = sel.find_all("option")
            values = [o.get("value", "") for o in options]
            assert "google" in values, "google LLM provider opsiyonu bulunamadı"

    def test_llm_provider_has_claude_option(self, soup):
        sel = soup.find("select", {"id": "llm_provider"})
        if sel:
            options = sel.find_all("option")
            values = [o.get("value", "") for o in options]
            assert "claude" in values, "claude LLM provider opsiyonu bulunamadı"

    def test_test_wait_seconds_input(self, soup):
        inp = soup.find("input", {"id": "test_wait_seconds"})
        assert inp is not None, "test_wait_seconds input bulunamadı"

    def test_test_max_retries_input(self, soup):
        inp = soup.find("input", {"id": "test_max_retries"})
        assert inp is not None, "test_max_retries input bulunamadı"

    def test_google_api_key_input(self, soup):
        inp = soup.find("input", {"id": "google_api_key"})
        assert inp is not None, "google_api_key input bulunamadı"

    def test_password_inputs_are_type_password(self, soup):
        """Şifre alanları type='password' olmalı."""
        password_inputs = ["ems_password", "gmail_password", "outlook_password", "google_api_key"]
        for inp_id in password_inputs:
            inp = soup.find("input", {"id": inp_id})
            if inp:
                assert inp.get("type") == "password", f"{inp_id} type='password' değil"

    def test_checkbox_inputs_exist(self, soup):
        """TLS/SSL checkbox'ları bulunmalı."""
        tls_cb = soup.find("input", {"id": "ems_smtp_use_tls"})
        ssl_cb = soup.find("input", {"id": "ems_imap_use_ssl"})
        assert tls_cb is not None, "ems_smtp_use_tls checkbox bulunamadı"
        assert ssl_cb is not None, "ems_imap_use_ssl checkbox bulunamadı"

    def test_tls_checkbox_checked_by_default(self, soup):
        """TLS checkbox'u varsayılan olarak işaretli olmalı."""
        tls_cb = soup.find("input", {"id": "ems_smtp_use_tls"})
        if tls_cb:
            assert tls_cb.has_attr("checked"), "TLS checkbox varsayılan olarak checked değil"


# ══════════════════════════════════════════════════════════════════════
# 11. OTOMASYON SAYFASI BUTONLARI
# ══════════════════════════════════════════════════════════════════════
class TestAutomationPageButtons:
    def test_automation_save_function(self, html_content):
        """Otomasyon kaydı fonksiyonu bulunmalı."""
        assert "saveAutomation" in html_content or "automation/sync" in html_content

    def test_scheduler_sync_reference(self, html_content):
        """Zamanlayıcı senkronizasyon referansı bulunmalı."""
        assert "automation/sync" in html_content or "syncScheduler" in html_content


# ══════════════════════════════════════════════════════════════════════
# 12. API BUTON ENTEGRASYON TESTLERİ
# ══════════════════════════════════════════════════════════════════════
class TestButtonAPIIntegration:
    """Buton tıklamaları Flask API endpoint'lerini tetiklemeli."""

    def test_stop_run_api_endpoint_works(self, client):
        """/api/run/stop butonu arkasındaki endpoint çalışmali."""
        resp = client.post("/api/run/stop")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_reset_run_api_endpoint_works(self, client):
        """/api/run/reset butonu arkasındaki endpoint çalışmalı."""
        resp = client.post("/api/run/reset")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_get_templates_api_endpoint_works(self, client):
        """Şablonlar sayfası yüklendiğinde /api/templates çalışmalı."""
        resp = client.get("/api/templates")
        assert resp.status_code == 200

    def test_get_combinations_api_endpoint_works(self, client):
        """Test matrisi yüklendiğinde /api/combinations çalışmalı."""
        resp = client.get("/api/combinations")
        assert resp.status_code == 200

    def test_get_attachments_api_endpoint_works(self, client):
        """Ekler sayfası yüklendiğinde /api/attachments çalışmalı."""
        resp = client.get("/api/attachments")
        assert resp.status_code == 200

    def test_mfa_status_api_endpoint_works(self, client):
        """MFA durum kontrolü çalışmalı."""
        resp = client.get("/api/mfa/status")
        assert resp.status_code == 200

    def test_verification_pending_api_endpoint_works(self, client):
        """Doğrulama kontrol butonu endpoint'i çalışmalı."""
        resp = client.get("/api/verification/pending")
        assert resp.status_code == 200

    def test_save_config_api_endpoint_works(self, client):
        """Kaydet butonu endpoint'i çalışmalı."""
        resp = client.post(
            "/api/config",
            data=json.dumps({"config": {}}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_reports_list_api_endpoint_works(self, client):
        """Raporlar listesi endpoint'i çalışmalı."""
        resp = client.get("/api/reports")
        assert resp.status_code == 200

    def test_export_defaults_api_endpoint_works(self, client):
        """Varsayılan şablonları aktar butonu endpoint'i çalışmalı."""
        resp = client.post("/api/templates/export-defaults")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════
# 13. JAVASCRIPT FONKSİYON VARLIK TESTLERİ
# ══════════════════════════════════════════════════════════════════════
class TestJavaScriptFunctions:
    """HTML'de buton onclick'lerinde çağrılan JS fonksiyonlarının tanımlandığını doğrula."""

    # Kritik JS fonksiyonları
    EXPECTED_FUNCTIONS = [
        "function quickRun",
        "function stopRun",
        "function refreshAll",
        "function saveConfig",
        "function loadConfig",
        "function testConn",
        "function switchSrv",
        "function loadCombinations",
        "function loadReports",
    ]

    @pytest.mark.parametrize("func_name", EXPECTED_FUNCTIONS)
    def test_js_function_defined(self, html_content, func_name):
        """Her kritik JS fonksiyonu HTML içinde tanımlanmış olmalı."""
        assert func_name in html_content, f"JS fonksiyonu tanımlı değil: {func_name}"

    def test_no_undefined_onclick_handlers(self, html_content):
        """HTML onclick'lerinde kullanılan fonksiyonlar tanımlı olmalı."""
        # onclick'teki fonksiyon adlarını çıkar
        onclick_pattern = re.compile(r'onclick="(\w+)\(')
        onclick_funcs = set(onclick_pattern.findall(html_content))
        
        # Bunlar tarayıcı built-in'ler, test etmemize gerek yok
        browser_builtins = {"window", "document", "event", "this"}
        
        missing = []
        for func in onclick_funcs:
            if func not in browser_builtins:
                # Fonksiyon tanımını ara
                if f"function {func}" not in html_content:
                    missing.append(func)
        
        assert len(missing) == 0, f"Tanımsız onclick fonksiyonları: {missing}"


# ══════════════════════════════════════════════════════════════════════
# 14. ERİŞİLEBİLİRLİK (ACCESSIBILITY) BUTON TESTLERİ
# ══════════════════════════════════════════════════════════════════════
class TestButtonAccessibility:
    def test_buttons_not_disabled_by_default(self, soup):
        """Çoğu buton başlangıçta disabled olmamalı."""
        buttons = soup.find_all("button")
        disabled_btns = [b for b in buttons if b.has_attr("disabled")]
        # Bazı butonlar disabled başlayabilir ama çoğunluğu değil
        total = len(buttons)
        disabled_count = len(disabled_btns)
        assert disabled_count < total * 0.5, \
            f"Butonların yarısından fazlası disabled: {disabled_count}/{total}"

    def test_critical_pages_exist(self, soup):
        """Tüm kritik sayfa div'leri HTML'de bulunmalı."""
        pages = [
            "page-dashboard", "page-config", "page-security",
            "page-runner", "page-logs", "page-results",
            "page-reports", "page-templates", "page-automation", "page-guide"
        ]
        for page_id in pages:
            el = soup.find(id=page_id)
            assert el is not None, f"Sayfa div'i bulunamadı: #{page_id}"

    def test_sidebar_has_footer(self, soup):
        """Sidebar footer bulunmalı."""
        footer = soup.find(class_="sidebar-footer")
        assert footer is not None, "sidebar-footer bulunamadı"

    def test_status_dot_exists(self, soup):
        """Durum göstergesi (status-dot) bulunmalı."""
        dot = soup.find("span", {"id": "sidebarDot"})
        assert dot is not None, "sidebarDot bulunamadı"
