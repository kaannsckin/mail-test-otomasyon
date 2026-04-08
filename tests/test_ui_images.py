"""
test_ui_images.py — Frontend HTML şablonundaki img tag'larinin,
asset dosyalarının ve görsel kaynaklarının doğruluğunu test eder.

Test Kategorileri:
  - HTML'deki img elementleri ve src attribute'ları
  - Statik asset dosyalarının varlığı
  - Favicon/logo/ikon dosyaları
  - Görsel regresyon screenshot dosyaları
  - Chart.js CDN kaynağı uyumluluğu
  - logo-icon ve emoji ikonlarının varlığı
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Proje root
PROJECT_ROOT = Path(__file__).parent.parent
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "index.html"
ASSETS_DIR = PROJECT_ROOT / "assets"
TEST_FILES_DIR = PROJECT_ROOT / "test_files"


# ── Fixtures ───────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def html_content():
    """index.html içeriğini döner."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def soup(html_content):
    """BeautifulSoup nesnesi döner."""
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
# 1. HTML ŞABLONU VARLIK TESTLERİ
# ══════════════════════════════════════════════════════════════════════
class TestTemplateFile:
    def test_template_file_exists(self):
        """index.html dosyası bulunmalı."""
        assert TEMPLATE_PATH.exists(), f"Template bulunamadı: {TEMPLATE_PATH}"

    def test_template_file_not_empty(self):
        """index.html boş olmamalı."""
        assert TEMPLATE_PATH.stat().st_size > 0

    def test_template_is_valid_html(self, html_content):
        """Temel HTML yapısı geçerli olmalı."""
        assert "<!DOCTYPE html>" in html_content or "<!doctype html>" in html_content.lower()
        assert "<html" in html_content
        assert "</html>" in html_content

    def test_template_has_head_and_body(self, html_content):
        assert "<head>" in html_content
        assert "<body>" in html_content
        assert "</head>" in html_content
        assert "</body>" in html_content

    def test_template_has_meta_charset(self, html_content):
        assert 'charset="UTF-8"' in html_content or 'charset="utf-8"' in html_content.lower()

    def test_template_has_title(self, html_content):
        assert "<title>" in html_content
        assert "Mail Test Otomasyon" in html_content


# ══════════════════════════════════════════════════════════════════════
# 2. IMG ELEMENTLERI TESTLERİ
# ══════════════════════════════════════════════════════════════════════
class TestImgElements:
    def test_img_elements_exist(self, soup):
        """HTML'de img elementleri bulunmalı."""
        imgs = soup.find_all("img")
        assert len(imgs) > 0, "Hiç img elementi bulunamadı"

    def test_screenshot_image_has_id(self, soup):
        """screenshotImage id'li img elementi bulunmalı."""
        img = soup.find("img", {"id": "screenshotImage"})
        assert img is not None, "screenshotImage img elementi bulunamadı"

    def test_baseline_img_has_id(self, soup):
        """baselineImg id'li img elementi bulunmalı (görsel regresyon)."""
        img = soup.find("img", {"id": "baselineImg"})
        assert img is not None, "baselineImg img elementi bulunamadı"

    def test_current_img_has_id(self, soup):
        """currentImg id'li img elementi bulunmalı."""
        img = soup.find("img", {"id": "currentImg"})
        assert img is not None, "currentImg img elementi bulunamadı"

    def test_diff_img_has_id(self, soup):
        """diffImg id'li img elementi bulunmalı."""
        img = soup.find("img", {"id": "diffImg"})
        assert img is not None, "diffImg img elementi bulunamadı"

    def test_baseline_img_has_alt(self, soup):
        """baselineImg alt attribute'a sahip olmalı."""
        img = soup.find("img", {"id": "baselineImg"})
        assert img is not None
        assert img.get("alt"), "baselineImg alt attribute eksik"

    def test_current_img_has_alt(self, soup):
        """currentImg alt attribute'a sahip olmalı."""
        img = soup.find("img", {"id": "currentImg"})
        assert img is not None
        assert img.get("alt"), "currentImg alt attribute eksik"

    def test_diff_img_has_alt(self, soup):
        """diffImg alt attribute'a sahip olmalı."""
        img = soup.find("img", {"id": "diffImg"})
        assert img is not None
        assert img.get("alt"), "diffImg alt attribute eksik"

    def test_no_broken_static_src(self, html_content):
        """img src değerleri external (http/https) veya boş olmalı — bozuk static path yok."""
        # data: veya http/https/boş src dışında /static/ benzeri sabit kırık path'ler olmamalı
        img_srcs = re.findall(r'<img[^>]+src=["\']([^"\']*)["\']', html_content, re.IGNORECASE)
        broken = []
        for src in img_srcs:
            if src and not src.startswith(("http", "data:", "//", "{{", "/api/")):
                # Boş src dinamik JS ile set ediliyor, bu normal
                if src.strip():
                    broken.append(src)
        assert len(broken) == 0, f"Potansiyel kırık statik img src'ler bulundu: {broken}"


# ══════════════════════════════════════════════════════════════════════
# 3. STATIK VARLIK DOSYALARI
# ══════════════════════════════════════════════════════════════════════
class TestStaticAssets:
    def test_assets_dir_exists(self):
        """assets/ dizini bulunmalı."""
        assert ASSETS_DIR.exists(), f"assets/ dizini bulunamadı: {ASSETS_DIR}"

    def test_safir_logo_exists(self):
        """safir_logo.png asset dosyası bulunmalı."""
        logo_path = ASSETS_DIR / "safir_logo.png"
        assert logo_path.exists(), f"safir_logo.png bulunamadı: {logo_path}"

    def test_safir_logo_not_empty(self):
        """safir_logo.png boş olmamalı."""
        logo_path = ASSETS_DIR / "safir_logo.png"
        if logo_path.exists():
            assert logo_path.stat().st_size > 0, "safir_logo.png boş dosya"

    def test_safir_logo_is_valid_png(self):
        """safir_logo.png geçerli PNG magic bytes ile başlamalı."""
        logo_path = ASSETS_DIR / "safir_logo.png"
        if logo_path.exists():
            with open(logo_path, "rb") as f:
                header = f.read(8)
            png_signature = b"\x89PNG\r\n\x1a\n"
            assert header == png_signature, "safir_logo.png geçerli bir PNG dosyası değil"

    def test_safir_logo_size_reasonable(self):
        """safir_logo.png makul bir boyutta olmalı (100B - 5MB arası)."""
        logo_path = ASSETS_DIR / "safir_logo.png"
        if logo_path.exists():
            size = logo_path.stat().st_size
            assert 100 <= size <= 5_000_000, f"safir_logo.png boyutu anormal: {size} bytes"


# ══════════════════════════════════════════════════════════════════════
# 4. TEST DOSYALARI DİZİNİ
# ══════════════════════════════════════════════════════════════════════
class TestTestFiles:
    def test_test_files_dir_exists(self):
        """test_files/ dizini bulunmalı."""
        assert TEST_FILES_DIR.exists(), f"test_files/ dizini bulunamadı"

    def test_test_files_dir_not_empty(self):
        """test_files/ dizini en az bir dosya içermeli."""
        if TEST_FILES_DIR.exists():
            files = list(TEST_FILES_DIR.iterdir())
            assert len(files) > 0, "test_files/ dizini boş"

    def test_test_document_pdf_exists(self):
        """test_document.pdf dosyası bulunmalı."""
        pdf_path = TEST_FILES_DIR / "test_document.pdf"
        if not pdf_path.exists():
            pytest.skip("test_document.pdf henüz oluşturulmamış (opsiyonel)")
        assert pdf_path.stat().st_size > 0

    def test_test_image_png_exists(self):
        """test_image.png dosyası bulunmalı."""
        img_path = TEST_FILES_DIR / "test_image.png"
        if not img_path.exists():
            pytest.skip("test_image.png henüz oluşturulmamış (opsiyonel)")
        assert img_path.stat().st_size > 0


# ══════════════════════════════════════════════════════════════════════
# 5. EXTERNAL KAYNAKLAR (CDN)
# ══════════════════════════════════════════════════════════════════════
class TestExternalResources:
    def test_google_fonts_linked(self, html_content):
        """Google Fonts CSS link'i bulunmalı."""
        assert "fonts.googleapis.com" in html_content

    def test_chartjs_script_referenced(self, html_content):
        """Chart.js CDN script tag'i bulunmalı."""
        assert "chart.js" in html_content.lower() or "Chart.js" in html_content

    def test_jetbrains_mono_font(self, html_content):
        """JetBrains Mono font'u referans edilmeli."""
        assert "JetBrains+Mono" in html_content or "JetBrains Mono" in html_content

    def test_syne_font(self, html_content):
        """Syne font'u referans edilmeli."""
        assert "Syne" in html_content


# ══════════════════════════════════════════════════════════════════════
# 6. FLASK ÜZERINDEN GÖRSEL ENDPOINT TESTLERİ
# ══════════════════════════════════════════════════════════════════════
class TestVisualEndpointsViaFlask:
    def test_report_images_404_without_file(self, client):
        """Olmayan rapor dosyasına istek 404 döndürmeli."""
        resp = client.get("/api/reports/imaginary_report.html")
        assert resp.status_code == 404

    def test_diff_info_bad_path_returns_404(self, client):
        """Olmayan diff dosyasına istek 404 döndürmeli."""
        resp = client.get("/api/visual/diff-info?path=/tmp/nonexistent.png")
        assert resp.status_code == 404

    def test_index_serves_html_with_img_tags(self, client):
        """Ana sayfa img tag içerdiğini doğrula."""
        resp = client.get("/")
        assert b"<img" in resp.data

    def test_index_serves_html_with_canvas(self, client):
        """Chart canvas elementi HTML'de bulunmalı."""
        resp = client.get("/")
        assert b"<canvas" in resp.data


# ══════════════════════════════════════════════════════════════════════
# 7. LOGO VE EMOJI İKONLARI
# ══════════════════════════════════════════════════════════════════════
class TestLogoAndIcons:
    def test_logo_emoji_present(self, html_content):
        """📧 logo emoji HTML'de bulunmalı."""
        assert "📧" in html_content

    def test_sidebar_nav_icons_present(self, html_content):
        """Sidebar navigasyon ikonları HTML'de bulunmalı."""
        # Ana navigasyon ikonları
        nav_icons = ["⬡", "⚙", "🔐", "▶", "≡", "◈", "⬖", "📝", "☁", "📖"]
        for icon in nav_icons:
            assert icon in html_content, f"Nav ikonu eksik: {icon}"

    def test_canvas_chart_element_present(self, html_content):
        """chartHistory canvas elementi HTML'de bulunmalı."""
        assert 'id="chartHistory"' in html_content

    def test_logo_title_text_present(self, html_content):
        """Logo başlık metni HTML'de bulunmalı."""
        assert "Mail" in html_content
        assert "Otomasyon" in html_content
