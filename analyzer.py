"""
analyzer.py — LLM (Claude API veya Google Gemini) ile MIME içeriğini analiz eder.
Her senaryo tipi için özelleştirilmiş prompt'lar kullanır.

Mode:
  active  — Otomatik LLM analizi (API key gerekli)
  passive — Manuel doğrulama (CLI interactive veya App-driven dosya IPC)
"""

import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

# Lazy import — sadece aktif modda (LLM gerektiğinde) yüklenir.
# Pasif / dry-run modda llm_provider modülü import edilmez.
try:
    from llm_provider import create_provider, LLMProvider
except ImportError:
    create_provider = None   # type: ignore
    LLMProvider = object     # type: ignore

logger = logging.getLogger(__name__)

VERIFICATION_DIR = Path(".verifications")


class MailAnalyzer:
    def __init__(self, provider: Optional[LLMProvider] = None, mode: str = "active"):
        """
        Args:
            provider: LLMProvider instance (aktif modda zorunlu, pasif modda None olabilir)
            mode: "active" (otomatik analiz) veya "passive" (manuel doğrulama)
        """
        self.provider = provider
        self.mode = mode.lower().strip()
        if self.mode not in ("active", "passive", "auto"):
            raise ValueError(f"Geçersiz mode: {mode}. 'active', 'passive' veya 'auto' kullanın.")
        if self.mode == "active" and provider is None:
            raise ValueError("Active modda LLM provider zorunludur.")

    def analyze(self, scenario_type: str, send_meta: dict,
                received_msg: Optional[dict], combination: dict) -> dict:
        """
        Ana analiz metodu. Mode'a göre LLM veya manuel doğrulama yapar.

        Active: LLM'nin analiz sonucu döndürür
        Passive: Tester manual doğrulama yapar (CLI veya App üzerinden)

        Returns: {passed: bool, checks: [...], summary: str, confidence: str}
        """
        if received_msg is None:
            if self.mode == "passive":
                # Pasif modda bile kullanıcıya göster — mail gelmedi bilgisiyle
                dummy_meta = dict(send_meta)
                dummy_meta["subject"] = send_meta.get("subject", "")
                return self._manual_verify(
                    scenario_type,
                    send_meta,
                    None,
                    combination,
                    delivery_failed=True
                )
            return {
                "passed": False,
                "checks": [{"name": "Mesaj Alımı", "passed": False,
                             "detail": "Mesaj belirlenen sürede alınamadı (timeout)"}],
                "summary": "Mesaj teslim edilemedi — timeout.",
                "confidence": "HIGH",
            }

        if self.mode == "active":
            prompt = self._build_prompt(scenario_type, send_meta, received_msg, combination)
            response = self._call_provider(prompt)
            return self._parse_response(response, scenario_type)
        elif self.mode == "auto":
            return self._auto_verify(scenario_type, send_meta, received_msg, combination)
        else:
            return self._manual_verify(scenario_type, send_meta, received_msg, combination)

    # ------------------------------------------------------------------ #
    #  Prompt Builder
    # ------------------------------------------------------------------ #
    def _build_prompt(self, scenario_type: str, send_meta: dict,
                      received_msg: dict, combination: dict) -> str:
        combo_str = (
            f"Gönderen Sunucu: {combination['sender_server']} | "
            f"Gönderen İstemci: {combination['sender_client']} | "
            f"Alan Sunucu: {combination['receiver_server']} | "
            f"Alan İstemci: {combination['receiver_client']}"
        )

        def _safe_json(obj):
            """bytes içeren nesneleri JSON-güvenli hale getir."""
            if isinstance(obj, bytes):
                return f"<bytes len={len(obj)}>"
            if isinstance(obj, dict):
                return {k: _safe_json(v) for k, v in obj.items() if k != "data"}
            if isinstance(obj, list):
                return [_safe_json(i) for i in obj]
            return obj

        headers_str = json.dumps(_safe_json(received_msg["headers"]), ensure_ascii=False, indent=2)
        parts_str = json.dumps(_safe_json(received_msg["parts"][:5]), ensure_ascii=False, indent=2)
        attachments_str = json.dumps(_safe_json(received_msg["attachments"]), ensure_ascii=False, indent=2)
        inline_str = json.dumps(_safe_json(received_msg["inline_images"]), ensure_ascii=False, indent=2)
        send_meta_str = json.dumps({k: v for k, v in send_meta.items() if k not in ("raw_bytes", "data")},
                                   ensure_ascii=False, indent=2)

        scenario_checks = {
            "plain_text": """
Kontrol edilecekler:
1. Mesaj içeriği bozulmadan iletildi mi? (UTF-8 Türkçe karakterler: ğüşıöçĞÜŞİÖÇ)
2. Karakter seti / encoding doğru mu? (charset=utf-8 bekleniyor)
3. Header bilgileri tam mı? (From, To, Subject alanları dolu mu)
4. Content-Type doğru mu? (text/plain; charset=utf-8 bekleniyor)
5. Mesaj boyutu mantıklı mı? (çok küçük = içerik kaybolmuş olabilir)
""",
            "attachment": f"""
Gönderilen ek bilgisi: {send_meta_str}

Kontrol edilecekler:
1. Eklenti mesajda var mı? (attachments listesi boş olmamalı)
2. Eklenti dosya adı korunmuş mu? (orijinal: {send_meta.get('attachment_name', '?')})
3. Eklenti boyutu korunmuş mu? (orijinal: {send_meta.get('attachment_size', '?')} byte)
4. MIME type doğru mu? (application/pdf, image/png vb.)
5. Content-Transfer-Encoding base64 mı? (büyük dosyalar için beklenen)
""",
            "inline_image": f"""
Gönderilen inline resim CID: {send_meta.get('cid', '?')}

Kontrol edilecekler:
1. Inline image mesajda var mı? (inline_images listesi boş olmamalı)
2. CID referansı ({send_meta.get('cid', '?')}) doğru çözümlenmiş mi?
3. HTML yapısı korunmuş mu? (text/html part mevcut olmalı, <img> tag içermeli)
4. Resim bozulma / kayıp var mı? (inline_images[0].size > 0 olmalı)
5. multipart/related structure doğru mu?
""",
            "smime": """
Kontrol edilecekler:
1. Content-Type: multipart/signed veya application/pkcs7-mime var mı?
2. İmza (smime.p7s) eki var mı?
3. Sertifika zinciri bilgisi mevcut mu?
4. İmza kaldırılmadan iletildi mi? (attachments içinde smime.p7s olmalı)
5. Orijinal mesaj içeriği bütün mü?
""",
            "reply_chain": f"""
Orijinal mesaj ID: {send_meta.get('in_reply_to', '?')}

Kontrol edilecekler:
1. In-Reply-To header'ı doğru mu? (beklenen: {send_meta.get('in_reply_to', '?')})
2. References header'ı mevcut ve dolu mu?
3. Subject "Re:" prefixi ile başlıyor mu?
4. Alıntı (quote) bölümü ">" ile işaretlenmiş mi?
5. Encoding farklılıklarında karakter bozulması var mı?
""",
            "calendar_invite": """
Kontrol edilecekler:
1. Content-Type 'text/calendar' var mı?
2. METHOD:REQUEST olarak seçilmiş mi ve vCalendar başlangıç/bitiş tagleri düzgün pars edilmiş mi?
3. Mesaj gövdesinde SUMMARY, DTSTART gibi takvim objeleri bozulmadan iletilmiş mi?
""",
            "i18n": """
Kontrol edilecekler:
1. Subject başlığındaki (ÖÇŞĞÜİ, Arapça harfler, Kanji, Emojiler) düzgün bir şekilde parse edilmiş ve okunabilir yapıda mı? (=?UTF-8?Q? vb. decoder edilmiş mi?)
2. E-posta gövdesindeki Unicode karakterler bozulmuş mu? (mojibake tespiti yap)
""",
            "complex_html": """
Kontrol edilecekler:
1. Gelen e-posta gövdesinde kompleks HTML yapıları (div, style tag'i, media query objeleri) silinmiş mi veya bozulmuş mu?
2. Eğer "screenshot" alanı mevcut ise, görseldeki renderin bütünlüğü hakkında çıkarımda bulun (arka plan renkleri işlenmiş mi vb.)
""",
            "forward": """
Kontrol edilecekler:
1. "Fwd:" ile başlayan Forward Subject string'i var mı?
2. Gövdede "---------- Forwarded message ---------" silsilesi ve orijinal gönderici (From, Date, Subject, To) izolasyonu başarılı mı?
"""
        }

        checks = scenario_checks.get(scenario_type, "Genel mesaj iletim kontrolü yap.")
        
        receiver_client = combination.get('receiver_client', '').lower()
        sender_client = combination.get('sender_client', '').lower()
        
        extra_rules = ""
        if "apple" in receiver_client or "ios" in receiver_client:
            extra_rules += "\n- DİKKAT (Apple Mail Alıcısı): Apple Mail bazen CID resimleri ve inline HTML yapısını bozup içeriği ek (attachment) olarak algılayabilir. Bunu kesinlikle HATA/BAŞARISIZ olarak değerlendirme. Apple için bu normal bir render davranışıdır."
        if "outlook" in sender_client or "exchange" in sender_client:
            extra_rules += "\n- DİKKAT (Outlook Göndericisi): E-postanın içinde VML/XML taglarının (<!--[if mso]>) gömülü olduğunu teyit et. Eğer MSO blokları yoksa ama testte gönderici 'Outlook' diyorsa bunu uyumsuzluk/spoofing hatası say."

        checks += extra_rules

        return f"""Sen bir mail protokolü test uzmanısın. Aşağıdaki MIME verisini analiz et ve her kontrol noktasını değerlendir.

## Test Kombinasyonu
{combo_str}

## Senaryo Tipi
{scenario_type}

## Gönderim Metadata
{send_meta_str}

## Alınan Mesaj Headers
{headers_str}

## Mesaj Parts (ilk 5)
{parts_str}

## Eklentiler
{attachments_str}

## Inline Görseller
{inline_str}

## Kontrol Noktaları
{checks}

## Yanıt Formatı
Aşağıdaki JSON formatında yanıt ver, başka hiçbir şey yazma:

{{
  "passed": true/false,
  "confidence": "HIGH/MEDIUM/LOW",
  "checks": [
    {{"name": "Kontrol adı", "passed": true/false, "detail": "Kısa açıklama"}},
    ...
  ],
  "summary": "Genel sonuç özeti (1-2 cümle)",
  "issues": ["varsa bulunan sorunlar listesi"],
  "recommendations": ["varsa öneri listesi"]
}}

Sadece JSON döndür, markdown veya açıklama ekleme."""

    # ------------------------------------------------------------------ #
    #  LLM Provider çağrısı
    # ------------------------------------------------------------------ #
    def _call_provider(self, prompt: str) -> str:
        """LLM provider'ı çağır."""
        return self.provider.analyze(prompt)

    # ------------------------------------------------------------------ #
    #  Manuel Doğrulama (Pasif Mode) — Hibrit: CLI + App IPC
    # ------------------------------------------------------------------ #
    def _manual_verify(self, scenario_type: str, send_meta: dict,
                       received_msg, combination: dict,
                       delivery_failed: bool = False) -> dict:
        """
        Pasif modda doğrulama.
        - Terminal (stdin.isatty): CLI interactive input
        - Subprocess (app-driven): Dosya-tabanlı IPC ile bekleme
        """
        headers = received_msg.get("headers", {}) if received_msg else {}
        subject = headers.get("subject", headers.get("Subject", send_meta.get("subject", "?")))
        from_addr = headers.get("from", headers.get("From", send_meta.get("from_addr", "?")))
        date_str = headers.get("date", headers.get("Date", "?"))

        sender_server   = combination.get("sender_server", "?")
        receiver_server = combination.get("receiver_server", "?")
        label = combination.get("label") or f"{receiver_server} ← {sender_server}"

        test_info = {
            "scenario": scenario_type,
            "label": label,
            "from": from_addr,
            "subject": subject,
            "date": date_str,
            "sender_server": sender_server,
            "receiver_server": receiver_server,
            "attachments": len(received_msg.get("attachments", [])) if received_msg else 0,
            "inline_images": len(received_msg.get("inline_images", [])) if received_msg else 0,
            "delivery_failed": delivery_failed,
        }

        # App'ten subprocess olarak çalıştırılıyor mu?
        # app.py MAIL_AUTO_APP_MODE=1 set eder — isatty() güvenilmez.
        import os as _os
        if _os.environ.get("MAIL_AUTO_APP_MODE") == "1":
            return self._app_verify(test_info)
        elif sys.stdin.isatty():
            return self._cli_verify(test_info)
        else:
            return self._app_verify(test_info)

    def _cli_verify(self, test_info: dict) -> dict:
        """Terminal'den interaktif yes/no girdi al."""
        print("\n" + "=" * 70)
        print("PASIF MOD — Manuel Dogrulama")
        print("=" * 70)
        print(f"Senaryo   : {test_info['scenario']}")
        print(f"Test      : {test_info['label']}")
        print(f"Gonderen  : {test_info['from']}")
        print(f"Konu      : {test_info['subject']}")
        print(f"Alinan    : {test_info['date']}")
        print(f"Ekler     : {test_info['attachments']} dosya")
        print("-" * 70)

        passed = False
        while True:
            try:
                response = input("\nTest basarili mi? (y/n): ").strip().lower()
                if response in ("y", "yes", "evet"):
                    passed = True
                    break
                elif response in ("n", "no", "hayir"):
                    passed = False
                    break
                else:
                    print("Lutfen 'y' veya 'n' girin.")
            except (KeyboardInterrupt, EOFError):
                print("\nIptal edildi.")
                passed = False
                break

        issues = []
        if not passed:
            try:
                detail = input("  Sorun nedir? (opsiyonel, Enter ile gecin): ").strip()
                if detail:
                    issues.append(detail)
            except (KeyboardInterrupt, EOFError):
                pass

        return self._build_manual_result(passed, issues, "cli")

    def _app_verify(self, test_info: dict) -> dict:
        """
        App-driven dosya-tabanlı IPC.
        1. .verifications/pending_<id>.json yaz
        2. .verifications/response_<id>.json bekle (polling)
        3. Response'u oku ve döndür
        """
        VERIFICATION_DIR.mkdir(exist_ok=True)
        verify_id = uuid.uuid4().hex[:8]

        pending_file = VERIFICATION_DIR / f"pending_{verify_id}.json"
        response_file = VERIFICATION_DIR / f"response_{verify_id}.json"

        # Pending yaz — app bu dosyayı okuyup UI'da gösterecek
        pending_data = {
            "id": verify_id,
            "timestamp": time.time(),
            **test_info,
        }
        with open(pending_file, "w", encoding="utf-8") as f:
            json.dump(pending_data, f, ensure_ascii=False, indent=2)

        logger.info(f"⏳ Manuel doğrulama bekleniyor: {verify_id} ({test_info['label']})")

        # Response bekle (polling) — max 10 dakika timeout
        timeout = 600
        start = time.time()
        while time.time() - start < timeout:
            if response_file.exists():
                try:
                    with open(response_file, "r", encoding="utf-8") as f:
                        resp = json.load(f)
                    # Temizle
                    pending_file.unlink(missing_ok=True)
                    response_file.unlink(missing_ok=True)

                    passed = resp.get("approved", False)
                    issues = resp.get("issues", [])
                    logger.info(f"{'✅' if passed else '❌'} Manuel doğrulama: {verify_id} → {'PASS' if passed else 'FAIL'}")
                    return self._build_manual_result(passed, issues, "app")
                except (json.JSONDecodeError, IOError) as e:
                    logger.warning(f"Response dosyası okunamadı: {e}")
            time.sleep(1)

        # Timeout
        pending_file.unlink(missing_ok=True)
        logger.warning(f"⏰ Manuel doğrulama timeout: {verify_id}")
        return self._build_manual_result(False, ["Manuel doğrulama zaman aşımına uğradı (10dk)"], "timeout")

    @staticmethod
    def _build_manual_result(passed: bool, issues: list, source: str) -> dict:
        """Standart manuel doğrulama sonucu oluştur."""
        return {
            "passed": passed,
            "confidence": "MANUAL",
            "checks": [
                {
                    "name": "Manuel Doğrulama",
                    "passed": passed,
                    "detail": f"Tester tarafından kontrol edildi ({source})"
                }
            ],
            "summary": f"Tester tarafından {'onaylandı' if passed else 'reddedildi'} ({source}).",
            "issues": issues,
            "recommendations": [],
            "verification_mode": "manual",
            "verification_source": source,
        }

    # ------------------------------------------------------------------ #
    #  Response parser
    # ------------------------------------------------------------------ #
    def _parse_response(self, response: str, scenario_type: str) -> dict:
        try:
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                return json.loads(match.group())
            return json.loads(response)
        except json.JSONDecodeError:
            logger.error(f"Claude yanıtı parse edilemedi: {response[:200]}")
            return {
                "passed": False,
                "confidence": "LOW",
                "checks": [{"name": "Parse Hatası", "passed": False,
                             "detail": "Claude yanıtı JSON olarak okunamadı"}],
                "summary": "Analiz yanıtı işlenemedi.",
                "issues": ["JSON parse hatası"],
                "recommendations": [],
            }
    # ------------------------------------------------------------------ #
    #  Otomatik Doğrulama (Kural Bazlı)
    # ------------------------------------------------------------------ #
    def _auto_verify(self, scenario_type: str, send_meta: dict,
                     received_msg: dict, combination: dict) -> dict:
        """
        LLM veya insan müdahalesi olmadan kural bazlı doğrulama.
        Sadece MIME yapısını ve temel metadata'yı kontrol eder.
        """
        checks = []
        is_passed = True
        summary = "Otomatik kontrol tamamlandı."

        # 1. Temiz Gönderen/Alıcı Kontrolü
        headers = received_msg.get("headers", {})
        subj = headers.get("subject", "").lower()
        orig_subj = send_meta.get("subject", "").lower()
        
        # Konu eşleşmesi (Prefix'leri tolere et)
        subject_match = orig_subj in subj or subj in orig_subj or "[auto-test]" in subj
        checks.append({
            "name": "Konu Eşleşmesi",
            "passed": subject_match,
            "detail": f"Gelen: {headers.get('subject', '?')}"
        })

        # 2. Senaryo Spesifik Kurallar
        if scenario_type == "plain_text":
            body_exists = any(p.get("content_type", "").startswith("text/plain") for p in received_msg.get("parts", []))
            checks.append({"name": "Metin İçeriği", "passed": body_exists, "detail": "text/plain part mevcut."})
        
        elif scenario_type == "attachment" or scenario_type == "multi_attachment":
            has_attach = len(received_msg.get("attachments", [])) > 0
            count = len(received_msg.get("attachments", []))
            checks.append({"name": "Ek Dosya", "passed": has_attach, "detail": f"{count} ek bulundu."})
            if scenario_type == "multi_attachment":
                checks.append({"name": "Çoklu Ek Kontrolü", "passed": count >= 2, "detail": f"En az 2 ek bekleniyor, {count} bulundu."})

        elif scenario_type == "inline_image":
            has_inline = len(received_msg.get("inline_images", [])) > 0
            checks.append({"name": "Inline Resim", "passed": has_inline, "detail": "CID referanslı resim bulundu."})

        elif scenario_type == "reply_chain":
            has_in_reply = bool(headers.get("in_reply_to"))
            checks.append({"name": "MIME Reply Zinciri", "passed": has_in_reply, "detail": "In-Reply-To header mevcut."})

        elif scenario_type == "calendar_invite":
            has_cal = any("calendar" in p.get("content_type", "").lower() for p in received_msg.get("parts", []))
            checks.append({"name": "Takvim Partı", "passed": has_cal, "detail": "text/calendar part bulundu."})

        elif scenario_type == "html_table":
            html_parts = [p.get("full_text", "") for p in received_msg.get("parts", []) if "html" in p.get("content_type", "").lower()]
            has_table = any("<table" in h.lower() for h in html_parts)
            checks.append({"name": "Tablo Yapısı (HTML)", "passed": has_table, "detail": "HTML içerisinde <table> etiketi tespit edildi."})

        elif scenario_type == "forward_chain":
            is_fwd = "fwd:" in subj or "fw:" in subj
            checks.append({"name": "Forward Prefix", "passed": is_fwd, "detail": "Konu Fwd/Fw ile başlıyor."})

        # Genel sonuç
        failed_count = sum(1 for c in checks if not c["passed"])
        is_passed = failed_count == 0
        
        return {
            "passed": is_passed,
            "confidence": "AUTO-RULE",
            "checks": checks,
            "summary": f"Kurallar ile {'BAŞARILI' if is_passed else 'BAŞARISIZ'} olarak işaretlendi.",
            "issues": [c["name"] for c in checks if not c["passed"]],
            "recommendations": ["Analizi derinleştirmek için 'Aktif' moda geçin."]
        }
