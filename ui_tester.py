import os
import logging
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

class UITester:
    """
    E-postaları headless (görünmez) bir tarayıcıda (Chromium) render edip
    ekran görüntülerini alarak UI / DOM bazlı testleri gerçekleştirir.
    """
    
    def __init__(self, headless: bool = True):
        self.headless = headless

    def take_screenshot_from_html(self, html_content: str, output_path: str, width: int = 800, height: int = 1200) -> bool:
        """
        Verilen HTML içeriğini tarayıcıda render eder ve output_path hedefine screenshot kaydeder.
        """
        try:
            # Dizin yoksa oluştur
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()
                
                # HTML içeriğini doğrudan sayfaya göm
                page.set_content(html_content, wait_until="networkidle")
                
                # Full page screenshot al
                page.screenshot(path=output_path, full_page=True)
                
                browser.close()
                logger.info(f"UI Screenshot baseline alındı: {output_path}")
                return True
        except Exception as e:
            logger.error(f"Playwright screenshot hatası: {e}")
            return False
