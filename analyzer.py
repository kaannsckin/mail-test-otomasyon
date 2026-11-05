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
import httpx  # Gemini REST çağrısı için (requirements.txt'te açıkça listeli)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Güncel Claude modellerinde düşünme (thinking) her zaman açıktır ve max_tokens
# bütçesinden pay alır; 2000 gibi düşük bir sınır yanıtı JSON'un ortasında
# keserdi. 16000, akışsız (non-streaming) isteklerin önerilen üst sınırıdır.
CLAUDE_MAX_TOKENS = 16000
# Sınıflandırıcı bir isteği reddederse API aynı isteği uygun modelde yeniden
# çalıştırır ("default" = Anthropic'in kategoriye göre önerdiği model).
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# LLM'e art arda bu kadar kez ulaşılamazsa (ağ/zaman aşımı/5xx) kalan
# senaryolar için API çağrılmaz; ölü bir uç nokta tam koşuyu saatlerce
# kilitlemesin (eskiden senaryo başına ~6 dk → tam matris ~20 saat).
CIRCUIT_BREAKER_THRESHOLD = 3

# Analiz kararının şeması: yapılandırılmış çıktı (output_config.format) ile
# model geçerli JSON döndürmeye zorlanır; yanıt yine de doğrulanır.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "detail": {"type": "string"},
                },
                "required": ["name", "passed", "detail"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["passed", "confidence", "checks", "summary", "issues", "recommendations"],
    "additionalProperties": False,
}


def _json_default(value):
    """json.dumps'ın bilmediği tipler (ör. ek dosyasının ilk baytları) prompt'u
    çökertmesin — eskiden her ekli senaryo burada TypeError ile düşüyordu."""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bayt ikili veri>"
    return str(value)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=_json_default)


def _failure(summary: str, issue: str, check_name: str = "Analiz") -> dict:
    return {
        "passed": False,
        "confidence": "LOW",
        "checks": [{"name": check_name, "passed": False, "detail": issue}],
        "summary": summary,
        "issues": [issue],
        "recommendations": [],
    }


class MailAnalyzer:
    def __init__(self, api_key: str, model: Optional[str] = None,
                 provider: str = "claude", effort: Optional[str] = None):
        self.provider = (provider or "claude").lower().strip()
        if self.provider not in ("claude", "gemini"):
            raise ValueError(
                f"Bilinmeyen provider: {provider}. 'claude' veya 'gemini' kullanın."
            )
        self.api_key = str(api_key or "")
        self.effort = (effort or DEFAULT_EFFORT).lower().strip()
        # Yapılandırılmış çıktı / fallback / effort'u desteklemeyen eski bir
        # model seçilmişse ilk 400'den sonra sade isteğe geçilir (yapışkan).
        self._enhanced = True
        self._consecutive_unavailable = 0
        self._circuit_reason: Optional[str] = None
        if self.provider == "gemini":
            self.model = model or DEFAULT_GEMINI_MODEL
            self.client = None
        else:
            self.model = model or DEFAULT_MODEL
            # SDK 408/409/429/5xx ve bağlantı hatalarında exponential backoff ile
            # bir kez yeniden dener; senaryo başına en kötü durum ~3 dk.
            self.client = anthropic.Anthropic(api_key=self.api_key, max_retries=1, timeout=90.0)

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

        if self._circuit_reason:
            return _failure(
                f"LLM analizi yapılmadı: {self._circuit_reason}",
                self._circuit_reason, check_name="LLM Analizi")

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

        headers_str = _dumps(received_msg.get("headers", {}))
        parts_str = _dumps(received_msg.get("parts", [])[:5])
        attachments_str = _dumps(received_msg.get("attachments", []))
        inline_str = _dumps(received_msg.get("inline_images", []))
        send_meta_str = _dumps({k: v for k, v in send_meta.items() if k != "raw_bytes"})

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
            "calendar_invite": f"""
Gönderilen davet: UID={send_meta.get('ics_uid', '?')} | \
METHOD={send_meta.get('ics_method', 'REQUEST')} | \
Başlangıç={send_meta.get('event_start', '?')}

Kontrol edilecekler:
1. text/calendar part'ı mevcut mu? (parts içinde content_type=text/calendar olmalı)
2. METHOD=REQUEST parametresi korunmuş mu? (davet olarak tanınması için şart)
3. invite.ics eki iletilmiş mi? (attachments içinde .ics dosyası)
4. VEVENT içeriği bozulmamış mı? (UID {send_meta.get('ics_uid', '?')} korunmalı)
5. Sunucu daveti düz ek dosyaya indirgemiş mi? (text/calendar kaybolduysa
   istemci "Kabul Et / Reddet" düğmelerini gösteremez — bu bir BAŞARISIZLIKTIR)
""",
            "i18n": f"""
Gönderilen alfabe grupları: {send_meta.get('body_charsets', [])}
Başlık ASCII mi: {send_meta.get('subject_is_ascii', '?')} (False bekleniyor)

Kontrol edilecekler:
1. Subject header'ı RFC 2047 encoded-word ile kodlanmış mı? (=?utf-8?...?= biçimi)
   veya çözülmüş hâliyle özel karakterler bozulmadan okunuyor mu?
2. Gövdedeki Türkçe karakterler korunmuş mu? (ğüşıöç ĞÜŞİÖÇ)
3. Latin dışı alfabeler korunmuş mu? (Arapça ﷽, CJK 漢字)
4. Emoji karakterleri korunmuş mu? (🚀🔥🐞 — soru işareti/kutu olmamalı)
5. charset=utf-8 mi ve Content-Transfer-Encoding uygun mu?
   (8bit / base64 / quoted-printable kabul edilir; us-ascii'ye düşürülmüş
   olması VERİ KAYBI demektir)
""",
            "complex_html": f"""
Gönderilen HTML uzunluğu: {send_meta.get('html_length', '?')} karakter
Düz metin alternatifi var mı: {send_meta.get('has_plain_fallback', '?')}

Kontrol edilecekler:
1. multipart/alternative yapısı korunmuş mu? (hem text/plain hem text/html)
2. text/html part'ı mevcut ve gövdesi boş değil mi?
3. CSS içeriği korunmuş mu? (<style> bloğu veya inline style attribute'ları)
4. Media query (@media) ifadesi hayatta kalmış mı? Kaldırıldıysa duyarlı
   yerleşim bozulur — bunu bir uyarı olarak raporla.
5. HTML uzunluğu ciddi şekilde kısalmış mı? (sunucu sanitizasyonu içeriği
   buduyor olabilir)
""",
            "html_table": """
Kontrol edilecekler:
1. text/html part'ı mevcut mu?
2. <table> yapısı korunmuş mu? (tr/td/th etiketleri hayatta mı)
3. Hücre stilleri (border, padding, background) korunmuş mu?
4. Tablo içindeki Türkçe karakterler bozulmamış mı? (ğüşıöç)
5. Tablo düz metne indirgenmiş mi? (yapı kaybolduysa BAŞARISIZ sayılır)
""",
            "forward": f"""
İletilen orijinal mesaj: {send_meta.get('original_msg_id', '?')} | \
Konu: {send_meta.get('original_subject', '?')}

Kontrol edilecekler:
1. Subject "Fwd:" öneki ile başlıyor mu?
2. message/rfc822 part'ı mevcut mu? (orijinal mesaj kapsüllenmiş olmalı)
3. Kapsüllenen mesajın header'ları okunabiliyor mu?
   (orijinal Message-ID {send_meta.get('original_msg_id', '?')} korunmalı)
4. Gövdedeki "---------- İletilen Mesaj ----------" bloğu ve orijinal
   Kimden/Tarih/Konu bilgileri duruyor mu?
5. MIME boundary izolasyonu bozulmuş mu? (iç içe part'lar karışmamalı)
""",
            "multi_attachment": f"""
Gönderilen ek sayısı: {send_meta.get('attachment_count', '?')} | \
Dosyalar: {send_meta.get('attachment_name', '?')}

Kontrol edilecekler:
1. Eklerin TAMAMI iletilmiş mi? (beklenen adet:
   {send_meta.get('attachment_count', '?')} — eksik ek BAŞARISIZLIKTIR)
2. Her ekin dosya adı korunmuş mu? (Türkçe karakterli adlar dahil, RFC 2231)
3. Her ekin boyutu orijinaliyle tutarlı mı?
4. MIME type'lar doğru atanmış mı? (application/pdf, text/csv vb.)
5. multipart/mixed yapısı ve base64 kodlaması bozulmamış mı?
""",
        }
        # Eski ad — CSV'de 'forward_chain' geçen kurulumlar için
        scenario_checks["forward_chain"] = scenario_checks["forward"]

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
    def _open_circuit(self, reason: str) -> None:
        if not self._circuit_reason:
            self._circuit_reason = reason
            logger.error(f"LLM devre dışı — kalan senaryolar analiz edilmeyecek: {reason}")

    def _note_unavailable(self, reason: str) -> None:
        self._consecutive_unavailable += 1
        if self._consecutive_unavailable >= CIRCUIT_BREAKER_THRESHOLD:
            self._open_circuit(f"API'ye art arda {self._consecutive_unavailable} kez "
                               f"ulaşılamadı ({reason})")

    def _claude_request(self, prompt: str):
        kwargs = {
            "model": self.model,
            "max_tokens": CLAUDE_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        if not self._enhanced:
            return self.client.messages.create(**kwargs)
        return self.client.beta.messages.create(
            **kwargs,
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
            },
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    def _call_claude(self, prompt: str) -> str:
        try:
            try:
                response = self._claude_request(prompt)
            except anthropic.BadRequestError as e:
                if not self._enhanced:
                    raise
                # Seçilen model yapılandırılmış çıktı/effort/fallback'i
                # desteklemiyor olabilir (eski model) — sade istekle tekrar dene.
                # Sade istek BAŞARILI olursa sorunun gelişmiş parametreler
                # olduğu kanıtlanmış olur ve tercih kalıcılaşır; tek seferlik bir
                # 400 (ör. aşırı uzun prompt) yapılandırılmış çıktıyı kapatmaz.
                logger.warning(
                    f"Model ({self.model}) gelişmiş istek parametrelerini reddetti, "
                    f"sade istekle deneniyor: {e}")
                self._enhanced = False
                try:
                    response = self._claude_request(prompt)
                except Exception:
                    self._enhanced = True
                    raise

            self._consecutive_unavailable = 0
            if response.stop_reason == "refusal":
                return json.dumps(_failure(
                    "Claude analizi reddetti (refusal).", "stop_reason=refusal",
                    check_name="LLM Analizi"))
            text = next((b.text for b in response.content
                         if getattr(b, "type", None) == "text"), "")
            if response.stop_reason == "max_tokens":
                logger.error("Claude yanıtı max_tokens sınırında kesildi.")
                return json.dumps(_failure(
                    "Analiz yanıtı token sınırında kesildi.", "stop_reason=max_tokens",
                    check_name="LLM Analizi"))
            return text
        except anthropic.AuthenticationError as e:
            error_msg = "API key geçersiz — Konfigürasyon sayfasından güncelleyin."
            exc: Exception = e
            self._open_circuit(error_msg)          # sonraki her çağrı da aynı sonucu verir
        except anthropic.PermissionDeniedError as e:
            error_msg = "API key'in bu modele/özelliğe erişim izni yok."
            exc = e
            self._open_circuit(error_msg)
        except anthropic.NotFoundError as e:
            error_msg = f"Model bulunamadı: {self.model} — Konfigürasyondan güncel bir model seçin."
            exc = e
            self._open_circuit(error_msg)
        except anthropic.RateLimitError as e:
            error_msg = "Claude API rate limit aşıldı — daha sonra tekrar deneyin."
            exc = e
            self._note_unavailable(error_msg)
        except anthropic.APIStatusError as e:
            error_msg = f"Claude API hatası (HTTP {e.status_code})."
            exc = e
            if e.status_code >= 500:
                self._note_unavailable(error_msg)
        except anthropic.APIConnectionError as e:
            error_msg = "Claude API'ye bağlanılamadı ya da zaman aşımı — ağ bağlantısını kontrol edin."
            exc = e
            self._note_unavailable(error_msg)
        except Exception as e:  # noqa: BLE001 — bozuk yanıt gövdesi vb. koşuyu düşürmesin
            error_msg = f"Beklenmeyen API yanıtı ({type(e).__name__})."
            exc = e

        logger.error(f"Claude API hatası: {exc}")
        return json.dumps(_failure(f"Claude API erişim hatası: {error_msg}", str(exc),
                                   check_name="LLM Analizi"))

    # ------------------------------------------------------------------ #
    #  Gemini API çağrısı (REST)
    # ------------------------------------------------------------------ #
    def _call_gemini(self, prompt: str) -> str:
        url = f"{GEMINI_API_BASE}/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 8192,
                                 "responseMimeType": "application/json"},
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
    @staticmethod
    def _extract_json(text: str):
        """Yanıttaki ilk geçerli JSON nesnesini döndürür.

        Önce tamamı denenir; olmazsa her '{' konumundan raw_decode ile
        okunur — böylece JSON'dan sonra gelen ve '}' içeren açıklama metni
        (eski açgözlü regex'i bozan durum) ya da markdown çiti sorun olmaz.
        """
        text = (text or "").strip()
        try:
            whole = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            # Yanıtın tamamı geçerli JSON ama nesne değilse (liste, true, metin)
            # içinden parça cımbızlanmaz — biçim bozuk karar asla PASS olmasın.
            if isinstance(whole, dict):
                return whole
            raise ValueError("JSON kökü nesne değil")
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                obj, _ = decoder.raw_decode(text, match.start())
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
        raise ValueError("JSON bulunamadı")

    @staticmethod
    def _as_bool(value) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        return None

    def _normalize_verdict(self, data: dict) -> dict:
        """LLM kararını doğrular; şüpheli her durum FAIL/LOW'a düşer.

        Eskiden "passed": "false" (metin) PASS sayılıyor, passed anahtarı
        olmayan yanıt "atlandı" sayılıp başarı oranından düşülüyor, checks
        metin gelince rapor üretimi çöküyordu.
        """
        notes = []
        passed = self._as_bool(data.get("passed"))
        if passed is None:
            notes.append(f"Analiz yanıtında geçerli 'passed' değeri yok: {data.get('passed')!r}")
            passed = False

        confidence = str(data.get("confidence") or "").upper()
        if confidence not in ("HIGH", "MEDIUM", "LOW"):
            confidence = "LOW"

        raw_checks = data.get("checks", [])
        checks = []
        if isinstance(raw_checks, list):
            for item in raw_checks:
                if isinstance(item, dict):
                    ok = self._as_bool(item.get("passed"))
                    checks.append({"name": str(item.get("name") or "Kontrol"),
                                   "passed": bool(ok),
                                   "detail": str(item.get("detail") or "")})
                else:
                    checks.append({"name": str(item), "passed": False, "detail": ""})
        elif raw_checks:
            notes.append("Analiz yanıtında 'checks' liste değil")

        def _str_list(value):
            if isinstance(value, list):
                return [str(v) for v in value if v not in (None, "")]
            return [str(value)] if value not in (None, "") else []

        issues = _str_list(data.get("issues")) + notes
        if notes:
            confidence = "LOW"
        return {
            "passed": passed,
            "confidence": confidence,
            "checks": checks,
            "summary": str(data.get("summary") or ("Analiz yanıtı eksik." if notes else "")),
            "issues": issues,
            "recommendations": _str_list(data.get("recommendations")),
        }

    def _parse_response(self, response: str, scenario_type: str) -> dict:
        try:
            data = self._extract_json(response)
        except ValueError:
            logger.error(f"LLM yanıtı parse edilemedi: {str(response)[:200]}")
            return {
                "passed": False,
                "confidence": "LOW",
                "checks": [{"name": "Parse Hatası", "passed": False,
                             "detail": "LLM yanıtı JSON olarak okunamadı"}],
                "summary": "Analiz yanıtı işlenemedi.",
                "issues": ["JSON parse hatası"],
                "recommendations": [],
            }
        return self._normalize_verdict(data)
