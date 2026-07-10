"""
analyzer.py — LLM (Claude API veya Google Gemini) ile MIME içeriğini analiz eder.
Her senaryo tipi için özelleştirilmiş prompt'lar kullanır.

Provider seçimi config.yaml'daki ``analysis.provider`` alanı ile yapılır:
  claude (varsayılan) → anthropic SDK, ``anthropic.api_key`` / ``anthropic.model``
  gemini              → Google Generative Language REST API,
                        ``gemini.api_key`` / ``gemini.model``
"""

import json
import logging
import re
from typing import Optional

import anthropic
import httpx  # anthropic SDK'nın bağımlılığı — ek paket gerektirmez

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class MailAnalyzer:
    def __init__(self, api_key: str, model: Optional[str] = None,
                 provider: str = "claude"):
        self.provider = (provider or "claude").lower().strip()
        if self.provider not in ("claude", "gemini"):
            raise ValueError(
                f"Bilinmeyen provider: {provider}. 'claude' veya 'gemini' kullanın."
            )
        self.api_key = api_key
        if self.provider == "gemini":
            self.model = model or DEFAULT_GEMINI_MODEL
            self.client = None
        else:
            self.model = model or DEFAULT_MODEL
            # SDK 429/5xx hatalarında otomatik exponential backoff ile yeniden dener.
            self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)

    def analyze(self, scenario_type: str, send_meta: dict,
                received_msg: Optional[dict], combination: dict) -> dict:
        """
        Ana analiz metodu. Senaryo tipine göre uygun prompt seçer.
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

        prompt = self._build_prompt(scenario_type, send_meta, received_msg, combination)
        if self.provider == "gemini":
            response = self._call_gemini(prompt)
        else:
            response = self._call_claude(prompt)
        return self._parse_response(response, scenario_type)

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
    #  Claude API çağrısı
    # ------------------------------------------------------------------ #
    def _call_claude(self, prompt: str) -> str:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            if response.stop_reason == "refusal":
                return json.dumps({
                    "passed": False,
                    "confidence": "LOW",
                    "checks": [],
                    "summary": "Claude analizi reddetti (refusal).",
                    "issues": ["stop_reason=refusal"],
                    "recommendations": ["Mesaj içeriğini kontrol edin"],
                })
            text = next((b.text for b in response.content if b.type == "text"), "")
            return text
        except anthropic.AuthenticationError as e:
            error_msg = "API key geçersiz — Konfigürasyon sayfasından güncelleyin."
            exc: Exception = e
        except anthropic.RateLimitError as e:
            error_msg = "Claude API rate limit aşıldı — daha sonra tekrar deneyin."
            exc = e
        except anthropic.APIStatusError as e:
            error_msg = f"Claude API hatası (HTTP {e.status_code})."
            exc = e
        except anthropic.APIConnectionError as e:
            error_msg = "Claude API'ye bağlanılamadı — ağ bağlantısını kontrol edin."
            exc = e

        logger.error(f"Claude API hatası: {exc}")
        return json.dumps({
            "passed": False,
            "confidence": "LOW",
            "checks": [],
            "summary": f"Claude API erişim hatası: {error_msg}",
            "issues": [str(exc)],
            "recommendations": [],
        })

    # ------------------------------------------------------------------ #
    #  Gemini API çağrısı (REST)
    # ------------------------------------------------------------------ #
    def _call_gemini(self, prompt: str) -> str:
        url = f"{GEMINI_API_BASE}/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 2000},
        }
        try:
            resp = httpx.post(
                url,
                headers={"x-goog-api-key": self.api_key},  # key URL'de/loglarda görünmesin
                json=payload,
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            logger.error(f"Gemini API hatası: {e}")
            return json.dumps({
                "passed": False,
                "confidence": "LOW",
                "checks": [],
                "summary": f"Gemini API erişim hatası: {e}",
                "issues": [str(e)],
                "recommendations": [],
            })

    # ------------------------------------------------------------------ #
    #  Response parser
    # ------------------------------------------------------------------ #
    def _parse_response(self, response: str, scenario_type: str) -> dict:
        try:
            # JSON blok varsa çıkar
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
