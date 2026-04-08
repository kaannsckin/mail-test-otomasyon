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

    def compare_screenshots(self, baseline_path: str, current_path: str, diff_path: str) -> dict:
        """
        İki görseli pixel bazlı karşılaştırır.
        Farkları diff_path hedefine kaydeder.
        """
        try:
            from PIL import Image, ImageChops
            import numpy as np

            if not os.path.exists(baseline_path):
                return {"error": "Baseline bulunamadı", "diff_percent": 0.0}
            
            img1 = Image.open(baseline_path).convert("RGB")
            img2 = Image.open(current_path).convert("RGB")
            
            # Boyutları eşitle (canvas extension)
            max_w = max(img1.size[0], img2.size[0])
            max_h = max(img1.size[1], img2.size[1])
            
            def pad_image(img, w, h):
                new_img = Image.new("RGB", (w, h), (255, 255, 255))
                new_img.paste(img, (0, 0))
                return new_img
            
            img1 = pad_image(img1, max_w, max_h)
            img2 = pad_image(img2, max_w, max_h)
            
            # Farkı bul (Heatmap)
            diff = ImageChops.difference(img1, img2)
            
            # Fark yüzdesini hesapla
            diff_arr = np.array(diff)
            non_zero_pixels = np.count_nonzero(diff_arr)
            total_elements = diff_arr.size
            diff_percent = (non_zero_pixels / total_elements) * 100
            
            # Görsel farkları kırmızı ile işaretle
            if diff_percent > 0:
                # Kırmızı bir maske ekleyerek farkları belirginleştir
                mask = diff.convert("L").point(lambda x: 255 if x > 10 else 0)
                red_diff = Image.new("RGB", (max_w, max_h), (255, 0, 0))
                overlay = Image.composite(red_diff, img1, mask)
                overlay.save(diff_path)
            
            logger.info(f"Görsel regresyon tamamlandı: {diff_percent:.2f}% fark.")
            return {
                "ok": True,
                "diff_percent": round(diff_percent, 4),
                "is_match": diff_percent < 0.5, # %0.5 tolerans
                "max_w": max_w,
                "max_h": max_h
            }
        except Exception as e:
            logger.error(f"Görsel karşılaştırma hatası: {e}")
            return {"error": str(e), "diff_percent": 0.0}
