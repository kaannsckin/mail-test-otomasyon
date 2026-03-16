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
        if self.mode not in ("active", "passive"):
            raise ValueError(f"Geçersiz mode: {mode}. 'active' veya 'passive' kullanın.")
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

        headers_str = json.dumps(received_msg["headers"], ensure_ascii=False, indent=2)
        parts_str = json.dumps(received_msg["parts"][:5], ensure_ascii=False, indent=2)
        attachments_str = json.dumps(received_msg["attachments"], ensure_ascii=False, indent=2)
        inline_str = json.dumps(received_msg["inline_images"], ensure_ascii=False, indent=2)
        send_meta_str = json.dumps({k: v for k, v in send_meta.items() if k != "raw_bytes"},
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
        }

        checks = scenario_checks.get(scenario_type, "Genel mesaj iletim kontrolü yap.")

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
                       received_msg: dict, combination: dict) -> dict:
        """
        Pasif modda doğrulama.
        - Terminal (stdin.isatty): CLI interactive input
        - Subprocess (app-driven): Dosya-tabanlı IPC ile bekleme
        """
        headers = received_msg.get("headers", {})
        subject = headers.get("Subject", "?")
        from_addr = headers.get("From", "?")

        test_info = {
            "scenario": scenario_type,
            "label": combination.get("label", "?"),
            "from": from_addr,
            "subject": subject,
            "date": headers.get("Date", "?"),
            "sender_server": combination.get("sender_server", "?"),
            "receiver_server": combination.get("receiver_server", "?"),
            "attachments": len(received_msg.get("attachments", [])),
            "inline_images": len(received_msg.get("inline_images", [])),
        }

        if sys.stdin.isatty():
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
